# PauseBot cold standby on Render

PauseBot runs on Railway. If Railway is down for long enough that PauseBot's absence matters, it can be brought up on Render, in Frankfurt, from `render.yaml` in this repo and the newest nightly database backup. Nothing on Render exists between rehearsals: it's a cold standby, created when needed and deleted afterwards. This is infrastructure plan 2.1.

- What `render.yaml` creates: the bot as a Render web service (`pausebot`, paid `starter` plan, health check `/`) and a managed Postgres 18 (`pausebot-db`, paid `basic-256mb`), both in Frankfurt. Why those plans, and the disk, are in the comments at the top of the file.
- Where the data comes from: the nightly B2 dump made by PauseBot's Railway `pausebot-backup` service at 03:00 UTC. How to download, decrypt and restore one is in the [backup-image README, Restoring](https://github.com/PauseAI/infra/blob/main/backup-image/README.md#restoring).
- What's lost in a real failover: everything written since the last nightly backup, up to about a day. That's member records changed or added (joins, leaves, role changes), the message log and its edits, and server snapshots. Members who joined in that time are still in Discord; `/sync_recent` brings their records back.
- Where the secrets are: Psono. Never commit a value, or paste one into a ticket or chat.

**Never run two bots on the same token.** Both would answer every Discord event (double onboarding emails, double role changes) and both would run the daily jobs. A rehearsal uses no token or a dev bot's token, never production's. A real failover starts the Render bot only once the Railway one is down for good, or its token has been reset.

## Before you start

You need:

- A Render account in PauseAI's workspace, allowed to create Blueprints.
- Access to the B2 backups and the age identity that decrypts them (see the backup-image README).
- `psql` and `pg_restore` 18 on a trusted machine. Dumps hold the 30-day message log, so members' messages: restore from a trusted machine, not a checkout an AI agent can read, and delete the dump and the age identity file afterwards.
- For a real failover: the production values from Psono, and Jakub to change the webhook URL in CiviCRM.

## Rehearsal

The rehearsal proves the standby works: that the Blueprint builds and starts, and that the newest backup restores into it complete. It runs no production bot. Keep the time each step takes in the table at the end, and add the totals to the log at the bottom: they're how long a real failover takes.

1. **Create from the Blueprint.** In the Render Dashboard: New > Blueprint, pick `PauseAI/PauseBot`, branch `main`. To save money on a same-day rehearsal you can change the database `plan` to `free` (and remove its `diskSizeGB`) in a branch first and create from that branch. Render asks for each `sync: false` variable. Fill in:

   | Variable | Rehearsal value |
   |---|---|
   | `DISCORD_TOKEN` | empty for now (step 4 says what to set) |
   | `MAILERSEND_API_KEY` | `rehearsal` (the bot won't start without some value; emails then fail, which is what we want) |
   | `WEBHOOK_SECRET`, `STATS_SECRET` | a random value each, not production's: `openssl rand -hex 32` |
   | `DEVELOPER_MODE` | `true` |
   | everything else | empty |

   With no token, `main.py` prints `Error: DISCORD_TOKEN, ... is not set`, exits without opening a port, and Render restarts it until the deploy fails its port or health check. That's expected, and it means the bot creates no tables before the restore.

2. **Restore the newest dump.**
   1. In the Dashboard, open `pausebot-db` > Networking and add your own IP to the allow list. Copy the **External Database URL** from Connect and keep it in a variable for the next steps (it holds the password, so start the line with a space to keep it out of shell history, if your shell is set up for that): `export RENDER_EXTERNAL_URL='<External Database URL>'`.
   2. Download and decrypt the newest PauseBot dump from B2, as in [Restoring](https://github.com/PauseAI/infra/blob/main/backup-image/README.md#restoring), and restore it into that URL with the command given there. Render's database user isn't Railway's, so restore with `--no-owner --no-acl` if it's a `pg_restore` dump. If it's a plain SQL dump, restore with `psql -v ON_ERROR_STOP=1`, after removing its `OWNER TO` and `GRANT` lines, so a real error stops the restore instead of scrolling past among ownership errors.
   3. Remove your IP from the allow list again.

3. **Check the row counts.** Count each table in the restored database:

   ```bash
   psql "$RENDER_EXTERNAL_URL" -c "
     SELECT 'discord_members' AS t, count(*) FROM discord_members
     UNION ALL SELECT 'message_log', count(*) FROM message_log
     UNION ALL SELECT 'message_edits', count(*) FROM message_edits
     UNION ALL SELECT 'server_snapshots', count(*) FROM server_snapshots
     UNION ALL SELECT 'log_posts', count(*) FROM log_posts;"
   ```

   Run the same query against Railway's Postgres if you can reach it (through `railway connect Postgres` with the Railway CLI), or against a local restore of the same dump. They should match the dump exactly; against live Railway, expect only the small difference since 03:00 UTC (`discord_members` a few rows at most, `message_log` up to a day of messages). A table that's empty or far short means the restore failed: read its output for errors.

4. **Start the bot, without production's token.** Choose one:
   - **With a dev bot (recommended).** Use the token of a separate dev application from the Discord developer portal, never production's. Set it as `DISCORD_TOKEN` (service > Environment) and save, which redeploys. With `DEVELOPER_MODE=true` it acts only on test servers, never on the PauseAI server, and the CiviCRM webhook is switched off. This proves the build, the start and the database connection.
   - **Without a token.** Skip this step. You've proved the build and the restore, not that the bot starts on Render.

5. **Check the logs, then the health page.** The logs should show `Web server started on port 8080`, then `Ready! Logged in as <the dev bot>`, and no database errors or restarts. Check the logs first: the web server starts a moment before the database connection, so `/` can answer once even when the database then fails and the bot exits. Then open the service's `https://pausebot-….onrender.com/`. It should answer `{"status": "ok", "bot_user": "<the dev bot's name>"}`, and still answer a few minutes later.

6. **Tear down.** In the Dashboard, delete the `pausebot` service, then `pausebot-db` (Settings > Delete), then the Blueprint itself. Delete the downloaded dump, the decrypted file and the age identity file from your machine. If you used a dev token, reset it in the developer portal, since it was pasted into Render.

**Timings** (copy into the log below):

| Step | Minutes |
|---|---|
| 1. Create from Blueprint (to first failed deploy) | |
| 2. Restore the newest dump | |
| 3. Check the row counts | |
| 4–5. Start with a dev token, health page OK | |
| 6. Tear down | |
| Total | |

## Real failover

Only when Railway is down, or PauseBot on Railway can't run, and it isn't coming back soon. Steps 1 and 2 are the rehearsal's, with these differences.

1. **Create from the Blueprint** as in the rehearsal's step 1, with the paid plans as the file has them. Leave `DISCORD_TOKEN` empty for now. Fill in the rest from Psono, with the values Railway's `PauseBot` service had: `MAILERSEND_API_KEY` (or whatever replaced it after MailerSend closes on 11 December 2026), `WEBHOOK_SECRET` and `STATS_SECRET` (the same values, so CiviCRM and the onboarding dashboard need only the new URL), `CIVICRM_URL`, `CIVICRM_API_KEY`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `MESSAGE_LOG_CHANNEL_ID`, `MESSAGE_LOG_IGNORE_IDS`. Leave `DEVELOPER_MODE` empty. Set every one Railway has: without `CF_ACCESS_*` the CRM calls are refused and members never link, and with `MESSAGE_LOG_CHANNEL_ID` but no `MESSAGE_LOG_IGNORE_IDS`, edits in staff channels get posted to #logs.

2. **Restore the newest dump** as in the rehearsal's step 2, and **check the row counts** (step 3): `discord_members` should be close to the PauseAI server's member count (Server Settings > Members), and the newest `server_snapshots` row should be from the day or two before the dump. Note the dump's time: everything after it is lost.

3. **Make sure the Railway bot can't run.** Either:
   - Confirm it's down: the bot shows as offline in the PauseAI server's member list, Railway's `PauseBot` service is stopped, crashed or unreachable, and its health page doesn't answer. If Railway might come back on its own, don't rely on this: use the next option.
   - Or, safer, **reset the token**: Discord developer portal > PauseBot's application > Bot > Reset Token. The old token stops working, so the Railway bot can't log in again even if Railway recovers. Save the new token in Psono straight away, replacing the old one. Don't put it in Railway's `DISCORD_TOKEN`: Railway redeploys a service when its variables change, so the Railway bot would log in next to the Render one. Railway gets the new token only when moving back (below).

4. **Set the token and start.** Set `DISCORD_TOKEN` on the `pausebot` service (Environment) and save, which deploys.

5. **Check the logs, then the health page**, as in the rehearsal's step 5. `https://pausebot-….onrender.com/` should answer `{"status": "ok", "bot_user": "PauseBot#…"}`. In Discord, the bot should be online, and a slash command such as `/country` should answer.

6. **Point CiviCRM at the new host.** Jakub changes the PauseBot webhook URL in CiviCRM from the Railway domain to `https://pausebot-….onrender.com/webhook/add_role`; the secret stays the same. Until then, verified volunteers don't get the volunteer role (CiviCRM's calls go to the dead Railway host). Whoever runs the onboarding dashboard changes its `/stats/discord` URL the same way.

7. **Catch up.** Run `/sync_recent minutes:<minutes since the dump>` so members who joined or rejoined since the backup get their records and saved roles. The hourly CiviCRM sweep relinks members on its own. Nothing recovers the rest: members who left since the dump still look present, and role changes since the dump are lost from the records, so a member who dropped a role in that time and later rejoins gets it back. Tell the moderators the time of the dump so they can check anyone who complains.

8. **Back up the new database.** The nightly backup runs on Railway and won't reach Render. If Render runs for more than a day, set up the backup-image against `pausebot-db` (see the backup-image README), or take manual dumps.

**Moving back to Railway** is the same in reverse:

1. Suspend the Render service, and check the bot shows offline in Discord.
2. Dump Render's database. Restore it into an empty database on Railway: Railway's Postgres still holds the data from before the outage, so either drop and recreate the `public` schema first, or restore with `pg_restore --clean --if-exists`. Check the row counts against Render's, as in the rehearsal's step 3.
3. Set the current token from Psono in Railway's `DISCORD_TOKEN` (if it was reset) and start the Railway bot. Check its logs and health page.
4. Point CiviCRM and the onboarding dashboard back at Railway.
5. Delete the Render resources once Railway has run for a day.

## Rehearsal log

| Date | Who | Dump restored | Total minutes | Notes |
|---|---|---|---|---|
