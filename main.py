import os
import datetime
import discord
import aiohttp
import asyncio
import csv
import hmac
import io
import re
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web
import db
from snapshot import take_snapshot
import onboarding
import civicrm
import members
import country_finder
import countries
import modlog
# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MAILERSEND_API_KEY = os.getenv('MAILERSEND_API_KEY')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
# Lets the onboarding dashboard read /stats/discord; kept apart from WEBHOOK_SECRET, which can grant roles
STATS_SECRET = os.getenv('STATS_SECRET')
PORT = int(os.getenv('PORT', 8080))
DATABASE_URL = os.getenv('DATABASE_URL')
CIVICRM_URL = os.getenv('CIVICRM_URL')
CIVICRM_API_KEY = os.getenv('CIVICRM_API_KEY')

def ids_from_env(name):
    """The Discord IDs in an environment variable, separated by commas or spaces: an empty set when
    unset or blank, or None (with a warning) if any entry isn't an ID."""
    parts = [part for part in re.split(r"[\s,]+", os.getenv(name) or "") if part]
    if not all(part.isascii() and part.isdigit() for part in parts):
        print(f"Warning: {name} has an entry that isn't a Discord ID.")
        return None
    return {int(part) for part in parts}

def channel_id_from_env(name):
    """The one channel ID in an environment variable, or None when unset or not exactly one ID
    (so a bad value turns the feature off rather than stopping the bot)."""
    ids = ids_from_env(name)
    if ids and len(ids) > 1:
        print(f"Warning: {name} holds more than one ID; ignoring it.")
    return next(iter(ids)) if ids and len(ids) == 1 else None

# The moderators' log channel (#logs), where message edits and deletions are posted. Unset means off.
MESSAGE_LOG_CHANNEL_ID = channel_id_from_env('MESSAGE_LOG_CHANNEL_ID')

# Categories and channels whose edits and deletions are never posted to the log channel: internal
# groups such as staff and national leads, where nothing needs moderating. A channel or thread is
# left out if its own ID, its parent channel's or its category's is listed. None (an entry that
# isn't an ID) turns posting off altogether, so a typo never posts an internal channel.
MESSAGE_LOG_IGNORE_IDS = ids_from_env('MESSAGE_LOG_IGNORE_IDS')

ALLOWED_EXPORT_ROLE_ID = 1409324452545822793

# Developer / Production Environment Mode
DEVELOPER_MODE = os.getenv('DEVELOPER_MODE', 'false').lower() == 'true'
PAUSEAI_SERVER_ID = 1100491867675709580
MODERATORS_CHANNEL_ID = 1103655770693832775
MODERATORS_ROLE_ID = 1108034486479880236
YAGPDB_USER_ID = 204255221017214977
last_yagpdb_alert_time = None

ONBOARDING_PIPELINE_CHANNEL_ID = 1174807044990193775
# Left out of GENERAL_DM for members who've already joined (see onboarding.has_joined)
GENERAL_DM_JOIN_PARAGRAPH = "If you’re looking for an immediate action to take, join up at [pauseai.info/join](https://pauseai.info/join)."
GENERAL_DM = """Welcome to the PauseAI Global Discord, {user}!
We're delighted to have you join us.
 
Feel free to introduce yourself in the <#1101804381755686972> – it's always nice to know who's joining us. 
 
""" + GENERAL_DM_JOIN_PARAGRAPH + """
 
We also conduct welcome calls where you can meet others who are joining PauseAI, you can check them out [here](https://luma.com/PauseAI?tag=welcome).
 
Lastly, you can find others near you who are organizing in communities: <#1171376873721298984>"""

intents = discord.Intents.default()
intents.members = True # Required for on_member_join!
intents.message_content = True # Required for the message log (privileged: enable it in the Developer Portal)

from discord import app_commands
bot = commands.Bot(command_prefix='!', intents=intents)

# Shared aiohttp session — created once on startup, reused for all HTTP calls
http_session: aiohttp.ClientSession | None = None

# Postgres pool for server snapshots, the message log and member records; set up before any event is handled
db_pool = None

# Permissions a role must NOT have for the bot to grant/re-grant it automatically
# (via the webhook, or by restoring saved roles), so a leaked
# WEBHOOK_SECRET or a stale/tampered saved role can't be used to escalate.
DANGEROUS_ROLE_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "manage_messages", "kick_members", "ban_members",
    "mention_everyone", "moderate_members", "manage_nicknames",
    "manage_events", "manage_threads", "manage_emojis_and_stickers",
    "view_audit_log",
)

def is_role_safe_to_grant(role):
    if role.is_default() or role.is_bot_managed():
        return False
    return not any(getattr(role.permissions, perm) for perm in DANGEROUS_ROLE_PERMISSIONS)

def has_bearer(request, secret):
    """Whether the request's Authorization header is `Bearer <secret>`, compared in constant time."""
    # aiohttp keeps undecodable header bytes as surrogates, so encode them back rather than raise
    sent = request.headers.get("Authorization", "").encode("utf-8", "surrogateescape")
    return hmac.compare_digest(sent, f"Bearer {secret}".encode())

async def handle_webhook(request):
    if not WEBHOOK_SECRET:
        return web.json_response({"error": "Webhook secret not configured in the bot"}, status=500)

    if not has_bearer(request, WEBHOOK_SECRET):
        return web.json_response({"error": "Unauthorized"}, status=401)

    # This endpoint only ever targets the production guild, so refuse it entirely
    # while running in dev mode (mirrors the DEVELOPER_MODE gate every other handler enforces).
    if DEVELOPER_MODE:
        return web.json_response({"error": "Webhook disabled while DEVELOPER_MODE is on"}, status=403)

    try:
        data = await request.json()
        user_id = data.get("user_id")
        role_id = data.get("role_id")
        
        if not user_id or not role_id:
            return web.json_response({"error": "Missing user_id or role_id"}, status=400)
            
        guild = bot.get_guild(PAUSEAI_SERVER_ID)
        if not guild:
            return web.json_response({"error": "Guild not found"}, status=500)
            
        member = guild.get_member(int(user_id))
        if not member:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.NotFound:
                # CiviCRM matches this exact text to retry later rather than log an error: keep it as is
                return web.json_response({"error": "Member not found in guild"}, status=404)
            
        role = guild.get_role(int(role_id))
        if not role:
            return web.json_response({"error": f"Role {role_id} not found in guild"}, status=404)

        if not is_role_safe_to_grant(role):
            print(f"WEBHOOK: Refused to add disallowed role '{role.name}' to {member.name}")
            return web.json_response({"error": f"Role {role.name} is not permitted via webhook"}, status=403)

        await member.add_roles(role)
        print(f"WEBHOOK: Added role '{role.name}' to {member.name}")
        return web.json_response({"success": True, "message": f"Added role {role.name} to {member.name}"})
        
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def handle_discord_stats(request):
    """Aggregate member counts for the onboarding dashboard (see db.discord_member_stats)."""
    if not STATS_SECRET:
        return web.json_response({"error": "Stats secret not configured in the bot"}, status=500)
    if not has_bearer(request, STATS_SECRET):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        return web.json_response(await db.discord_member_stats(db_pool))
    except Exception as e:
        print(f"STATS ERROR: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

def make_app():
    app = web.Application()
    app.router.add_post('/webhook/add_role', handle_webhook)
    app.router.add_get('/stats/discord', handle_discord_stats)
    
    # Simple healthcheck for the deployment platform
    async def healthcheck(request):
        return web.json_response({"status": "ok", "bot_user": str(bot.user) if bot.user else "Starting..."})
    app.router.add_get('/', healthcheck)
    return app

async def start_web_server():
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

async def setup_hook():
    global http_session, db_pool
    http_session = aiohttp.ClientSession()
    bot.loop.create_task(start_web_server())
    # Member records live in the database, so the bot doesn't start without it
    db_pool = await db.create_pool(DATABASE_URL)
    await db.apply_schema(db_pool)
    daily_snapshot_and_prune.start()
    civicrm_link_sweep.start()
    await bot.tree.sync()

bot.setup_hook = setup_hook

def is_active_guild(guild_id):
    """Dev mode only acts on test servers, production only on the PauseAI server."""
    if DEVELOPER_MODE:
        return guild_id is not None and guild_id != PAUSEAI_SERVER_ID
    return guild_id == PAUSEAI_SERVER_ID

@tasks.loop(hours=24)
async def daily_snapshot_and_prune():
    """Replaces Xenon backups: snapshot each server's structure, and drop anything past 30 days."""
    for guild in bot.guilds:
        if not is_active_guild(guild.id):
            continue
        try:
            # The loop also runs on every restart, so skip servers snapshotted recently
            latest = await db.latest_snapshot_time(db_pool, guild.id)
            if latest and datetime.datetime.now(datetime.UTC) - latest < datetime.timedelta(hours=20):
                continue
            snapshot_id = await db.save_snapshot(db_pool, guild.id, await take_snapshot(guild))
            print(f"Saved snapshot {snapshot_id} of {guild.name}.")
        except Exception as e:
            print(f"Error snapshotting {guild.name}: {e}")
    try:
        snapshots, messages = await db.prune(db_pool)
        print(f"Pruned {snapshots} snapshot(s) and {messages} logged message(s) older than {db.RETENTION_DAYS} days.")
    except Exception as e:
        print(f"Error pruning old snapshots and messages: {e}")
    try:
        removed = await prune_log_channel()
        if removed:
            print(f"Deleted {removed} log channel post(s) older than {db.RETENTION_DAYS} days.")
    except Exception as e:
        print(f"Error pruning the log channel: {e}")

@daily_snapshot_and_prune.before_loop
async def before_daily_snapshot():
    await bot.wait_until_ready()


def role_lists(roles):
    """(role_ids, role_names) of a member's roles, leaving out @everyone."""
    roles = [r for r in roles if not r.is_default()]
    return [r.id for r in roles], [r.name for r in roles]

# Roles the bot is giving back from saved roles, as (member ID, role ID) -> when, so the
# on_member_update they cause can tell a restore from a member picking a country
restoring_roles = {}
RESTORE_MARK_SECONDS = 300


def mark_restoring(member_id, roles, now=None):
    now = now or datetime.datetime.now(datetime.UTC)
    for key, when in list(restoring_roles.items()):
        if (now - when).total_seconds() > RESTORE_MARK_SECONDS:
            del restoring_roles[key]
    for role in roles:
        restoring_roles[(member_id, role.id)] = now


def take_restored(member_id, role_id):
    """Whether this role was just given back to this member from their saved roles (forgotten once asked)."""
    return restoring_roles.pop((member_id, role_id), None) is not None


async def sync_member_record(member, onboarding_notification=False, restore_roles=False):
    """Saves the member's record. With restore_roles (only when they've just joined), first gives back
    any saved roles they don't have, such as after leaving and rejoining, that are safe to grant.
    Never restoring otherwise means a role a member dropped (say, a country they unticked in the
    country finder) isn't given back the next time their record is saved."""
    saved = await db.saved_roles(db_pool, member.id) if restore_roles else None
    roles = list(member.roles)
    if saved:
        to_restore = [r for r in members.roles_to_restore(member.guild, *saved)
                      if r not in roles and is_role_safe_to_grant(r)]
        if to_restore:
            mark_restoring(member.id, to_restore)
            try:
                await member.add_roles(*to_restore)
                print(f"Restored {len(to_restore)} roles for returning user {member.name}.")
                roles += to_restore
            except Exception as e:
                for r in to_restore:
                    take_restored(member.id, r.id)
                print(f"Error restoring roles for {member.name}: {e}")

    role_ids, role_names = role_lists(roles)
    await db.save_member(
        db_pool,
        member_id=member.id,
        username=member.name,
        nick=member.nick,
        global_name=getattr(member, 'global_name', member.name),
        joined_at=member.joined_at,
        role_ids=role_ids,
        role_names=role_names,
        onboarding_notification=onboarding_notification,
    )

async def link_to_civicrm(member):
    """Asks CiviCRM to link the member to their member record. Returns CiviCRM's refusal reason, or None
    once linked. A refusal (no member gave this username yet, or more than one did) is retried by the
    next hourly sweep. Raises civicrm.LinkError when CiviCRM doesn't answer."""
    answer = await civicrm.link(http_session, CIVICRM_URL, CIVICRM_API_KEY, member.id, member.name)
    if answer["status"] != "ok":
        return answer.get("reason") or "refused"
    await db.mark_linked(db_pool, member.id, member.name)
    print(f"CiviCRM linked {member.name}.")
    return None

@tasks.loop(hours=1)
async def civicrm_link_sweep():
    """Links everyone in the server CiviCRM hasn't linked yet, such as members who joined Discord
    before signing up. Runs on startup too."""
    if not CIVICRM_URL or not CIVICRM_API_KEY:
        return
    try:
        linked = await db.linked_member_ids(db_pool)
    except Exception as e:
        print(f"Error reading linked members, skipping this CiviCRM link sweep: {e}")
        return
    for guild in bot.guilds:
        if not is_active_guild(guild.id):
            continue
        unlinked = [m for m in guild.members if not m.bot and m.id not in linked]
        print(f"Asking CiviCRM to link {len(unlinked)} member(s) of {guild.name}...")
        newly_linked = 0
        for start in range(0, len(unlinked), civicrm.BATCH_SIZE):
            batch = unlinked[start:start + civicrm.BATCH_SIZE]
            try:
                answers = await civicrm.link_many(http_session, CIVICRM_URL, CIVICRM_API_KEY,
                                                  [(m.id, m.name) for m in batch])
            except civicrm.LinkError as e:
                # Every other batch would fail the same way: try again next hour
                print(f"Stopping the CiviCRM link sweep, CiviCRM didn't answer: {e}")
                break
            for member, answer in zip(batch, answers):
                if answer["status"] == "ok":
                    try:
                        await db.mark_linked(db_pool, member.id, member.name)
                        newly_linked += 1
                    except Exception as e:
                        print(f"Error recording {member.name} as linked: {e}")
        print(f"CiviCRM linked {newly_linked} of them.")

@civicrm_link_sweep.before_loop
async def before_civicrm_link_sweep():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f'Ready! Logged in as {bot.user}')
    for guild in bot.guilds:
        if not is_active_guild(guild.id):
            continue
        # Looked up by name on each role change too, so a role created or renamed later is picked up
        for name in onboarding.TIER_ROLE_NAMES:
            tier_role = discord.utils.get(guild.roles, name=name)
            if tier_role:
                print(f"{name} role in {guild.name}: {tier_role.id}")
            else:
                print(f"No '{name}' role in {guild.name} yet: skipping it when choosing who to ping")
    print('Listening for users joining...')
    bot.loop.create_task(startup_sync_recent_members())

async def startup_sync_recent_members():
    """Auto-sync members from the last 10 minutes just in case the bot went down."""
    now = datetime.datetime.now(datetime.UTC)
    delta = datetime.timedelta(minutes=10)
    
    await asyncio.sleep(5) # Give the gateway a moment to finish chunking and caching members
    
    for guild in bot.guilds:
        # Respect developer mode rules
        if DEVELOPER_MODE and guild.id == PAUSEAI_SERVER_ID:
            continue
        if not DEVELOPER_MODE and guild.id != PAUSEAI_SERVER_ID:
            continue
            
        print(f"Performing startup sync for {guild.name}...")
        synced_count = 0
        for member in guild.members:
            if member.joined_at and (now - member.joined_at) <= delta:
                try:
                    await sync_member_record(member, restore_roles=True)
                    synced_count += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"Error auto-syncing {member.name}: {e}")
                    
        print(f"Startup check complete for {guild.name}: Auto-synced {synced_count} recent members.")

async def _handle_member_join(member):
    """Background task: saves the new member's record and asks CiviCRM to link them."""
    await asyncio.sleep(5) # Small delay to ensure they are fully joined
    guild = member.guild
    member = guild.get_member(member.id)
    if not member:
        return
    try:
        await sync_member_record(member, restore_roles=True)
        if CIVICRM_URL and CIVICRM_API_KEY:
            reason = await link_to_civicrm(member)
            if reason:
                print(f"CiviCRM did not link {member.name}: {reason}")
    except Exception as e:
        print(f"Error saving or linking new member {member.name}: {e}")

# Members with a delayed welcome DM scheduled or still running: they're new, however long ago
# they joined (say, passing rules screening days later), and their country DMs come with it
awaiting_welcome = set()


def schedule_welcome_dm(member):
    # Marked before the task starts, so the same update's role handling already sees it
    awaiting_welcome.add(member.id)
    asyncio.create_task(send_delayed_welcome_dm(member))


async def send_delayed_welcome_dm(member):
    """Wait temporarily after joining/screening to give the user time to settle, then send a DM."""
    try:
        await _send_delayed_welcome_dm(member)
    finally:
        awaiting_welcome.discard(member.id)


async def _send_delayed_welcome_dm(member):
    await asyncio.sleep(180) # Wait 3 minutes

    guild = member.guild
    member = guild.get_member(member.id)
    if not member:
        return

    # Every country role they picked during the 3 minutes, each with its own DM if it has one
    country_infos = [info for _, info in countries.infos(member.roles)]
    # Members who've already joined don't need asking to
    skip = GENERAL_DM_JOIN_PARAGRAPH if onboarding.has_joined(member) else None
    for dm in onboarding.welcome_dms(country_infos, GENERAL_DM, member.name, skip_paragraph=skip):
        try:
            await member.send(dm)
        except discord.Forbidden:
            print(f"Could not DM {member.name} - they might have DMs disabled.")
            return

@bot.event
async def on_member_join(member):
    if DEVELOPER_MODE and member.guild.id == PAUSEAI_SERVER_ID:
        print(f"DEV MODE: Ignored {member.name} joining public server.")
        return
    if not DEVELOPER_MODE and member.guild.id != PAUSEAI_SERVER_ID:
        print(f"PROD MODE: Ignored {member.name} joining test/dev server.")
        return

    print(f"User {member.name} joined! Saving their record...")
    asyncio.create_task(_handle_member_join(member))

    # If they are NOT pending (meaning no Rules Screening), send Welcome DM on a timer
    if not getattr(member, 'pending', False):
        schedule_welcome_dm(member)

@bot.event
async def on_member_update(before, after):
    if DEVELOPER_MODE and after.guild.id == PAUSEAI_SERVER_ID:
        return
    if not DEVELOPER_MODE and after.guild.id != PAUSEAI_SERVER_ID:
        return

    # User just passed rules screening! Send delayed DM.
    if getattr(before, 'pending', False) and not getattr(after, 'pending', False):
        schedule_welcome_dm(after)

    # A dropped role is saved straight away, so a later join-time restore (startup sync, /sync_recent)
    # doesn't give back, say, a country the member unticked in the country finder
    if any(role not in after.roles for role in before.roles):
        task = asyncio.create_task(save_record_safely(after))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    # Check if any new roles were added
    new_roles = [role for role in after.roles if role not in before.roles]
    # Any role named after a country in jobs/country_regions_table.py (see countries.py)
    added_countries = countries.infos(new_roles)
    added_tier_names = [role.name for role in new_roles if role.name in onboarding.TIER_ROLE_NAMES]

    if not added_countries and not added_tier_names:
        return

    channel = bot.get_channel(int(ONBOARDING_PIPELINE_CHANNEL_ID)) if ONBOARDING_PIPELINE_CHANNEL_ID else None
    # Read the clock once, so every post in this update agrees on who counts as an existing member
    now = datetime.datetime.now(datetime.UTC)
    # None (counted as a new member) while their delayed welcome is still to come
    joined_at = None if after.id in awaiting_welcome else after.joined_at

    # Became a Chapter Greeter / National Leader: announce it for every country role they hold,
    # including any gained in this same update (their own "joined" post is then skipped, but only
    # if the announcement went out)
    announced_tier_names = []
    if added_tier_names and channel:
        held_countries = [info for _, info in countries.infos(after.roles)]
        tier_msg = onboarding.tier_role_message(added_tier_names, held_countries, after.mention)
        if tier_msg:
            try:
                await channel.send(tier_msg)
                announced_tier_names = added_tier_names
            except discord.HTTPException as e:
                print(f"Error posting tier role post for {after.name}: {e}")

    if not added_countries:
        return

    # Will track if we need to set the notification flag
    onboarding_notification = False

    # By name, so test servers' own roles work and a role created after startup is found
    tier_roles = [discord.utils.get(after.guild.roles, name=name) for name in onboarding.TIER_ROLE_NAMES]

    for role, info in added_countries:
        # Ping the onboarders in #onboarding-pipeline (skipped for noPing countries)
        if channel:
            ping_msg, onboarder_ids = onboarding.onboarding_ping(
                info, role, tier_roles, after.id, after.mention, now=now,
                added_tier_names=announced_tier_names, joined_at=joined_at,
                restored=take_restored(after.id, role.id))
            if ping_msg:
                # A failed post shouldn't stop the other countries' posts, the email (if any) and the record update below
                try:
                    await channel.send(ping_msg)
                    if onboarder_ids:
                        onboarding_notification = True
                except discord.HTTPException as e:
                    print(f"Error posting onboarding ping for {after.name} ({info['name']}): {e}")

        # Email the chapter's address through MailerSend, for countries with an "email" (see countries.py)
        # Not from a developer-mode bot: test servers' roles have the same names, but the addresses are real
        target_email = info.get("email")
        if target_email and not DEVELOPER_MODE:
            url = "https://api.mailersend.com/v1/email"
            headers = {
                "Authorization": f"Bearer {MAILERSEND_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "from": {"email": "info@pauseai.info", "name": "PauseAI Info"},
                "to": [{"email": target_email, "name": f"{info['name']} Onboarder"}],
                "subject": f"{after.name} has joined PauseAI Discord",
                "text": f"A new user just joined the Discord server and received the {info['name']} role!\n\nUser: {after.name}",
                "html": f"<strong>A new user just joined the Discord server and received the {info['name']} role!</strong><br><br>User: {after.name}"
            }

            try:
                async with http_session.post(url, headers=headers, json=payload) as response:
                    if response.status in (200, 202):
                        print(f"Email sent successfully to {target_email}!")
                    else:
                        response_text = await response.text()
                        print(f"Error from MailerSend for {target_email}: {response.status} - {response_text}")
            except Exception as e:
                print(f"Error sending email to {target_email}: {e}")

    # An existing member adding a country gets that country's own welcome now (new members get
    # theirs, with the general welcome, from send_delayed_welcome_dm)
    added_infos = [info for _, info in added_countries]
    for dm in onboarding.added_country_dms(added_infos, tier_roles, after.id, after.name, joined_at, now):
        try:
            await after.send(dm)
        except discord.Forbidden:
            print(f"Could not DM {after.name} - they might have DMs disabled.")
            break
        except discord.HTTPException as e:
            print(f"Error sending country DM to {after.name}: {e}")

    print(f"User {after.name} received country role(s)! Updating their record and notifying coordinators.")
    await sync_member_record(after, onboarding_notification=onboarding_notification)

async def save_record_safely(member):
    try:
        await sync_member_record(member)
    except Exception as e:
        print(f"Error saving {member.name}'s record after a role was removed: {e}")

@bot.event
async def on_member_remove(member):
    if DEVELOPER_MODE and member.guild.id == PAUSEAI_SERVER_ID:
        return
    if not DEVELOPER_MODE and member.guild.id != PAUSEAI_SERVER_ID:
        return
        
    print(f"User {member.name} left the server!")

    # Saving the roles they had means any granted since the last sync come back if they rejoin
    role_ids, role_names = role_lists(member.roles)
    try:
        await db.mark_left(db_pool, member.id, member.name, datetime.datetime.now(datetime.UTC), role_ids, role_names)
    except Exception as e:
        print(f"Error marking {member.name} as left: {e}")

@bot.tree.command(name='export_members', description="Generates a CSV export of all members.")
async def export_members(interaction: discord.Interaction):
    if not interaction.guild:
        return
    if DEVELOPER_MODE and interaction.guild.id == PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in public server in Dev Mode.", ephemeral=True)
        return
    if not DEVELOPER_MODE and interaction.guild.id != PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in test server in Prod Mode.", ephemeral=True)
        return

    is_admin = interaction.user.guild_permissions.administrator
    has_role = False
    if ALLOWED_EXPORT_ROLE_ID != 0:
        has_role = any(role.id == ALLOWED_EXPORT_ROLE_ID for role in interaction.user.roles)

    if not is_admin and not has_role:
        await interaction.response.send_message("❌ You must be an Administrator or have the required role to run this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(['id', 'username', 'nick', 'global_name', 'joined_at', 'role_ids', 'role_names'])
    
    for member in interaction.guild.members:
        joined_at_str = member.joined_at.isoformat() if member.joined_at else ""
        role_ids_str = ", ".join([str(role.id) for role in member.roles if role.name != "@everyone"])
        role_names_str = ", ".join([role.name for role in member.roles if role.name != "@everyone"])
        
        writer.writerow([
            str(member.id),
            member.name,
            member.nick or "",
            getattr(member, 'global_name', member.name) or "",
            joined_at_str,
            role_ids_str,
            role_names_str
        ])
        
    csv_buffer.seek(0)
    file = discord.File(fp=csv_buffer, filename="members_export.csv")
    await interaction.followup.send(content="✅ Here is the export of all members:", file=file, ephemeral=True)

@bot.tree.command(name='sync_member', description="Syncs a specific member to the database.")
@app_commands.describe(member="The Discord member to sync")
async def sync_member(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild:
        return
    if DEVELOPER_MODE and interaction.guild.id == PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in public server in Dev Mode.", ephemeral=True)
        return
    if not DEVELOPER_MODE and interaction.guild.id != PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in test server in Prod Mode.", ephemeral=True)
        return

    is_admin = interaction.user.guild_permissions.administrator
    has_role = False
    if ALLOWED_EXPORT_ROLE_ID != 0:
        has_role = any(role.id == ALLOWED_EXPORT_ROLE_ID for role in interaction.user.roles)

    if not is_admin and not has_role:
        await interaction.response.send_message("❌ You must be an Administrator or have the required role to run this command.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        await sync_member_record(member)
        await interaction.followup.send(content=f"✅ Successfully synced {member.name}!")
    except Exception as e:
        await interaction.followup.send(content=f"❌ Failed to sync {member.name}: {e}")

@bot.tree.command(name='sync_recent', description="Syncs members who joined in the last N minutes.")
@app_commands.describe(minutes="Minutes backward to check for new members")
async def sync_recent_cmd(interaction: discord.Interaction, minutes: float = 60.0):
    if not interaction.guild:
        return
    if DEVELOPER_MODE and interaction.guild.id == PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in public server in Dev Mode.", ephemeral=True)
        return
    if not DEVELOPER_MODE and interaction.guild.id != PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in test server in Prod Mode.", ephemeral=True)
        return

    is_admin = interaction.user.guild_permissions.administrator
    has_role = False
    if ALLOWED_EXPORT_ROLE_ID != 0:
        has_role = any(role.id == ALLOWED_EXPORT_ROLE_ID for role in interaction.user.roles)

    if not is_admin and not has_role:
        await interaction.response.send_message("❌ You must be an Administrator or have the required role to run this command.", ephemeral=True)
        return

    await interaction.response.defer()
    
    now = datetime.datetime.now(datetime.UTC)
    delta = datetime.timedelta(minutes=minutes)
    
    synced_count = 0
    for m in interaction.guild.members:
        if m.joined_at and (now - m.joined_at) <= delta:
            try:
                await sync_member_record(m, restore_roles=True)
                synced_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Error syncing {m.name} in sync_recent: {e}")
                
    await interaction.followup.send(content=f"✅ Finished! Synced {synced_count} recent members.")

@bot.tree.command(name='sync_old_notifications', description="Retroactively records old onboarding notifications")
@app_commands.describe(limit="Number of past messages to scan (default 200)")
async def sync_old_notifications_cmd(interaction: discord.Interaction, limit: int = 200):
    if not interaction.guild:
        return
    if DEVELOPER_MODE and interaction.guild.id == PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in public server in Dev Mode.", ephemeral=True)
        return
    if not DEVELOPER_MODE and interaction.guild.id != PAUSEAI_SERVER_ID:
        await interaction.response.send_message("❌ Cannot run in test server in Prod Mode.", ephemeral=True)
        return

    is_admin = interaction.user.guild_permissions.administrator
    has_role = False
    if ALLOWED_EXPORT_ROLE_ID != 0:
        has_role = any(role.id == ALLOWED_EXPORT_ROLE_ID for role in interaction.user.roles)

    if not is_admin and not has_role:
        await interaction.response.send_message("❌ You must be an Administrator or have the required role to run this command.", ephemeral=True)
        return

    await interaction.response.defer()
    
    channel = bot.get_channel(int(ONBOARDING_PIPELINE_CHANNEL_ID)) if ONBOARDING_PIPELINE_CHANNEL_ID else None
    if not channel:
        await interaction.followup.send(content="❌ Onboarding channel not configured or found.")
        return
        
    await interaction.followup.send(content="⏳ Scanning channel history... please wait.")    
        
    notified, reacted = set(), set()
    async for message in channel.history(limit=limit):
        if message.author.id != bot.user.id:
            continue
        match = re.search(r'<@!?(\d+)>\s+has joined from', message.content)
        if match:
            notified.add(int(match.group(1)))
            if message.reactions:
                reacted.add(int(match.group(1)))

    if not notified:
        await interaction.followup.send(content="✅ Found 0 records that needed updates.")
        return

    updated = await db.set_flag(db_pool, "onboarding_notification", notified)
    await db.set_flag(db_pool, "discord_reaction", reacted)
    await interaction.followup.send(content=f"✅ Finished! Updated {updated} of the {len(notified)} users' records.")

@bot.tree.command(name='country', description="Pick your country, to get its role and its channel.")
@app_commands.guild_only()
@app_commands.describe(name="Your country. Leave it empty to browse by region.")
async def country_cmd(interaction: discord.Interaction, name: str | None = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    if not is_active_guild(interaction.guild.id):
        await interaction.response.send_message("❌ Not available on this server.", ephemeral=True)
        return
    if name:
        await country_finder.add_country(interaction, is_role_safe_to_grant, name)
    else:
        await interaction.response.send_message(country_finder.INTRO, view=country_finder.region_buttons(),
                                                ephemeral=True)

@country_cmd.autocomplete('name')
async def country_cmd_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    roles = country_finder.country_roles(interaction.guild, is_role_safe_to_grant)
    return [app_commands.Choice(name=n, value=n) for n in country_finder.autocomplete(roles, current)]

@bot.tree.command(name='country_finder_post', description="Posts the country finder in this channel.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(region="The region whose countries to post. Left empty, it's this other-<region> channel's.")
@app_commands.choices(region=[app_commands.Choice(name=r, value=i) for i, r in enumerate(country_finder.REGIONS)])
async def country_finder_post(interaction: discord.Interaction, region: app_commands.Choice[int] | None = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    if not is_active_guild(interaction.guild.id):
        await interaction.response.send_message("❌ Not available on this server.", ephemeral=True)
        return
    perms = interaction.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message("❌ You need Manage Server to post the country finder.", ephemeral=True)
        return
    await country_finder.post(interaction, is_role_safe_to_grant, region.value if region else None)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Slash commands are answered by bot.tree; this only picks up the country finder's buttons and dropdowns
    if not is_active_guild(interaction.guild_id):
        return
    try:
        await country_finder.handle_interaction(interaction, is_role_safe_to_grant)
    except Exception as e:
        print(f"Country finder error for {interaction.user}: {e!r}")

@bot.event
async def on_raw_reaction_add(payload):
    if not ONBOARDING_PIPELINE_CHANNEL_ID:
        return

    if DEVELOPER_MODE and payload.guild_id == PAUSEAI_SERVER_ID:
        return
    if not DEVELOPER_MODE and payload.guild_id != PAUSEAI_SERVER_ID:
        return

    if payload.channel_id != int(ONBOARDING_PIPELINE_CHANNEL_ID):
        return
        
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
        
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
        
    if message.author.id != bot.user.id:
        return
        
    match = re.search(r'<@!?(\d+)>\s+has joined from', message.content)
    if match:
        joined_user_id = match.group(1)
        print(f"Reaction added to notification for user {joined_user_id}. Updating their record.")
        await db.set_flag(db_pool, "discord_reaction", [int(joined_user_id)])

@bot.event
async def on_message(message):
    if message.author.id == bot.user.id:
        return

    # Check developer mode restrictions to prevent conflicts during local testing
    if message.guild:
        if DEVELOPER_MODE and message.guild.id == PAUSEAI_SERVER_ID:
            return
        if not DEVELOPER_MODE and message.guild.id != PAUSEAI_SERVER_ID:
            return

    # Replaces Quark: keep every server message for 30 days. Runs in the background so a slow
    # database never holds up the moderator ping or commands below.
    if message.guild:
        task = asyncio.create_task(log_message_safely(message))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    # If YAGPDB sends a message in the moderators channel, ping the moderators role in a thread (cooldown of 60s to prevent spam)
    if message.author.id == YAGPDB_USER_ID and message.channel.id == MODERATORS_CHANNEL_ID:
        global last_yagpdb_alert_time
        now = datetime.datetime.now(datetime.UTC)
        if last_yagpdb_alert_time is None or (now - last_yagpdb_alert_time).total_seconds() > 60:
            last_yagpdb_alert_time = now
            try:
                thread = await message.create_thread(name="YAGPDB Alert")
                await thread.send(f"<@&{MODERATORS_ROLE_ID}>")
            except Exception as e:
                print(f"Error creating thread or pinging moderators: {e}")

    await bot.process_commands(message)

# Holds references to fire-and-forget tasks so they aren't garbage collected mid-run
background_tasks = set()

# Upper bound on one log write, including waiting for a free connection, so a hung database
# can't pile up a task per message
LOG_WRITE_TIMEOUT = 15

def attachments_json(raw_attachments):
    """What message_log stores per attachment, from raw gateway dicts (Attachment.to_dict() gives the same shape)."""
    return [
        {"id": int(a["id"]), "filename": a.get("filename"), "url": a.get("url"),
         "content_type": a.get("content_type"), "size": a.get("size")}
        for a in raw_attachments
    ]

async def log_message_safely(message):
    try:
        await asyncio.wait_for(db.log_message(
            db_pool,
            message_id=message.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            content=message.content,
            attachments=attachments_json(a.to_dict() for a in message.attachments),
            created_at=message.created_at,
            author_bot=message.author.bot,
        ), LOG_WRITE_TIMEOUT)
    except Exception as e:
        print(f"Error logging message {message.id}: {e!r}")

def message_fields_from_raw(data):
    """The log_message fields of a raw gateway message, or None if the edit payload is partial."""
    try:
        return {
            "guild_id": int(data["guild_id"]),
            "channel_id": int(data["channel_id"]),
            "author_id": int(data["author"]["id"]),
            "author_bot": bool(data["author"].get("bot", False)),
            "attachments": attachments_json(data.get("attachments", [])),
            "created_at": datetime.datetime.fromisoformat(data["timestamp"]),
        }
    except (KeyError, TypeError, ValueError):
        return None

# Raw events fire even for messages that aren't in discord.py's message cache
@bot.event
async def on_raw_message_edit(payload):
    if not is_active_guild(payload.guild_id):
        return
    # Edits that only add embeds arrive without content
    if "content" not in payload.data:
        return
    # on_message never logs the bot's own messages, so neither do edits
    if payload.data.get("author", {}).get("id") == str(bot.user.id):
        return
    # Link unfurls and embed updates arrive with no edited_timestamp; they aren't edits
    edited = payload.data.get("edited_timestamp")
    edited_at = datetime.datetime.fromisoformat(edited) if edited else None
    try:
        # Passing the message's fields lets an edit that beats its on_message insert still be logged
        before = await asyncio.wait_for(db.log_edit(
            db_pool, payload.message_id, payload.data["content"], edited_at,
            message=message_fields_from_raw(payload.data),
        ), LOG_WRITE_TIMEOUT)
    except Exception as e:
        print(f"Error logging edit of message {payload.message_id}: {e}")
        return
    log_channel = log_channel_for(payload.guild_id, payload.channel_id)
    # log_edit gives the old text only for a real change to a message the log already had
    if log_channel is None or before is None or payload.data.get("author", {}).get("bot"):
        return
    try:
        author_id = int(payload.data["author"]["id"])
    except (KeyError, TypeError, ValueError):
        return
    if is_bot_user(author_id):
        return
    embed = modlog.edit_embed(
        guild_id=payload.guild_id, channel_id=payload.channel_id, message_id=payload.message_id,
        author_id=author_id, before=before, after=payload.data["content"], edited_at=edited_at,
        parent_id=thread_parent_id(payload.guild_id, payload.channel_id),
    )
    sent_at = discord.utils.snowflake_time(payload.message_id)
    await post_to_log_channel(log_channel, payload.guild_id, payload.channel_id, [(embed, sent_at)])

@bot.event
async def on_raw_message_delete(payload):
    if not is_active_guild(payload.guild_id):
        return
    cached = [payload.cached_message] if payload.cached_message else []
    await handle_deletion(payload.guild_id, payload.channel_id, [payload.message_id], cached)

@bot.event
async def on_raw_bulk_message_delete(payload):
    if not is_active_guild(payload.guild_id):
        return
    await handle_deletion(payload.guild_id, payload.channel_id, payload.message_ids, payload.cached_messages)

async def handle_deletion(guild_id, channel_id, message_ids, cached):
    """Marks the messages deleted in the log, then posts them to the log channel."""
    deleted_at = datetime.datetime.now(datetime.UTC)
    try:
        rows = list(await asyncio.wait_for(db.log_delete(db_pool, message_ids, deleted_at), LOG_WRITE_TIMEOUT))
        database_ok = True
    except Exception as e:
        print(f"Error logging deletion of {len(message_ids)} message(s) in channel {channel_id}: {e}")
        rows, database_ok = [], False
    # A message deleted before its background log write landed (a fast auto-delete) isn't in the
    # log yet, but discord.py's cache has it: log it as deleted, so the late write can't record it as
    # live, and post it from there. Never one past the retention window, which the log has dropped.
    logged = {r["message_id"] for r in rows}
    cutoff = deleted_at - datetime.timedelta(days=db.RETENTION_DAYS)
    fallback = [row_from_cached(m, guild_id) for m in cached
                if m.id not in logged and m.created_at >= cutoff and m.author.id != bot.user.id]
    if fallback and database_ok:
        try:
            # Only the ones this write marked deleted: one already marked was posted before
            fallback = await asyncio.wait_for(log_cached_deletions(fallback, deleted_at), LOG_WRITE_TIMEOUT)
        except Exception as e:
            print(f"Error logging deletion of {len(fallback)} cached message(s): {e}")
    rows += fallback
    log_channel = log_channel_for(guild_id, channel_id)
    if log_channel is not None:
        await post_deletions(log_channel, guild_id, channel_id, message_ids, rows, deleted_at)

# ── Log channel (replacing Quark's posts in #logs) ────────────────────────────

def log_channel_for(guild_id, channel_id):
    """The log channel to post an event in guild_id to, or None: when MESSAGE_LOG_CHANNEL_ID is
    unset, MESSAGE_LOG_IGNORE_IDS is malformed, the channel isn't in that server (so a test server's
    messages never reach production's log, or the other way round), or the event happened in the
    log channel itself (which would post about its own posts)."""
    if not MESSAGE_LOG_CHANNEL_ID or MESSAGE_LOG_IGNORE_IDS is None or channel_id == MESSAGE_LOG_CHANNEL_ID:
        return None
    channel = bot.get_channel(MESSAGE_LOG_CHANNEL_ID)
    if channel is None or channel.guild.id != guild_id:
        return None
    return channel

def thread_parent_id(guild_id, channel_id):
    """The parent channel's ID if channel_id is a thread the bot knows of, else None."""
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel_or_thread(channel_id) if guild else None
    return channel.parent_id if isinstance(channel, discord.Thread) else None

def is_bot_user(user_id):
    """Whether the user cache knows user_id as a bot (or as this bot)."""
    if bot.user and user_id == bot.user.id:
        return True
    user = bot.get_user(user_id)
    return bool(user and user.bot)

def is_bot_author(row):
    """Whether a logged message was a bot's. Rows logged before author_bot existed read false,
    so fall back on the user cache."""
    return bool(row["author_bot"]) or is_bot_user(row["author_id"])

async def log_cached_deletions(rows, deleted_at):
    """Logs cached rows as deleted; returns the ones that weren't already logged as deleted."""
    fields = ("message_id", "guild_id", "channel_id", "author_id", "content", "attachments", "created_at", "author_bot")
    marked = []
    for row in rows:
        if await db.log_message(db_pool, **{k: row[k] for k in fields}, deleted_at=deleted_at):
            marked.append(row)
    return marked

def row_from_cached(message, guild_id):
    """A cached discord.Message in the shape log_delete returns."""
    return {
        "message_id": message.id, "guild_id": guild_id, "channel_id": message.channel.id,
        "author_id": message.author.id, "author_bot": message.author.bot, "content": message.content,
        "attachments": attachments_json(a.to_dict() for a in message.attachments),
        "created_at": message.created_at,
    }

# Upper bound on looking up a channel that isn't cached, so a slow Discord can't stall a post
CHANNEL_LOOKUP_TIMEOUT = 10

async def channel_or_thread(guild, channel_id):
    """The channel or thread, from the cache or else from Discord (archived threads often aren't
    cached), or None if it can't be found."""
    channel = guild.get_channel_or_thread(channel_id)
    if channel is not None:
        return channel
    try:
        return await asyncio.wait_for(guild.fetch_channel(channel_id), CHANNEL_LOOKUP_TIMEOUT)
    except Exception as e:
        print(f"Couldn't look up channel {channel_id}, so its edit or deletion isn't posted: {e!r}")
        return None

async def is_ignored_channel(guild_id, channel_id):
    """Whether channel_id, its parent channel (for a thread) or its category is in
    MESSAGE_LOG_IGNORE_IDS. With the list set, a channel that can't be looked up counts as
    ignored, so a lookup failure never posts an internal channel's messages."""
    if MESSAGE_LOG_IGNORE_IDS is None:
        return True  # malformed list: ignore everything
    if not MESSAGE_LOG_IGNORE_IDS:
        return False
    if channel_id in MESSAGE_LOG_IGNORE_IDS:
        return True
    guild = bot.get_guild(guild_id)
    channel = await channel_or_thread(guild, channel_id) if guild else None
    if channel is None:
        return True
    if isinstance(channel, discord.Thread):
        # A thread's parent is a guild channel, always cached. (Thread.category_id raises when
        # the parent isn't cached, so it isn't used.)
        channel = guild.get_channel(channel.parent_id)
        if channel is None:
            return True
    return bool({channel.id, channel.category_id} & MESSAGE_LOG_IGNORE_IDS)

async def post_to_log_channel(log_channel, guild_id, channel_id, items):
    """Posts (embed, sent_at) items, where sent_at is when the quoted message was sent, and records
    each post so the daily prune can delete it once its oldest quoted message passes retention."""
    if not items or await is_ignored_channel(guild_id, channel_id):
        return
    for group in modlog.chunk(items):
        try:
            post = await log_channel.send(embeds=[embed for embed, _ in group], allowed_mentions=modlog.NO_MENTIONS)
        except Exception as e:
            print(f"Error posting to the log channel: {e}")
            return
        oldest = min(sent_at for _, sent_at in group)
        try:
            await asyncio.wait_for(db.record_log_post(db_pool, post.id, log_channel.id, oldest), LOG_WRITE_TIMEOUT)
        except Exception as e:
            # A post the prune doesn't know of would keep message text past retention, so take it
            # down, or failing that keep it for the next prune to record
            print(f"Error recording log channel post {post.id}, so deleting it: {e}")
            try:
                await post.delete()
            except Exception as e:
                print(f"Error deleting unrecorded log channel post {post.id}, will record it later: {e}")
                unrecorded_log_posts.append((post.id, log_channel.id, oldest))

# Posts that could be neither recorded nor deleted; the next prune records them first
unrecorded_log_posts = []

async def post_deletions(log_channel, guild_id, channel_id, message_ids, rows, deleted_at):
    """Posts the deleted messages. Ones the log and cache don't have are counted in one post."""
    known = {r["message_id"] for r in rows}
    unknown = sum(1 for i in message_ids if i not in known)
    parent_id = thread_parent_id(guild_id, channel_id)
    rows = sorted((r for r in rows if not is_bot_author(r)), key=lambda r: r["created_at"])
    items = [(modlog.delete_embed(r, deleted_at, parent_id), r["created_at"]) for r in rows]
    # Only for bulk deletions: a single unknown deletion has nothing worth showing
    if unknown and len(message_ids) > 1:
        items.append((modlog.unlogged_embed(unknown, channel_id, deleted_at, parent_id), deleted_at))
    await post_to_log_channel(log_channel, guild_id, channel_id, items)

async def prune_log_channel(now=None):
    """Deletes the bot's log channel posts whose oldest quoted message was sent over RETENTION_DAYS
    ago, so the channel keeps message text no longer than the database, which also counts from when
    the message was sent. Returns how many it deleted."""
    now = now or datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(days=db.RETENTION_DAYS)
    while unrecorded_log_posts:
        await db.record_log_post(db_pool, *unrecorded_log_posts[0])
        unrecorded_log_posts.pop(0)
    removed = 0
    for post in await db.expired_log_posts(db_pool, cutoff):
        # Only channels this bot posts in: a bot in developer mode never touches production's posts
        channel = bot.get_channel(post["channel_id"])
        if channel is None or not is_active_guild(channel.guild.id):
            continue
        try:
            await channel.get_partial_message(post["post_id"]).delete()
            removed += 1
        except discord.NotFound:
            pass  # someone deleted it already
        except Exception as e:
            print(f"Error deleting log channel post {post['post_id']}, will retry tomorrow: {e}")
            continue
        try:
            await db.forget_log_post(db_pool, post["post_id"])
        except Exception as e:
            print(f"Error forgetting log channel post {post['post_id']}: {e}")
    return removed

if __name__ == "__main__":
    if not DISCORD_TOKEN or not MAILERSEND_API_KEY or not DATABASE_URL:
        print("Error: DISCORD_TOKEN, MAILERSEND_API_KEY or DATABASE_URL is not set in the environment.")
    else:
        if not ONBOARDING_PIPELINE_CHANNEL_ID:
            print("Warning: ONBOARDING_PIPELINE_CHANNEL_ID is not set. The bot will not send messages in Discord.")
        if not CIVICRM_URL or not CIVICRM_API_KEY:
            print("Warning: CIVICRM_URL or CIVICRM_API_KEY is not set. The bot will not link members to CiviCRM.")
        bot.run(DISCORD_TOKEN)
