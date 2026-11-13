"""Copies Airtable's Discord table into discord_members, once, when the bot moves off Airtable.

Run by hand while the bot is stopped, from the repo root, with DATABASE_URL and
AIRTABLE_PERSONAL_ACCESS_TOKEN set: python jobs/copy_airtable_members.py
Applies the schema first. Safe to run again: each record overwrites the row with its id, and
civicrm_linked_at is left alone.
"""
import asyncio
import datetime
import os
import sys
from pathlib import Path
import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # So running this file directly can import db

import db  # noqa: E402

AIRTABLE_BASE_ID = "appWPTGqZmUcs3NWu"
AIRTABLE_TABLE_ID = "tblxeqggeTWU7Y8ME"


def _list(value):
    """A list from Airtable, which sends these fields as a list or as one comma-separated string."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value or [] if str(v).strip()]


def _time(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def from_airtable(fields):
    """The save fields of a record from Airtable's Discord table, or None if it has no Discord ID."""
    member_id = str(fields.get("id") or "").strip()
    if not member_id.isdigit():
        return None
    return {
        "id": int(member_id),
        "username": fields.get("username") or "",
        "nick": fields.get("nick") or None,
        "global_name": fields.get("global_name") or None,
        "joined_at": _time(fields.get("joined_at")),
        "left_at": _time(fields.get("left_at")),
        "role_ids": [int(r) for r in _list(fields.get("role_ids")) if r.isdigit()],
        "role_names": _list(fields.get("role_names")),
        "onboarding_notification": bool(fields.get("onboarding_notification")),
        "discord_reaction": bool(fields.get("discord_reaction")),
    }


async def airtable_records(session, token):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
    params = {}
    while True:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}, params=params) as response:
            if response.status != 200:
                raise RuntimeError(f"Airtable answered {response.status}: {await response.text()}")
            data = await response.json()
        for record in data.get("records", []):
            yield record.get("fields", {})
        if not data.get("offset"):
            return
        params = {"offset": data["offset"]}
        await asyncio.sleep(0.2)  # Airtable allows 5 requests a second


async def copy_row(pool, row):
    await pool.execute(
        """
        INSERT INTO discord_members (id, username, nick, global_name, joined_at, left_at, role_ids, role_names,
                                     onboarding_notification, discord_reaction)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (id) DO UPDATE SET
            username = EXCLUDED.username, nick = EXCLUDED.nick, global_name = EXCLUDED.global_name,
            joined_at = EXCLUDED.joined_at, left_at = EXCLUDED.left_at, role_ids = EXCLUDED.role_ids,
            role_names = EXCLUDED.role_names, onboarding_notification = EXCLUDED.onboarding_notification,
            discord_reaction = EXCLUDED.discord_reaction
        """,
        row["id"], row["username"], row["nick"], row["global_name"], row["joined_at"], row["left_at"],
        row["role_ids"], row["role_names"], row["onboarding_notification"], row["discord_reaction"],
    )


async def main():
    database_url = os.environ.get("DATABASE_URL")
    token = os.environ.get("AIRTABLE_PERSONAL_ACCESS_TOKEN")
    if not database_url or not token:
        sys.exit("Set DATABASE_URL and AIRTABLE_PERSONAL_ACCESS_TOKEN.")
    pool = await db.create_pool(database_url)
    try:
        await db.apply_schema(pool)
        copied = skipped = 0
        async with aiohttp.ClientSession() as session:
            async for fields in airtable_records(session, token):
                row = from_airtable(fields)
                if row is None:
                    skipped += 1
                    continue
                await copy_row(pool, row)
                copied += 1
        print(f"Copied {copied} record(s); skipped {skipped} without a Discord ID.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
