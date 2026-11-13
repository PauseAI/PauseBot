import datetime
from types import SimpleNamespace as NS
import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
import civicrm
import members


def guild(*roles):
    return NS(roles=list(roles), get_role=lambda role_id: next((r for r in roles if r.id == role_id), None))


UK = NS(id=10, name="UK")
VOLUNTEER = NS(id=11, name="Wants to volunteer")


# ── Role restore ──────────────────────────────────────────────────────────────

def test_restores_by_id_then_by_name_for_a_recreated_role():
    recreated = NS(id=99, name="Wants to volunteer")
    assert members.roles_to_restore(guild(UK, recreated), [10, 11], ["UK", "Wants to volunteer"]) == [UK, recreated]


def test_skips_roles_that_no_longer_exist():
    assert members.roles_to_restore(guild(UK), [10, 12], ["UK", "Gone"]) == [UK]


def test_a_role_matched_by_id_is_not_added_twice_by_name():
    assert members.roles_to_restore(guild(UK, VOLUNTEER), [10, 11], ["UK", "Wants to volunteer"]) == [UK, VOLUNTEER]


# ── Copying from Airtable ─────────────────────────────────────────────────────

def test_from_airtable_reads_lists():
    row = members.from_airtable({
        "id": "1240361207010164817", "username": "ann", "joined_at": "2024-05-01T10:00:00.000Z",
        "role_ids": ["10", "11"], "role_names": ["UK", "Wants to volunteer"], "onboarding_notification": True,
    })
    assert row["id"] == 1240361207010164817
    assert row["joined_at"] == datetime.datetime(2024, 5, 1, 10, tzinfo=datetime.UTC)
    assert row["left_at"] is None
    assert row["role_ids"] == [10, 11] and row["role_names"] == ["UK", "Wants to volunteer"]
    assert row["onboarding_notification"] is True and row["discord_reaction"] is False
    assert row["nick"] is None


def test_from_airtable_reads_comma_separated_strings():
    row = members.from_airtable({"id": "1", "username": "ann", "role_ids": "10, 11", "role_names": "UK, Volunteer"})
    assert row["role_ids"] == [10, 11] and row["role_names"] == ["UK", "Volunteer"]


def test_from_airtable_skips_records_without_an_id():
    assert members.from_airtable({"username": "ann"}) is None
    assert members.from_airtable({"id": "abc"}) is None


# ── CiviCRM link ──────────────────────────────────────────────────────────────

async def civicrm_server(handler):
    app = web.Application()
    app.router.add_post("/civicrm/ajax/api4/PauseaiDiscordMember/link", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_link_sends_the_key_and_the_id_as_a_string():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("X-Civi-Auth")
        seen["params"] = (await request.post())["params"]
        return web.json_response({"values": [{"status": "ok", "contact_id": 5}]})

    server = await civicrm_server(handler)
    try:
        async with aiohttp.ClientSession() as session:
            answer = await civicrm.link(session, str(server.make_url("/")), "key", 1240361207010164817, "ann")
    finally:
        await server.close()
    assert answer == {"status": "ok", "contact_id": 5}
    assert seen["auth"] == "Bearer key"
    assert seen["params"] == '{"discord_user_id": "1240361207010164817", "username": "ann"}'


async def test_link_returns_a_refusal():
    async def handler(request):
        return web.json_response({"values": [{"status": "refused", "reason": "no_match"}]})

    server = await civicrm_server(handler)
    try:
        async with aiohttp.ClientSession() as session:
            answer = await civicrm.link(session, str(server.make_url("/")), "key", 1, "ann")
    finally:
        await server.close()
    assert answer == {"status": "refused", "reason": "no_match"}


@pytest.mark.parametrize("response", [
    web.json_response({"error_message": "Authorization failed"}, status=401),
    web.json_response({"values": []}),
    web.Response(text="<html>", content_type="text/html"),
])
async def test_link_raises_when_civicrm_does_not_answer(response):
    async def handler(request):
        return response

    server = await civicrm_server(handler)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(civicrm.LinkError):
                await civicrm.link(session, str(server.make_url("/")), "key", 1, "ann")
    finally:
        await server.close()
