import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer
import main


@pytest.fixture
async def client(monkeypatch):
    async def fake_stats(pool):
        assert pool == "the pool"
        return {"records": 3}

    monkeypatch.setattr(main.db, "discord_member_stats", fake_stats)
    monkeypatch.setattr(main, "db_pool", "the pool")
    monkeypatch.setattr(main, "STATS_SECRET", "s3cret")
    async with TestClient(TestServer(main.make_app())) as client:
        yield client


async def test_stats_answer_the_counts_with_the_right_secret(client):
    response = await client.get("/stats/discord", headers={"Authorization": "Bearer s3cret"})
    assert response.status == 200
    assert await response.json() == {"records": 3}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"},
                                     {"Authorization": "Bearer s3crét"}])
async def test_stats_refuse_a_missing_or_wrong_secret(client, headers):
    response = await client.get("/stats/discord", headers=headers)
    assert response.status == 401


async def test_stats_refuse_while_no_secret_is_set(client, monkeypatch):
    monkeypatch.setattr(main, "STATS_SECRET", None)
    response = await client.get("/stats/discord", headers={"Authorization": "Bearer None"})
    assert response.status == 500


async def test_stats_refuse_a_header_that_is_not_utf8(client):
    # Raw bytes, since aiohttp's client won't send a header that isn't valid UTF-8
    reader, writer = await asyncio.open_connection(client.host, client.port)
    writer.write(b"GET /stats/discord HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer \xff\r\n"
                 b"Connection: close\r\n\r\n")
    status_line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    assert status_line.split()[1] == b"401"


async def test_stats_do_not_accept_the_webhook_secret(client, monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "hook")
    response = await client.get("/stats/discord", headers={"Authorization": "Bearer hook"})
    assert response.status == 401
