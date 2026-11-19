"""Links Discord users to their member record in CiviCRM (the CRM at crm.pauseai.info).

CiviCRM matches the Discord username against the one each member gave when they joined, and from
then on grants the volunteer role itself through /webhook/add_role once the member is verified and
has signed the Volunteer Agreement. The bot's API key can make this call and nothing else.
"""
import json
import os
from urllib.parse import urlsplit
import aiohttp

LINK_TIMEOUT = aiohttp.ClientTimeout(total=30)


class LinkError(Exception):
    """CiviCRM didn't answer the call (a network error, a bad key, a server error)."""


# The most members CiviCRM's linkMany takes in one call
BATCH_SIZE = 500

# Only the production CRM sits behind Cloudflare Access; the service token goes nowhere else
CF_ACCESS_ORIGIN = ("https", "crm.pauseai.info")


def _cf_access_headers(url):
    """The Cloudflare Access service-token headers for a request to url: both when CF_ACCESS_CLIENT_ID
    and CF_ACCESS_CLIENT_SECRET are set and url is on the production CRM, otherwise none."""
    client_id = os.getenv("CF_ACCESS_CLIENT_ID")
    client_secret = os.getenv("CF_ACCESS_CLIENT_SECRET")
    parts = urlsplit(url)
    if not client_id or not client_secret or (parts.scheme, parts.netloc.lower()) != CF_ACCESS_ORIGIN:
        return {}
    return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret}


async def _call(session, base_url, api_key, action, params):
    """One API4 call on PauseaiDiscordMember; returns its rows."""
    url = f"{base_url.rstrip('/')}/civicrm/ajax/api4/PauseaiDiscordMember/{action}"
    headers = {"X-Civi-Auth": f"Bearer {api_key}", "X-Requested-With": "XMLHttpRequest",
               **_cf_access_headers(url)}
    try:
        # A followed redirect would carry the key and the service token to wherever it points
        async with session.post(url, headers=headers, data={"params": json.dumps(params)},
                                timeout=LINK_TIMEOUT, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise LinkError(f"HTTP {response.status}: CiviCRM answered a redirect, which is not followed;"
                                " check CIVICRM_URL")
            if response.status != 200:
                raise LinkError(f"HTTP {response.status}: {(await response.text())[:200]}")
            body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        # Not repr(e): a ClientResponseError's repr holds the request's headers
        raise LinkError(f"{type(e).__name__}: {e}") from e
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
