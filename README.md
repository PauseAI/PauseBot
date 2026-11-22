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
- **Log Channel** (replacing Quark's posts in #logs): With `MESSAGE_LOG_CHANNEL_ID` set, posts each message edit (author, channel or thread, old and new text, link) and deletion (author, channel or thread, the text as last logged, attachment names, when it was sent) to that channel, from what the message log holds. A message deleted before its log write landed (e.g. an instant auto-delete) is taken from the bot's message cache instead, and logged as deleted. Bulk deletions post every message it has and a count of the rest. It leaves out internal channels (see `MESSAGE_LOG_IGNORE_IDS`), bots' messages, events in the log channel itself, edits of messages the log doesn't have, and single deletions of messages neither the log nor the cache has (sent over 30 days ago, or while the bot was down). Quoted text sits in a code block, so it can't pass for the post's own lines; it's cut to fit Discord's embed limits, and posts never ping anyone. The bot records each post in the `log_posts` table (no message text), and each daily run deletes the posts whose oldest quoted message was sent over 30 days ago, so the channel keeps message text no longer than the database does. A post only bundles messages sent on the same day, so none goes more than a day early. In the log channel the bot needs View Channel, Send Messages, Embed Links and Read Message History.

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
   - `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`: the bot's Cloudflare Access service token, which the CRM API needs once it sits behind Cloudflare Access. With both set, every CiviCRM call sends them as `CF-Access-Client-Id` and `CF-Access-Client-Secret`, only when `CIVICRM_URL` is `https://crm.pauseai.info`; with either unset it sends neither. CiviCRM calls never follow a redirect: a 3xx fails the call.
   - `WEBHOOK_SECRET`: A secret string of your choosing. Required if using the webhooks for assigning roles.
   - `STATS_SECRET`: Another secret, different from `WEBHOOK_SECRET`, for the onboarding dashboard to read `/stats/discord`. Without it, that endpoint refuses every request.
   - `PORT`: (Optional) Custom port for the web server to run over (defaults to 8080).
   - `DATABASE_URL`: Postgres connection string. The bot creates its tables on startup, and won't start without the database.
   - `MESSAGE_LOG_CHANNEL_ID`: (Optional) The ID of the moderators' log channel (#logs) to post message edits and deletions to. Unset (or not a single ID), nothing is posted; the message log itself is kept either way. In developer mode, set it to a channel on the test server.
   - `MESSAGE_LOG_IGNORE_IDS`: (Optional) Category and channel IDs, in any mix, separated by commas or spaces, whose edits and deletions are never posted to the log channel: internal groups such as staff and national chapter leads, where nothing needs moderating. A channel or thread is left out if its own ID, its parent channel's ID or its category's ID is listed. List a category to cover all its channels, including ones added later; for a mixed category (such as TEAMS), list only the channels to leave out. If any entry isn't an ID (say a pasted `<#123>` or a stray character), the bot posts nothing to the log channel at all and prints a warning, so a typo never shows internal channels. With this set, a channel the bot can't look up posts nothing, so a lookup failure never shows internal messages. The message log in Postgres still stores these channels' messages. To copy an ID, turn on Developer Mode in Discord (User Settings, Advanced, Developer Mode), then right-click the category name and choose **Copy Category ID**, or right-click a channel or thread and choose **Copy Channel ID** (on a phone, long-press it instead).

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

### Railway services

PauseBot runs as two Railway services, next to the project's Postgres. Their deploy settings live only in the Railway dashboard: the repo has no `railway.toml`, `railway.json` or `.railway/`. This list is what a standby host needs to match, as read from the dashboard on 18 November 2026. Variables are listed by name only; never put their values here.

The bot and Postgres are in US West (California). A move to EU West is planned (ClickUp: "Move PauseBot and Action Hub to Railway EU West").

**Bot** (service `PauseBot`, the `main.py` process):

- **Source**: this repo, GitHub `PauseAI/PauseBot`, deployed from `main`. No watch paths.
- **Build**: Railpack 0.43 (the default builder). There's no Dockerfile for the bot. Python 3.13.18, Railpack's default 3.13; the repo doesn't pin it, but it needs 3.13 or later because `audioop-lts` only installs there. Install step: `python -m venv /app/.venv`, then `pip install -r requirements.txt`. No custom build command.
- **Start command**: `python main.py`, chosen by Railpack (no Custom Start Command).
- **Networking**: `aiohttp` on `0.0.0.0:$PORT`, serving `GET /` (status), `POST /webhook/add_role` and `GET /stats/discord`. A public Railway domain, routed to port 8080; private `pausebot.railway.internal`. No static outbound IPs.
- **Healthcheck**: none set. `GET /` answers 200 once the web server is up, even before the bot has logged in to Discord, and doesn't check the database, so it isn't a good readiness check.
- **Restart policy**: On Failure, up to 10 retries.
- **Region and replicas**: US West (California), 1 replica, not serverless. Run one replica only: two would both answer Discord events and both run the scheduled jobs. Teardown (deploy overlap and draining) is off.
- **Volumes**: none. The bot keeps nothing on disk; its data is in Postgres.
- **Scheduled work**: none on Railway (no cron). The daily server snapshot and prune is a `discord.ext.tasks` loop inside the bot (`daily_snapshot_and_prune`). It runs at startup and then every 24 hours, so its time of day is set by the last restart (around 18:48 UTC as of November 2026). The same loop deletes the bot's log channel posts whose oldest quoted message was sent over 30 days ago. The startup run skips a server already snapshotted in the last 20 hours, so a restart within 20 hours of the last snapshot can leave a gap of up to about 44 hours. The CiviCRM link sweep is another in-process loop, hourly.
- **Variables**: `DATABASE_URL` is a reference, `${{Postgres.DATABASE_URL}}`. Set as values: `DISCORD_TOKEN`, `MAILERSEND_API_KEY`, `PORT` (8080), `WEBHOOK_SECRET`, `STATS_SECRET`, `CIVICRM_URL` and `CIVICRM_API_KEY` (without either of the last two, the bot starts but quietly never links members to CiviCRM). `DEVELOPER_MODE` is not set, which means off; leave it unset in production.
- `MAILERSEND_API_KEY` has to be replaced before MailerSend closes on 11 December 2026 (infrastructure plan 4.3/4.6).

**Postgres**: US West (California), with the volume `postgres-volume`.

**Backup** (service `pausebot-backup`, the daily database dump):

- **Source**: the image `ghcr.io/pauseai/backup-image:1.0.0`, pinned (no auto-updates), not this repo. `Dockerfile.backup`, `jobs/backup.py` and the [Database backups](#database-backups) setup steps are the older build-from-repo path, which a separate change removes; the live service doesn't use them.
- **Start command**: none, so the image's default runs.
- **Cron schedule**: `0 3 * * *`, daily at 03:00 UTC.
- **Restart policy**: Never, so a failed run shows as failed instead of retrying. Teardown is off.
- **Healthcheck**: none.
- **Region**: EU West (Amsterdam).
- **Variables**: `BACKUP_NAME`, `DATABASE_URL`, `BACKUP_AGE_RECIPIENTS`, `BACKUP_B2_REGION`, `BACKUP_B2_BUCKET`, `BACKUP_B2_KEY_ID`, `BACKUP_B2_SECRET`, `BACKUP_HEALTHCHECK_URL`. They're documented with the image.

To capture the dashboard settings as a file later, run `railway config pull` with the Railway CLI. It writes the project's current settings to `.railway/railway.ts`, Railway's [Infrastructure as Code](https://docs.railway.com/infrastructure-as-code) format.

### Moving the member records off Airtable

`jobs/copy_airtable_members.py` copies Airtable's Discord table into the `discord_members` table, once. Stop the bot, run it with `DATABASE_URL` and `AIRTABLE_PERSONAL_ACCESS_TOKEN` set, then start the bot. Running it again overwrites each copied row with Airtable's.

### Database backups

Backups aren't run from this repo. PauseBot's Railway "backup" service runs PauseAI/infra's backup-image: each night it dumps this database, encrypts the dump with age and uploads it to Backblaze B2, where Object Lock keeps it for 30 days. How it's set up, its variables, and how to download, decrypt and restore a dump are in the [backup-image README](https://github.com/PauseAI/infra/blob/main/backup-image/README.md) (see [Restoring](https://github.com/PauseAI/infra/blob/main/backup-image/README.md#restoring)).

Dumps contain the 30-day message log, so a dump holds members' messages. Restore on a trusted machine, not in a checkout an AI agent can read, and delete the downloaded file and the age identity file afterwards.
