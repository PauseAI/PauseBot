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
- **Off-site Database Backups**: A separate daily cron job dumps the whole database and uploads it to buckets at one or more providers (Backblaze B2, Cloudflare R2, AWS S3), keeping 30 days of dumps in each.

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

`jobs/backup.py` runs as its own Railway cron service, separate from the bot. The bot service stays on Railpack with no config. Railway has deprecated config files, so the backup service is set up by hand. Create a service from this repo and set these in its Settings:

- **Builder**: Dockerfile, with the path `Dockerfile.backup`.
- **Deploy > Cron Schedule**: `0 3 * * *` (daily at 03:00 UTC).
- **Restart policy**: Never, so a failed run shows as failed instead of retrying.
- **Watch Paths** (optional): `/jobs/**` and `/Dockerfile.backup`, so commits that only touch the bot don't rebuild it.

Then give the backup service these variables. The `BACKUP_*` variables go on the backup service only, never on the bot service.

- `DATABASE_URL`: the same database as the bot.
- `RAILWAY_VOLUME_MOUNT_PATH` is set by Railway if you attach a volume, which keeps the last 7 days of dumps locally across redeploys.
- Four variables for each provider you want a copy at, from the table below. Use different providers rather than two buckets at one, so one provider's outage or account problem can't take out every copy.

| Provider | Variables | The location value |
|---|---|---|
| Backblaze B2 | `BACKUP_B2_REGION`, `BACKUP_B2_BUCKET`, `BACKUP_B2_KEY_ID`, `BACKUP_B2_SECRET` | The part of the bucket's S3 endpoint after `s3.`, e.g. `eu-central-003` |
| Cloudflare R2 | `BACKUP_R2_ACCOUNT_ID`, `BACKUP_R2_BUCKET`, `BACKUP_R2_KEY_ID`, `BACKUP_R2_SECRET` | The 32-character account ID on the R2 overview page; add `.eu` (or `.fedramp`) for a bucket created in that jurisdiction |
| AWS S3 | `BACKUP_S3_REGION`, `BACKUP_S3_BUCKET`, `BACKUP_S3_KEY_ID`, `BACKUP_S3_SECRET` | The bucket's region, e.g. `eu-west-2` |

Each key needs read, write, list and delete access to its bucket, including deleting old file versions (B2 buckets keep every version by default, and cleanup removes each one). On B2, use an application key, not the master key, since only application keys work with the S3 API. On AWS, the key's policy needs `s3:PutObject`, `s3:ListBucket`, `s3:ListBucketVersions`, `s3:DeleteObject` and `s3:DeleteObjectVersion` on the bucket.

With no provider configured, the job keeps the local dump only. A provider with only some of its variables set, or a location value in the wrong form (such as a full URL), is reported and fails the cron run, but the dump and the uploads to the correctly configured providers still go ahead. Uploads to the providers run at the same time; one failing doesn't stop the others. A failed upload, or a failed cleanup of old dumps, fails the cron run so Railway shows it.

Dumps are uploaded to each bucket as `backups/pausebot-<timestamp>.dump`. After a successful upload, dumps older than 30 days are deleted from that bucket; nothing else in the bucket is touched. To restore one into a database, download it from any of the buckets (see [Downloading the latest dump](#downloading-the-latest-dump)) and run:

```bash
pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$DATABASE_URL" pausebot-<timestamp>.dump
```

#### Downloading the latest dump

`jobs/fetch_backup.py` downloads the newest dump from a bucket to your machine. It only lists and reads files under `backups/`; it never uploads or deletes anything. It prints the provider, bucket, file name, size, last modified time and local path, and never prints the key.

**1. Create a separate read-only key.** Don't copy the cron job's key out of Railway: it can write and delete, and it should stay there. In Backblaze, go to Application Keys and add a new key with:

- **Allow access to Bucket(s)**: `pauseai-pausebot-backups`
- **Type of Access**: Read Only
- **File name prefix**: `backups/`

Backblaze shows the secret only once. For a read-only key on AWS, the policy needs `s3:ListBucket`, `s3:ListBucketVersions` and `s3:GetObject`.

**2. Put it in `.env`** (which is gitignored), yourself, in an editor:

```bash
BACKUP_B2_REGION=eu-central-003
BACKUP_B2_BUCKET=pauseai-pausebot-backups
BACKUP_B2_KEY_ID=<keyID>
BACKUP_B2_SECRET=<applicationKey>
```

Anything that can read `.env` can read the key, including an AI agent working in the same checkout. To keep the key from an agent, run this from a checkout the agent can't reach.

**3. Run it** from the repo root:

```bash
pip install -r requirements.txt -r jobs/requirements.txt
python jobs/fetch_backup.py
```

The dump lands in `backups/` at the repo root, which is gitignored. Use `--out DIR` to put it somewhere else. If a file with the same name and size is already there, it isn't downloaded again.

If several providers are set up in `.env`, it uses the first one that works, in the order B2, R2, S3. Use `--provider b2`, `r2` or `s3` to choose one. It doesn't compare providers, so it warns you if the dump it found is more than 2 days old, which means recent backups to that bucket may have failed. It exits with a non-zero status and says why if variables are missing, the bucket has no dumps, or the provider rejects the key.

**Delete the dump when you're done.** Dumps contain the 30-day message log, so a dump holds members' messages.
