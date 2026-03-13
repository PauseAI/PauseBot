import os
import discord
import aiohttp
import asyncio
import json
import csv
import io
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MAILERSEND_API_KEY = os.getenv('MAILERSEND_API_KEY')
AIRTABLE_PERSONAL_ACCESS_TOKEN = os.getenv('AIRTABLE_PERSONAL_ACCESS_TOKEN')

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
}

intents = discord.Intents.default()
intents.members = True # Required for on_member_join!
intents.message_content = True # Required for commands

bot = commands.Bot(command_prefix='!', intents=intents)

async def post_to_airtable(member):
    if not AIRTABLE_PERSONAL_ACCESS_TOKEN:
        return

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_PERSONAL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    joined_at_str = member.joined_at.isoformat() if member.joined_at else ""
    role_ids = [str(role.id) for role in member.roles if role.name != "@everyone"]
    role_names = [role.name for role in member.roles if role.name != "@everyone"]

    payload = {
        "records": [
            {
                "fields": {
                    "id": member.id,
                    "username": member.name,
                    "nick": member.nick or "",
                    "global_name": getattr(member, 'global_name', member.name) or "",
                    "joined_at": joined_at_str,
                    "role_ids": role_ids,
                    "role_names": role_names
                }
            }
        ],
        "typecast": True
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status not in (200, 201):
                    text = await response.text()
                    print(f"Error posting user {member.name} to Airtable: {response.status} - {text}")
                else:
                    print(f"Successfully posted {member.name} to Airtable!")
    except Exception as e:
        print(f"Exception posting user {member.name} to Airtable: {e}")

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
        
    await post_to_airtable(member)
        
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
            member.id,
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
