import json
import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL
import civicrm

PROD = "https://crm.pauseai.info"
KEY = "test-key-not-real"
CLIENT_ID = "test-client-id.access"
CLIENT_SECRET = "test-client-secret-not-real"


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return json.dumps(self._body)

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records each post and answers one ok row."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.status, {"values": [{"status": "ok"}]})


def cf_header_names(session):
    return [name for name in session.calls[0][1]["headers"] if name.startswith("CF-")]


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", CLIENT_SECRET)


async def test_sends_the_service_token_to_the_production_crm_with_redirects_off(token):
    session = FakeSession()
    await civicrm.link(session, PROD, KEY, 1, "ada")
    url, kwargs = session.calls[0]
    assert url == f"{PROD}/civicrm/ajax/api4/PauseaiDiscordMember/link"
    assert kwargs["headers"]["X-Civi-Auth"] == f"Bearer {KEY}"
    assert kwargs["headers"]["CF-Access-Client-Id"] == CLIENT_ID
    assert kwargs["headers"]["CF-Access-Client-Secret"] == CLIENT_SECRET
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("unset", ["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"])
async def test_sends_neither_header_when_either_is_unset(token, monkeypatch, unset):
    monkeypatch.delenv(unset)
    session = FakeSession()
    await civicrm.link_many(session, PROD, KEY, [(1, "ada")])
    assert cf_header_names(session) == []


@pytest.mark.parametrize("base_url", [
    "https://crm.example.org",
    "http://crm.pauseai.info",
    "https://crm.pauseai.info:8443",
    "https://crm.pauseai.info.example.org",
])
async def test_sends_neither_header_to_another_origin(token, base_url):
    session = FakeSession()
    await civicrm.link(session, base_url, KEY, 1, "ada")
    assert cf_header_names(session) == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_is_an_error_and_is_not_followed(status):
    followed = []

    async def redirect(request):
        return web.Response(status=status, headers={"Location": "/elsewhere"})

    async def elsewhere(request):
        followed.append(dict(request.headers))
        return web.json_response({"values": [{"status": "ok"}]})

    app = web.Application()
    app.router.add_post("/civicrm/ajax/api4/PauseaiDiscordMember/link", redirect)
    app.router.add_route("*", "/elsewhere", elsewhere)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        with pytest.raises(civicrm.LinkError, match=f"HTTP {status}: CiviCRM answered a redirect") as error:
            await civicrm.link(session, str(server.make_url("")), KEY, 1, "ada")
    assert followed == []
    assert KEY not in str(error.value)


async def test_a_client_error_reports_no_header(token):
    headers = CIMultiDictProxy(CIMultiDict({"X-Civi-Auth": f"Bearer {KEY}", "CF-Access-Client-Secret": CLIENT_SECRET}))
    request_info = aiohttp.RequestInfo(URL(PROD), "POST", headers, URL(PROD))

    class FailingSession:
        def post(self, url, **kwargs):
            raise aiohttp.ClientResponseError(request_info, (), status=500, message="boom")

    with pytest.raises(civicrm.LinkError) as error:
        await civicrm.link(FailingSession(), PROD, KEY, 1, "ada")
    for secret in (KEY, CLIENT_SECRET):
        assert secret not in str(error.value)
