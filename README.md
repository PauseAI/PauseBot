# PauseBot

PauseBot is a Discord bot designed to streamline the onboarding pipeline for PauseAI. It listens for new members joining the server and, once they are assigned specific country roles, automatically sends an email to the corresponding country coordinator using the MailerSend API. It also automatically records new joining users to an Airtable database. It reports its email activities to a designated Discord channel.

## Features

- **Automatic Onboarding Emails**: Monitors new users and waits a few minutes. If a user is given a registered country role, an email is automatically sent to the country's coordinator.
- **Airtable Integration**: Records new member details (Discord ID, username, nick, global name, join date, and role IDs) to an Airtable table.
- **Airtable/Webhook to Discord Syncing**: Exposes a secure REST endpoint (`/webhook/add_role`) natively powered by a web server that accepts POST requests securely authenticated with a `WEBHOOK_SECRET`. Allows Airtable automations to seamlessly assign Discord roles to members based on Airtable state updates.
- **Member Export**: Administrators and authorized users can use the `!export_members` command to download a CSV file containing all members and their roles.
- **Discord Notification Channel**: Logs successful and failed email attempts directly to a designated Discord channel (e.g., `#onboarding-pipeline`).
- **Role-based Logic**: Easily map new or existing Discord Role IDs to specific email addresses.
- **Server Snapshots** (replacing Xenon): Once a day, saves the server's roles, channels and permission overwrites, member roles and nicknames, and bans to Postgres. Kept for 30 days.
- **Message Log** (replacing Quark): Stores every server message, its edits and when it was deleted in Postgres. Kept for 30 days from when the message was sent.
- **Off-site Database Backups**: A separate daily cron job dumps the whole database and uploads it to buckets at one or more providers (Backblaze B2, Cloudflare R2, AWS S3), keeping 30 days of dumps in each.

## Prerequisites

- Python 3.8+
- A Discord Bot Token
- A MailerSend API Key
- An Airtable Personal Access Token (optional, if you want an Airtable log)
- A PostgreSQL database (optional, for server snapshots and the message log)

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
   - `AIRTABLE_PERSONAL_ACCESS_TOKEN`: The PAT from your Airtable account.
   - `WEBHOOK_SECRET`: A secret string of your choosing. Required if using the webhooks for assigning roles.
   - `PORT`: (Optional) Custom port for the web server to run over (defaults to 8080).
   - `DATABASE_URL`: (Optional) Postgres connection string for snapshots and the message log. The bot creates its tables on startup.

5. **Configure Roles and Channels (in `main.py`):**
   - Update `AIRTABLE_BASE_ID` with your target Airtable base's ID.
   - Update `AIRTABLE_TABLE_NAME` with the table name where members will be recorded.
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

### Database backups

`jobs/backup.py` runs as its own Railway cron service: create a service from this repo, set its config file path to `railway.backup.json` (builds `Dockerfile.backup`, runs daily at 03:00 UTC), and give it these variables:

- `DATABASE_URL`: the same database as the bot.
- Four variables for each provider you want a copy at. Use different providers rather than two buckets at one, so one provider's outage or account problem can't take out every copy.

| Provider | Variables | Where to find the location value |
|---|---|---|
| Backblaze B2 | `BACKUP_B2_REGION`, `BACKUP_B2_BUCKET`, `BACKUP_B2_KEY_ID`, `BACKUP_B2_SECRET` | The part of the bucket's S3 endpoint after `s3.`, e.g. `eu-central-003` |
| Cloudflare R2 | `BACKUP_R2_ACCOUNT_ID`, `BACKUP_R2_BUCKET`, `BACKUP_R2_KEY_ID`, `BACKUP_R2_SECRET` | The account ID on the R2 overview page |
| AWS S3 | `BACKUP_S3_REGION`, `BACKUP_S3_BUCKET`, `BACKUP_S3_KEY_ID`, `BACKUP_S3_SECRET` | The bucket's region, e.g. `eu-west-2` |

Each key needs read, write, list and delete access to its bucket. On B2, use an application key, not the master key, since only application keys work with the S3 API.

- `RAILWAY_VOLUME_MOUNT_PATH` is set by Railway if you attach a volume, which keeps the last 7 days of dumps locally across redeploys.

With no provider configured, the job keeps the local dump only. A provider with only some of its variables set fails the run before dumping, naming what's missing. One provider failing doesn't stop the others, but fails the cron run so Railway shows it.

Dumps are uploaded to each bucket as `backups/pausebot-<timestamp>.dump`. After a successful upload, dumps older than 30 days are deleted from that bucket; nothing else in the bucket is touched. To restore one into a database, download it from any of the buckets and run:

```bash
pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$DATABASE_URL" pausebot-<timestamp>.dump
```
