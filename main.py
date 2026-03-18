import os
import urllib.parse
import datetime
import discord
import aiohttp
import asyncio
import json
import csv
import io
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web
# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MAILERSEND_API_KEY = os.getenv('MAILERSEND_API_KEY')
AIRTABLE_PERSONAL_ACCESS_TOKEN = os.getenv('AIRTABLE_PERSONAL_ACCESS_TOKEN')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
PORT = int(os.getenv('PORT', 8080))

ALLOWED_EXPORT_ROLE_ID = 1409324452545822793

# Developer / Production Environment Mode
DEVELOPER_MODE = os.getenv('DEVELOPER_MODE', 'false').lower() == 'true'
PAUSEAI_SERVER_ID = 1100491867675709580

ONBOARDING_PIPELINE_CHANNEL_ID = 1174807044990193775
AIRTABLE_BASE_ID = "appWPTGqZmUcs3NWu"
AIRTABLE_TABLE_ID = "tblxeqggeTWU7Y8ME"
# Map country Role IDs to their respective coordinator email addresses
COUNTRY_ROLES = {
    1250075465008549938: "canada@pauseai.info", # Canada
    1188719399117541396: "germany@pause-ai.info", # Germany
}

intents = discord.Intents.default()
intents.members = True # Required for on_member_join!
intents.message_content = True # Required for commands

bot = commands.Bot(command_prefix='!', intents=intents)

async def handle_webhook(request):
    auth_header = request.headers.get("Authorization")
    if not WEBHOOK_SECRET:
        return web.json_response({"error": "Webhook secret not configured in the bot"}, status=500)
        
    if not auth_header or auth_header != f"Bearer {WEBHOOK_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)
        
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
            
        await member.add_roles(role)
        print(f"WEBHOOK: Added role '{role.name}' to {member.name}")
        return web.json_response({"success": True, "message": f"Added role {role.name} to {member.name}"})
        
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return web.json_response({"error": str(e)}, status=500)

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
    bot.loop.create_task(start_web_server())

bot.setup_hook = setup_hook


async def find_airtable_record(discord_id):
    if not AIRTABLE_PERSONAL_ACCESS_TOKEN:
        return None, None
        
    formula = f"{{id}}='{discord_id}'"
    encoded_formula = urllib.parse.quote(formula)
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?filterByFormula={encoded_formula}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_PERSONAL_ACCESS_TOKEN}"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    records = data.get("records", [])
                    if records:
                        return records[0].get("id"), records[0].get("fields", {})
    except Exception as e:
        print(f"Error checking Airtable for {discord_id}: {e}")
        
    return None, None


async def sync_member_to_airtable(member):
    if not AIRTABLE_PERSONAL_ACCESS_TOKEN:
        return

    record_id, fields = await find_airtable_record(str(member.id))
    
    joined_at_str = member.joined_at.isoformat() if member.joined_at else ""
    role_ids = [str(role.id) for role in member.roles if role.name != "@everyone"]
    role_names = [role.name for role in member.roles if role.name != "@everyone"]

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_PERSONAL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    if record_id:
        roles_to_assign = []
        saved_role_ids = fields.get("role_ids", "")
        saved_role_names = fields.get("role_names", "")
        
        if isinstance(saved_role_ids, str):
            saved_ids_list = [r.strip() for r in saved_role_ids.split(",") if r.strip()]
        else:
            saved_ids_list = saved_role_ids or []
            
        if isinstance(saved_role_names, str):
            saved_names_list = [r.strip() for r in saved_role_names.split(",") if r.strip()]
        else:
            saved_names_list = saved_role_names or []

        for r_id in saved_ids_list:
            if r_id.isdigit():
                role = member.guild.get_role(int(r_id))
                if role and role not in roles_to_assign:
                    roles_to_assign.append(role)
                    
        for r_name in saved_names_list:
            if not any(r.name == r_name for r in roles_to_assign):
                role = discord.utils.get(member.guild.roles, name=r_name)
                if role and role not in roles_to_assign:
                    roles_to_assign.append(role)
        
        roles_to_assign = [r for r in roles_to_assign if not r.is_default() and not r.is_bot_managed()]

        if roles_to_assign:
            try:
                await member.add_roles(*roles_to_assign)
                print(f"Restored {len(roles_to_assign)} roles for returning user {member.name}.")
                all_current_roles = list(set(member.roles) | set(roles_to_assign))
                role_ids = [str(r.id) for r in all_current_roles if r.name != "@everyone"]
                role_names = [r.name for r in all_current_roles if r.name != "@everyone"]
            except Exception as e:
                print(f"Error restoring roles for {member.name}: {e}")

        patch_url = f"{url}/{record_id}"
        payload = {
            "fields": {
                "username": member.name,
                "nick": member.nick or "",
                "global_name": getattr(member, 'global_name', member.name) or "",
                "joined_at": joined_at_str,
                "role_ids": role_ids,
                "role_names": role_names,
                "left_at": None
            },
            "typecast": True
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(patch_url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        print(f"Successfully updated returning user {member.name} in Airtable!")
                    else:
                        text = await response.text()
                        print(f"Error updating user {member.name}: {response.status} - {text}")
        except Exception as e:
            print(f"Exception updating user {member.name}: {e}")

    else:
        payload = {
            "records": [
                {
                    "fields": {
                        "id": str(member.id),
                        "username": member.name,
                        "nick": member.nick or "",
                        "global_name": getattr(member, 'global_name', member.name) or "",
                        "joined_at": joined_at_str,
                        "role_ids": role_ids,
                        "role_names": role_names,
                        "left_at": None
                    }
                }
            ],
            "typecast": True
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status in (200, 201):
                        print(f"Successfully posted new user {member.name} to Airtable!")
                    else:
                        text = await response.text()
                        print(f"Error posting user {member.name}: {response.status} - {text}")
        except Exception as e:
            print(f"Exception posting user {member.name}: {e}")

@bot.event
async def on_ready():
    print(f'Ready! Logged in as {bot.user}')
    print('Listening for users joining...')

@bot.event
async def on_member_join(member):
    # Environment Protection:
    # Dev mode: ignore the public server. Prod mode: ignore all test servers.
    if DEVELOPER_MODE and member.guild.id == PAUSEAI_SERVER_ID:
        print(f"DEV MODE: Ignored {member.name} joining public server.")
        return
    if not DEVELOPER_MODE and member.guild.id != PAUSEAI_SERVER_ID:
        print(f"PROD MODE: Ignored {member.name} joining test/dev server.")
        return

    print(f"User {member.name} joined! Waiting some minutes before checking role...")
    
    # Wait for some minutes
    await asyncio.sleep(180)
    
    # In case they were kicked/left or their roles changed, we should re-fetch the member object
    guild = member.guild
    member = guild.get_member(member.id)
    
    if not member:
        print(f"The user {member.name} left the server.")
        return
        
    await sync_member_to_airtable(member)
        
    matched_roles = [role for role in member.roles if role.id in COUNTRY_ROLES]
    
    if not matched_roles:
        return
        
    channel = bot.get_channel(int(ONBOARDING_PIPELINE_CHANNEL_ID)) if ONBOARDING_PIPELINE_CHANNEL_ID else None
    
    url = "https://api.mailersend.com/v1/email"
    headers = {
        "Authorization": f"Bearer {MAILERSEND_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Iterate over all matched country roles and send an email to each coordinator
    for role in matched_roles:
        target_email = COUNTRY_ROLES[role.id]
        print(f"User {member.name} has the {role.name} role! Sending email to {target_email}...")
        
        payload = {
            "from": {
                "email": "info@pauseai.info",
                "name": "PauseAI Info"
            },
            "to": [
                {
                    "email": target_email,
                    "name": f"{role.name} Onboarder"
                }
            ],
            "subject": f"{member.name} has joined PauseAI Discord",
            "text": f"A new user just joined the Discord server and received the {role.name} role!\n\nUser: {member.name}",
            "html": f"<strong>A new user just joined the Discord server and received the {role.name} role!</strong><br><br>User: {member.name}"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status in (200, 202):
                        print(f"Email sent successfully to {target_email}!")
                        if channel:
                            await channel.send(f"An email was sent successfully to `{target_email}` for the new member: **{member.name}** ({role.name})")
                    else:
                        response_text = await response.text()
                        print(f"Error from MailerSend for {target_email}: {response.status} - {response_text}")
                        if channel:
                            await channel.send(f"Failed to send email to `{target_email}` for **{member.name}**. API returned status: {response.status}")
        except Exception as e:
            print(f"Error sending email to {target_email}: {e}")
            if channel:
                await channel.send(f"Failed to send email to `{target_email}` for **{member.name}**. Please check the bot logs.")

@bot.event
async def on_member_remove(member):
    if DEVELOPER_MODE and member.guild.id == PAUSEAI_SERVER_ID:
        return
    if not DEVELOPER_MODE and member.guild.id != PAUSEAI_SERVER_ID:
        return
        
    print(f"User {member.name} left the server!")
    
    if not AIRTABLE_PERSONAL_ACCESS_TOKEN:
        return
        
    record_id, _ = await find_airtable_record(str(member.id))
    if record_id:
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record_id}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_PERSONAL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        
        left_at_str = datetime.datetime.utcnow().isoformat() + "Z"
        
        payload = {
            "fields": {
                "left_at": left_at_str
            },
            "typecast": True
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        print(f"Successfully marked {member.name} as left in Airtable!")
                    else:
                        text = await response.text()
                        print(f"Error marking {member.name} as left: {response.status} - {text}")
        except Exception as e:
            print(f"Exception marking {member.name} as left: {e}")

@bot.command(name='export_members')
async def export_members(ctx):
    if not ctx.guild:
        return # Ignore Direct Messages

    # Environment Protection:
    if DEVELOPER_MODE and ctx.guild.id == PAUSEAI_SERVER_ID:
        print(f"DEV MODE: Ignored command from {ctx.author.name} in public server.")
        return
    if not DEVELOPER_MODE and ctx.guild.id != PAUSEAI_SERVER_ID:
        print(f"PROD MODE: Ignored command from {ctx.author.name} in test/dev server.")
        return

    # Check if user is administrator or has the specific role
    is_admin = ctx.author.guild_permissions.administrator
    has_role = False
    
    if ALLOWED_EXPORT_ROLE_ID != 0:
        has_role = any(role.id == ALLOWED_EXPORT_ROLE_ID for role in ctx.author.roles)

    if not is_admin and not has_role:
        await ctx.send("❌ You must be an Administrator or have the required role to run this command.")
        return

    status_msg = await ctx.send("⏳ Generating CSV export of all members... (This might take a moment)")
    
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    
    # Write CSV Header
    writer.writerow(['id', 'username', 'nick', 'global_name', 'joined_at', 'role_ids', 'role_names'])
    
    # Write each member's info to the CSV
    for member in ctx.guild.members:
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
    
    await status_msg.delete()
    await ctx.send(content="✅ Here is the export of all members:", file=file)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not MAILERSEND_API_KEY:
        print("Error: DISCORD_TOKEN or MAILERSEND_API_KEY is not set in the environment.")
    else:
        if not AIRTABLE_PERSONAL_ACCESS_TOKEN:
            print("Warning: Airtable variables are not fully set. The bot will skip posting to Airtable.")
        if not ONBOARDING_PIPELINE_CHANNEL_ID:
            print("Warning: ONBOARDING_PIPELINE_CHANNEL_ID is not set. The bot will not send messages in Discord.")
        bot.run(DISCORD_TOKEN)
