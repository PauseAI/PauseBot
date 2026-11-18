# PauseBot

PauseBot is a Discord bot designed to streamline the onboarding pipeline for PauseAI. It listens for new members joining the server and, once they are assigned specific country roles, automatically sends an email to the corresponding country coordinator using the MailerSend API. It also records each member in its Postgres database and links them to their member record in CiviCRM. It reports its email activities to a designated Discord channel.

## Features

- **Automatic Onboarding Emails**: Monitors new users and waits a few minutes. If a user is given a registered country role, an email is automatically sent to the country's coordinator.
- **Member Records**: Keeps each member's details (Discord ID, username, nick, global name, join and leave dates, and roles) in Postgres. When someone rejoins, the roles they had are given back (except any that `is_role_safe_to_grant` refuses).
- **CiviCRM Linking**: Asks CiviCRM to link each member to their member record by Discord username, on join, and in an hourly sweep (also run at startup) that sends everyone in the server not linked yet in batches of 500.
- **Webhook to Discord Syncing**: Exposes a secure REST endpoint (`/webhook/add_role`) that accepts POST requests authenticated with a `WEBHOOK_SECRET`. CiviCRM calls it to give the volunteer role to linked members once they're verified and have signed the Volunteer Agreement.
- **Dashboard Stats**: `GET /stats/discord`, authenticated with `Authorization: Bearer <STATS_SECRET>`, answers aggregate counts of the member records for the onboarding dashboard: joins and leaves per month, current members, onboarding pings and reactions, and current members per role ID. It never answers anything about one person.
- **Member Export**: Administrators and authorized users can use the `!export_members` command to download a CSV file containing all members and their roles.
- **Discord Notification Channel**: Logs successful and failed email attempts directly to a designated Discord channel (e.g., `#onboarding-pipeline`).
- **Role-based Logic**: Easily map new or existing Discord Role IDs to specific email addresses.
- **Server Snapshots** (replacing Xenon): Once a day, saves the server's roles, channels and permission overwrites, member roles and nicknames, and bans to Postgres. Kept for 30 days.
- **Message Log** (replacing Quark): Stores every server message, its edits and when it was deleted in Postgres. Kept for 30 days from when the message was sent.

## Prerequisites

- Python 3.8+
- A Discord Bot Token
- A MailerSend API Key
- A PostgreSQL database
- A CiviCRM API key for the bot's account (optional; without it, members aren't linked to CiviCRM)

## Setup

1. **Clone the repository:**

   ```bash
   git clone <your-repository-url>
   cd PauseBot
   ```

2. **Create a virtual environment (optional but recommended):**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables:**
   Copy the example environment file and fill in your details:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and configure the following variables:
   - `DISCORD_TOKEN`: Your Discord bot token.
   - `MAILERSEND_API_KEY`: Your MailerSend API Key.
   - `CIVICRM_URL`, `CIVICRM_API_KEY`: CiviCRM's address and the bot's API key, for linking members.
   - `WEBHOOK_SECRET`: A secret string of your choosing. Required if using the webhooks for assigning roles.
   - `STATS_SECRET`: Another secret, different from `WEBHOOK_SECRET`, for the onboarding dashboard to read `/stats/discord`. Without it, that endpoint refuses every request.
   - `PORT`: (Optional) Custom port for the web server to run over (defaults to 8080).
   - `DATABASE_URL`: Postgres connection string. The bot creates its tables on startup, and won't start without the database.

5. **Configure Roles and Channels (in `main.py`):**
   - Update `ONBOARDING_PIPELINE_CHANNEL_ID` with the channel ID where the bot should log its actions.
   - Update the `COUNTRY_ROLES` dictionary with the Discord Role IDs and their corresponding coordinator email addresses.

## Running the Bot

Run the bot directly via Python:

```bash
python main.py
```

## Running the Tests

The tests need a Postgres database whose name ends in `_test`; they drop and recreate its tables.

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/pausebot_test pytest
```

## Deployment

This bot is designed to be easily deployed to containerized environments or platforms like Railway or Heroku. Make sure to define your environment variables (`DISCORD_TOKEN`, `MAILERSEND_API_KEY`, etc.) in your deployment platform's dashboard.

The message log needs the **Message Content** privileged intent, enabled under Bot in the Discord Developer Portal.

### Moving the member records off Airtable

`jobs/copy_airtable_members.py` copies Airtable's Discord table into the `discord_members` table, once. Stop the bot, run it with `DATABASE_URL` and `AIRTABLE_PERSONAL_ACCESS_TOKEN` set, then start the bot. Running it again overwrites each copied row with Airtable's.

### Database backups

Backups aren't run from this repo. PauseBot's Railway "backup" service runs PauseAI/infra's backup-image: each night it dumps this database, encrypts the dump with age and uploads it to Backblaze B2, where Object Lock keeps it for 30 days. How it's set up, its variables, and how to download, decrypt and restore a dump are in the [backup-image README](https://github.com/PauseAI/infra/blob/main/backup-image/README.md) (see [Restoring](https://github.com/PauseAI/infra/blob/main/backup-image/README.md#restoring)).

Dumps contain the 30-day message log, so a dump holds members' messages. Restore on a trusted machine, not in a checkout an AI agent can read, and delete the downloaded file and the age identity file afterwards.
