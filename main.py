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
# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MAILERSEND_API_KEY = os.getenv('MAILERSEND_API_KEY')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
PORT = int(os.getenv('PORT', 8080))
DATABASE_URL = os.getenv('DATABASE_URL')
CIVICRM_URL = os.getenv('CIVICRM_URL')
CIVICRM_API_KEY = os.getenv('CIVICRM_API_KEY')

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

FRANCE_DM = """Bienvenue {user} ! PauseAI France est animé par Pause IA, qui a son propre serveur Discord : https://discord.gg/vyXGd7AeGc
C'est là-bas que ça se passe pour agir en France. Tu peux bien sûr rester sur ce serveur aussi !

Welcome {user}! PauseAI France is run by Pause IA, which has its own Discord server: https://discord.gg/vyXGd7AeGc
That's the place to get involved in France. You're still very welcome here too!"""

# Unified definition for Countries (MailerSend Emails + YAGPDB Onboarder Pings)
# "onboarders" lists only apply until onboarding.ROLE_ROUTING_FROM; after that, every country pings by role.
COUNTRY_DATA = {
    1188719374941560925: {"name": "UK", "flag": "🇬🇧", "onboarders": [335365981276995594, 925051691798233089], "msg": "", "email": None},
    1188717849980702781: {"name": "US", "flag": "🇺🇸", "onboarders": [732773275238793266], "msg": "", "email": None},
    # France is run by Pause IA on its own Discord, so from onboarding.ROLE_ROUTING_FROM new members are
    # pointed there instead of pinging anyone. Until then, beforeRoleRouting keeps its old behaviour.
    1188719426552479824: {"name": "France", "flag": "🇫🇷", "onboarders": [], "msg": FRANCE_DM, "noPing": True, "email": None,
                          "beforeRoleRouting": {"onboarders": [456868543225528331], "msg": "", "noPing": False}},
    1188719399117541396: {"name": "Germany", "flag": "🇩🇪", "onboarders": [775477651703726092], "msg": "", "email": "germany@pauseai.info"},
    1188719443954643035: {"name": "Netherlands", "flag": "🇳🇱", "onboarders": [1363472971141746718], "msg": "", "email": None},
    1188719729662234624: {"name": "Italy", "flag": "🇮🇹", "onboarders": [1217966947057139762], "msg": "", "email": None},
    1188720344882753566: {"name": "Spain", "flag": "🇪🇸", "onboarders": [758415679526797342], "msg": "", "email": None},
    1250075465008549938: {"name": "Canada", "flag": "🇨🇦", "onboarders": [719546151593967616, 800445163520524311], "msg": "", "email": "canada@pauseai.info"},
    1188719833555161089: {"name": "Nigeria", "flag": "🇳🇬", "onboarders": [1185890162027278446], "msg": "", "email": None},
    1188720325530243142: {"name": "Romania", "flag": "🇷🇴", "onboarders": [521285222990348290], "msg": "", "email": None},
    1188719500439343135: {"name": "Australia", "flag": "🇦🇺", "onboarders": [345795697792122891], "msg": "", "email": None},
    1188719860096712756: {"name": "Poland", "flag": "🇵🇱", "onboarders": [1405864336873881644], "msg": "", "email": None},
    1429216643459842129: {"name": "Serbia", "flag": "🇷🇸", "onboarders": [1060642083347632182], "msg": "", "email": None},
    1188719899401523330: {"name": "Sweden", "flag": "🇸🇪", "onboarders": [269204846572339211], "msg": "", "email": None},
    1188719610384633918: {"name": "Czech", "flag": "🇨🇿", "onboarders": [971680888960208926], "msg": "", "email": None},
    1256260218162122843: {"name": "Kenya", "flag": "🇰🇪", "onboarders": [853298855310000148], "msg": "", "email": None},
    # No static list: pings members with this country role and Chapter Greeter, or failing that National Leader
    1188719747282518130: {"name": "Israel", "flag": "🇮🇱", "onboarders": [], "msg": "", "email": None},
    1188719975796584479: {"name": "South Korea", "flag": "🇰🇷", "onboarders": [], "msg": "", "email": None},
}

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

async def handle_webhook(request):
    auth_header = request.headers.get("Authorization")
    if not WEBHOOK_SECRET:
        return web.json_response({"error": "Webhook secret not configured in the bot"}, status=500)

    expected_header = f"Bearer {WEBHOOK_SECRET}"
    if not auth_header or not hmac.compare_digest(auth_header, expected_header):
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

async def start_web_server():
    app = web.Application()
    app.router.add_post('/webhook/add_role', handle_webhook)
    
    # Simple healthcheck for the deployment platform
    async def healthcheck(request):
        return web.json_response({"status": "ok", "bot_user": str(bot.user) if bot.user else "Starting..."})
    app.router.add_get('/', healthcheck)
    
    runner = web.AppRunner(app)
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
    daily_civicrm_link_sweep.start()
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

@daily_snapshot_and_prune.before_loop
async def before_daily_snapshot():
    await bot.wait_until_ready()


def role_lists(roles):
    """(role_ids, role_names) of a member's roles, leaving out @everyone."""
    roles = [r for r in roles if not r.is_default()]
    return [r.id for r in roles], [r.name for r in roles]

async def sync_member_record(member, onboarding_notification=False):
    """Saves the member's record, first giving back any saved roles they don't have (e.g. after
    leaving and rejoining) that are safe to grant."""
    saved = await db.saved_roles(db_pool, member.id)
    roles = list(member.roles)
    if saved:
        to_restore = [r for r in members.roles_to_restore(member.guild, *saved)
                      if r not in roles and is_role_safe_to_grant(r)]
        if to_restore:
            try:
                await member.add_roles(*to_restore)
                print(f"Restored {len(to_restore)} roles for returning user {member.name}.")
                roles += to_restore
            except Exception as e:
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
    """Asks CiviCRM to link the member to their member record. Returns True once linked. A refusal
    (no member gave this username yet, or more than one did) is retried by the next daily sweep."""
    if not CIVICRM_URL or not CIVICRM_API_KEY:
        return False
    try:
        answer = await civicrm.link(http_session, CIVICRM_URL, CIVICRM_API_KEY, member.id, member.name)
    except civicrm.LinkError as e:
        print(f"Error linking {member.name} in CiviCRM: {e}")
        return False
    if answer["status"] != "ok":
        print(f"CiviCRM did not link {member.name}: {answer.get('reason')}")
        return False
    await db.mark_linked(db_pool, member.id, member.name)
    print(f"CiviCRM linked {member.name}.")
    return True

@tasks.loop(hours=24)
async def daily_civicrm_link_sweep():
    """Links everyone in the server CiviCRM hasn't linked yet, such as members who joined Discord
    before signing up. Runs on startup too."""
    if not CIVICRM_URL or not CIVICRM_API_KEY:
        return
    linked = await db.linked_member_ids(db_pool)
    for guild in bot.guilds:
        if not is_active_guild(guild.id):
            continue
        unlinked = [m for m in guild.members if not m.bot and m.id not in linked]
        print(f"Asking CiviCRM to link {len(unlinked)} member(s) of {guild.name}...")
        newly_linked = 0
        for member in unlinked:
            try:
                newly_linked += await link_to_civicrm(member)
            except Exception as e:
                print(f"Error linking {member.name}: {e}")
        print(f"CiviCRM linked {newly_linked} of them.")

@daily_civicrm_link_sweep.before_loop
async def before_daily_civicrm_link_sweep():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f'Ready! Logged in as {bot.user}')
    routing_state = "active" if onboarding.role_routing_active() else "not active yet"
    print(f"Onboarding role routing is {routing_state} (switches on at {onboarding.ROLE_ROUTING_FROM.isoformat()})")
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
                    await sync_member_record(member)
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
    await sync_member_record(member)
    await link_to_civicrm(member)

async def send_delayed_welcome_dm(member):
    """Wait temporarily after joining/screening to give the user time to settle, then send a DM."""
    await asyncio.sleep(180) # Wait 3 minutes

    guild = member.guild
    member = guild.get_member(member.id)
    if not member:
        return

    # Every country role they picked during the 3 minutes, each with its own DM if it has one
    country_infos = [COUNTRY_DATA[role.id] for role in member.roles if role.id in COUNTRY_DATA]
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
        asyncio.create_task(send_delayed_welcome_dm(member))

@bot.event
async def on_member_update(before, after):
    if DEVELOPER_MODE and after.guild.id == PAUSEAI_SERVER_ID:
        return
    if not DEVELOPER_MODE and after.guild.id != PAUSEAI_SERVER_ID:
        return

    # User just passed rules screening! Send delayed DM.
    if getattr(before, 'pending', False) and not getattr(after, 'pending', False):
        asyncio.create_task(send_delayed_welcome_dm(after))

    # Check if any new roles were added
    new_roles = [role for role in after.roles if role not in before.roles]
    added_country_roles = [role for role in new_roles if role.id in COUNTRY_DATA]
    
    if not added_country_roles:
        return

    # Will track if we need to set the notification flag
    onboarding_notification = False
    
    channel = bot.get_channel(int(ONBOARDING_PIPELINE_CHANNEL_ID)) if ONBOARDING_PIPELINE_CHANNEL_ID else None
    
    # By name, so test servers' own roles work and a role created after startup is found
    tier_roles = [discord.utils.get(after.guild.roles, name=name) for name in onboarding.TIER_ROLE_NAMES]

    for role in added_country_roles:
        info = COUNTRY_DATA[role.id]
        
        # 1. Ping Staff in Onboarding Channel (skipped for noPing countries)
        if channel:
            ping_msg, onboarder_ids = onboarding.onboarding_ping(info, role, tier_roles, after.id, after.mention)
            if ping_msg:
                # A failed post shouldn't stop the email and the record update below
                try:
                    await channel.send(ping_msg)
                    if onboarder_ids:
                        onboarding_notification = True
                except discord.HTTPException as e:
                    print(f"Error posting onboarding ping for {after.name} ({info['name']}): {e}")
            
        # 2. Send MailerSend Email (if email exists)
        target_email = info.get("email")
        if target_email:
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

    print(f"User {after.name} received country role(s)! Updating their record and notifying coordinators.")
    await sync_member_record(after, onboarding_notification=onboarding_notification)

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
        await db.mark_left(db_pool, member.id, datetime.datetime.now(datetime.UTC), role_ids, role_names)
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
                await sync_member_record(m)
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
        
    updates_needed = {}
    async for message in channel.history(limit=limit):
        if message.author.id != bot.user.id:
            continue
            
        match = re.search(r'<@!?(\d+)>\s+has joined from', message.content)
        if match:
            joined_user_id = match.group(1)
            
            extra_fields = {"onboarding_notification": True}
            
            # Check if there's any reactions
            if len(message.reactions) > 0:
                extra_fields["discord_reaction"] = True
                
            if joined_user_id not in updates_needed:
                updates_needed[joined_user_id] = extra_fields
            else:
                updates_needed[joined_user_id].update(extra_fields)
                
    if not updates_needed:
        await interaction.followup.send(content="✅ Found 0 records that needed updates.")
        return
        
    notified = [int(uid) for uid in updates_needed]
    reacted = [int(uid) for uid, fields in updates_needed.items() if fields.get("discord_reaction")]
    updated = await db.set_flag(db_pool, "onboarding_notification", notified)
    await db.set_flag(db_pool, "discord_reaction", reacted)
    await interaction.followup.send(content=f"✅ Finished! Updated {updated} of the {len(notified)} users' records.")

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
        await asyncio.wait_for(db.log_edit(
            db_pool, payload.message_id, payload.data["content"], edited_at,
            message=message_fields_from_raw(payload.data),
        ), LOG_WRITE_TIMEOUT)
    except Exception as e:
        print(f"Error logging edit of message {payload.message_id}: {e}")

@bot.event
async def on_raw_message_delete(payload):
    if not is_active_guild(payload.guild_id):
        return
    try:
        await db.log_delete(db_pool, [payload.message_id], datetime.datetime.now(datetime.UTC))
    except Exception as e:
        print(f"Error logging deletion of message {payload.message_id}: {e}")

@bot.event
async def on_raw_bulk_message_delete(payload):
    if not is_active_guild(payload.guild_id):
        return
    try:
        await db.log_delete(db_pool, payload.message_ids, datetime.datetime.now(datetime.UTC))
    except Exception as e:
        print(f"Error logging bulk deletion in channel {payload.channel_id}: {e}")

if __name__ == "__main__":
    if not DISCORD_TOKEN or not MAILERSEND_API_KEY or not DATABASE_URL:
        print("Error: DISCORD_TOKEN, MAILERSEND_API_KEY or DATABASE_URL is not set in the environment.")
    else:
        if not ONBOARDING_PIPELINE_CHANNEL_ID:
            print("Warning: ONBOARDING_PIPELINE_CHANNEL_ID is not set. The bot will not send messages in Discord.")
        if not CIVICRM_URL or not CIVICRM_API_KEY:
            print("Warning: CIVICRM_URL or CIVICRM_API_KEY is not set. The bot will not link members to CiviCRM.")
        bot.run(DISCORD_TOKEN)
