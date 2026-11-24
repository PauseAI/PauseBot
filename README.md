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
   - `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`: the bot's Cloudflare Access service token, which the CRM API needs once it sits behind Cloudflare Access. With both set, every CiviCRM call sends them as `CF-Access-Client-Id` and `CF-Access-Client-Secret`, only when `CIVICRM_URL` is `https://crm.pauseai.info`; with either unset it sends neither. CiviCRM calls never follow a redirect: a 3xx fails the call.
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
- **Scheduled work**: none on Railway (no cron). The daily server snapshot and prune is a `discord.ext.tasks` loop inside the bot (`daily_snapshot_and_prune`). It runs at startup and then every 24 hours, so its time of day is set by the last restart (around 18:48 UTC as of November 2026). The startup run skips a server already snapshotted in the last 20 hours, so a restart within 20 hours of the last snapshot can leave a gap of up to about 44 hours. The CiviCRM link sweep is another in-process loop, hourly.
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

### Country channels into regions (one-off, November 2026)

`jobs/country_regions.py` replaces the 🌍 COUNTRIES and 🌎 COUNTRIES+ categories with five regional ones (🌍 EUROPE, 🌎 AMERICAS, 🌍 AFRICA, 🌍 MIDDLE EAST, 🌏 ASIA-PACIFIC), once. Which region each country is in is the table in `jobs/country_regions_table.py`. What it does, in order:

1. Renames the role `Dominian Republic` to `Dominican Republic`.
2. Creates the regional categories with the old 🌍 COUNTRIES category's permission overwrites, except the `Paused (Wick)` deny. They go where the old categories are.
3. Moves every country channel and #european-union into its region, keeping their history and IDs, and syncs their permissions to the category. #france and #germany keep their own permissions. Creates a channel for every country role that has at least one member (or is held by a National Leader) and has no channel yet, plus `other-<region>` in each region. Sorts each region alphabetically, with `other-<region>` last.
4. Moves #request-a-country-🌍 to the end of 📚 SERVER GUIDE RESOURCES, posts a notice there that requests are no longer needed, then stops @everyone sending messages in it. It's not deleted.
5. Posts a welcome in each new country channel, pinging only that country's role.
6. Deletes the two old categories, only once they're empty. It deletes nothing else.

Onboarding is a separate step. Discord allows at most 4 questions with 50 answers each, so it replaces the country questions with three optional multi-select ones: "Where are you? Europe", "Where are you? Middle East & Asia-Pacific" and "Where are you? Americas & Africa" (`ONBOARDING_GROUPS` in the table). Each country option gives its role and its channel, with the country's own name as the description where the old option had none. Each region ends with "Somewhere else in <region>", which gives only `other-<region>`. A group with more countries than fit lists those with members first, then the most populous; the rest use "Somewhere else". To stay within 4 questions, merge one of the other questions into another with `--merge-question "<title>" --into "<title>"` (options that give nothing are dropped; rename one with `--rename-option "Yes=I want to volunteer"`). Default channels @everyone can't see are left out, as Discord refuses them. Discord also limits the answers across all questions (it accepted 105 and refused 162 on 24 November 2026), so on the live server add `--members-only`: only countries someone holds the role of are listed, and the rest use "Somewhere else". Rerun it as countries gain members. Every other question, the default channels and the onboarding settings are carried over unchanged, since Discord replaces the whole onboarding config at once.

Everything is read from the live server on each run, so after a partial failure, run it again: steps already done are skipped. The script needs only Python 3 (no packages) and reads the token from `DISCORD_TOKEN`, which it never prints.

**Before the run**

1. Read the last request in #request-a-country-🌍 (20 November). Once the run has made the person's country channel, point them to it.
2. In Server Settings > Roles, give the bot's role (PauseBot) **Manage Channels**, **Manage Server** and **Mention @everyone, @here and All Roles**. It already has **Manage Roles**. Mention @everyone is only so the welcome pings roles that aren't mentionable (on 22 November, only @Iran); without it, everything else still works.
3. From the repo root, set the token without it going into your shell history:

   ```bash
   read -rs DISCORD_TOKEN && export DISCORD_TOKEN
   ```

**Run**

1. Dry run. It reads the server, changes nothing and prints every change it would make, then a summary with any warnings. Check the new channels against the expected list (it says what differs):

   ```bash
   python3 jobs/country_regions.py
   ```

2. Make the changes. Before changing anything it saves the current roles, channels, permissions and onboarding config to `country_regions_backup.json`. Keep that file: it's the rollback, and a rerun reads it to tell which channels are new. A rerun never overwrites it; it saves its own copy beside it, with the time in the name.

   ```bash
   python3 jobs/country_regions.py --apply
   ```

   To give every country in the table its own channel now, not only those with members, rerun with `--all-countries` (dry run first, then add `--apply`). Countries nobody has picked yet get a channel but no welcome. Europe then has exactly 50 channels, Discord's limit for a category; the script stops rather than go over.

   ```bash
   python3 jobs/country_regions.py --all-countries
   python3 jobs/country_regions.py --all-countries --apply
   ```

3. Build the new onboarding config. This writes it to `onboarding_new.json` and lists what changes, without sending anything:

   ```bash
   python3 jobs/country_regions.py --onboarding --merge-question "Do you want to volunteer for Pause AI?" --into "What are you interested in?"
   ```

   If a country question also has an option giving a role that isn't a country in the table, it stops and names the option, so nothing is lost: move that option to another question by hand, or add the country to the table. Options that give no role (such as a "not listed? ask here" one) are dropped, and listed.

4. Read `onboarding_new.json`, then send it to Discord. It saves the current config to `country_regions_backup-onboarding-<time>.json` first:

   ```bash
   python3 jobs/country_regions.py --onboarding --apply-onboarding
   ```

   Discord's API docs don't give a limit on how many questions or options per question onboarding can have. The script stops itself over 15 questions or 50 options. If Discord's real limit is lower, Discord rejects the whole change and onboarding stays as it was.

**After the run**

1. Check the categories in Discord and the onboarding in Server Settings > Onboarding.
2. Take **Manage Channels**, **Manage Server** and **Mention @everyone** back off the bot's role. It keeps **Manage Roles**, which it had before.
3. Unset the token: `unset DISCORD_TOKEN`.
4. About two weeks after the run, delete #request-a-country-🌍 by hand.

**Rollback** (each is a dry run without `--apply`, and needs the same permissions as the run)

- Put the onboarding back as it was:

  ```bash
  python3 jobs/country_regions.py --restore-onboarding country_regions_backup.json --apply
  ```

- Put the old categories back and move each channel back into them with its old permissions (including unlocking #request-a-country-🌍):

  ```bash
  python3 jobs/country_regions.py --restore-layout country_regions_backup.json --apply
  ```

  This leaves the new channels and the regional categories in place, since the script never deletes channels. Delete them by hand if you want them gone. The role keeps its corrected name, and the categories' order may need dragging back.

If you get `CERTIFICATE_VERIFY_FAILED` on a Mac, your Python from python.org hasn't got its certificates yet: run `Install Certificates.command` from its folder in Applications.

### Database backups

Backups aren't run from this repo. PauseBot's Railway "backup" service runs PauseAI/infra's backup-image: each night it dumps this database, encrypts the dump with age and uploads it to Backblaze B2, where Object Lock keeps it for 30 days. How it's set up, its variables, and how to download, decrypt and restore a dump are in the [backup-image README](https://github.com/PauseAI/infra/blob/main/backup-image/README.md) (see [Restoring](https://github.com/PauseAI/infra/blob/main/backup-image/README.md#restoring)).

Dumps contain the 30-day message log, so a dump holds members' messages. Restore on a trusted machine, not in a checkout an AI agent can read, and delete the downloaded file and the age identity file afterwards.
