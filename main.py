import os
import discord
import aiohttp
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MAILERSEND_API_KEY = os.getenv('MAILERSEND_API_KEY')

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Ready! Logged in as {bot.user}')
    print('You can test the bot by sending "!sendemail <your message>" in a channel the bot has access to.')

@bot.command()
async def sendemail(ctx, *, custom_message: str = None):
    content = custom_message if custom_message else "This is a default message sent from the Discord bot."
    
    url = "https://api.mailersend.com/v1/email"
    headers = {
        "Authorization": f"Bearer {MAILERSEND_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": {
            "email": "info@pauseai.info",
            "name": "PauseAI Info"
        },
        "to": [
            {
                "email": "patricio@pauseai.info",
                "name": "Patricio"
            }
        ],
        "subject": "Message from PauseAI Discord Bot",
        "text": f"Message from Discord:\n\n{content}",
        "html": f"<strong>Message from Discord:</strong><br><br>{content}"
    }

    try:
        await ctx.send('Sending email...')
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status in (200, 202):
                    await ctx.send('Email sent successfully via MailerSend!')
                else:
                    response_text = await response.text()
                    print(f"Error from MailerSend: {response.status} - {response_text}")
                    await ctx.send(f'Failed to send the email. API returned status: {response.status}')
    except Exception as e:
        print(f'Error sending email: {e}')
        await ctx.send('Failed to send the email. Please check the bot logs for more details.')

if __name__ == "__main__":
    if not DISCORD_TOKEN or not MAILERSEND_API_KEY:
        print("Error: DISCORD_TOKEN or MAILERSEND_API_KEY is not set in the environment.")
    else:
        bot.run(DISCORD_TOKEN)
