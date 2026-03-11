import os
import discord
import aiohttp
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MAILERSEND_API_KEY = os.getenv('MAILERSEND_API_KEY')
ONBOARDING_PIPELINE_CHANNEL_ID = 1174807044990193775
# Map country Role IDs to their respective coordinator email addresses
COUNTRY_ROLES = {
    1250075465008549938: "canada@pauseai.info", # Canada
}

intents = discord.Intents.default()
intents.members = True # Required for on_member_join!

bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f'Ready! Logged in as {bot.user}')
    print('Listening for users joining...')

@bot.event
async def on_member_join(member):
    print(f"User {member.name} joined! Waiting some minutes before checking role...")
    
    # Wait for some minutes
    await asyncio.sleep(180)
    
    # In case they were kicked/left or their roles changed, we should re-fetch the member object
    guild = member.guild
    member = guild.get_member(member.id)
    
    if not member:
        print(f"The user {member.name} left the server.")
        return
        
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

if __name__ == "__main__":
    if not DISCORD_TOKEN or not MAILERSEND_API_KEY:
        print("Error: DISCORD_TOKEN or MAILERSEND_API_KEY is not set in the environment.")
    else:
        if not ONBOARDING_PIPELINE_CHANNEL_ID:
            print("Warning: ONBOARDING_PIPELINE_CHANNEL_ID is not set. The bot will not send messages in Discord.")
        bot.run(DISCORD_TOKEN)
