"""Links Discord users to their member record in CiviCRM (the CRM at crm.pauseai.info).

CiviCRM matches the Discord username against the one each member gave when they joined, and from
then on grants the volunteer role itself through /webhook/add_role once the member is verified and
has signed the Volunteer Agreement. The bot's API key can make this call and nothing else.
"""
import json
import aiohttp

LINK_TIMEOUT = aiohttp.ClientTimeout(total=30)


class LinkError(Exception):
    """CiviCRM didn't answer the call (a network error, a bad key, a server error)."""


# The most members CiviCRM's linkMany takes in one call
BATCH_SIZE = 500


async def _call(session, base_url, api_key, action, params):
    """One API4 call on PauseaiDiscordMember; returns its rows."""
    url = f"{base_url.rstrip('/')}/civicrm/ajax/api4/PauseaiDiscordMember/{action}"
    headers = {"X-Civi-Auth": f"Bearer {api_key}", "X-Requested-With": "XMLHttpRequest"}
    try:
        async with session.post(url, headers=headers, data={"params": json.dumps(params)},
                                timeout=LINK_TIMEOUT) as response:
            if response.status != 200:
                raise LinkError(f"HTTP {response.status}: {(await response.text())[:200]}")
            body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise LinkError(repr(e)) from e
    values = body.get("values") if isinstance(body, dict) else None
    if not isinstance(values, list) or not all(isinstance(v, dict) and "status" in v for v in values):
        raise LinkError(f"Unexpected answer: {body!r}")
    return values


async def link(session, base_url, api_key, discord_user_id, username):
    """Asks CiviCRM to link this Discord user. Returns CiviCRM's answer: {"status": "ok", ...}, or
    {"status": "refused", "reason": ...} when no single member's Discord username matches (yet)."""
    # Snowflakes go as strings: as JSON numbers they'd lose precision in PHP
    values = await _call(session, base_url, api_key, "link",
                         {"discord_user_id": str(discord_user_id), "username": username})
    if len(values) != 1:
        raise LinkError(f"Unexpected answer: {values!r}")
    return values[0]


async def link_many(session, base_url, api_key, members):
    """Asks CiviCRM to link up to BATCH_SIZE (discord_user_id, username) pairs at once, judged in order as
    separate link calls would be. Returns one {"discord_user_id", "status", "reason"?} row per pair."""
    pairs = [{"discord_user_id": str(member_id), "username": username} for member_id, username in members]
    values = await _call(session, base_url, api_key, "linkMany", {"members": pairs})
    if len(values) != len(pairs):
        raise LinkError(f"Asked about {len(pairs)} members, got {len(values)} answers")
    return values
