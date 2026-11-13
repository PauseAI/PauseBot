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


async def link(session, base_url, api_key, discord_user_id, username):
    """Asks CiviCRM to link this Discord user. Returns CiviCRM's answer: {"status": "ok", ...}, or
    {"status": "refused", "reason": ...} when no single member's Discord username matches (yet)."""
    url = f"{base_url.rstrip('/')}/civicrm/ajax/api4/PauseaiDiscordMember/link"
    headers = {"X-Civi-Auth": f"Bearer {api_key}", "X-Requested-With": "XMLHttpRequest"}
    # Snowflakes go as strings: as JSON numbers they'd lose precision in PHP
    params = {"discord_user_id": str(discord_user_id), "username": username}
    try:
        async with session.post(url, headers=headers, data={"params": json.dumps(params)},
                                timeout=LINK_TIMEOUT) as response:
            if response.status != 200:
                raise LinkError(f"HTTP {response.status}")
            body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise LinkError(repr(e)) from e
    values = body.get("values") if isinstance(body, dict) else None
    if not values or not isinstance(values[0], dict) or "status" not in values[0]:
        raise LinkError(f"Unexpected answer: {body!r}")
    return values[0]
