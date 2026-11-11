import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pipabobik.config import load_config
from pipabobik.app import Bot


@pytest.mark.asyncio
async def test_server_state_and_control(tmp_path, monkeypatch):
    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    bot = Bot(load_config(tmp_path / "none.toml"))
    bot.jukebox.data_dir = tmp_path
    srv = bot.server
    srv.key = "secret"

    class FakeChat:
        connected = asyncio.Event()

    bot.chat = FakeChat()
    client = TestClient(TestServer(srv.app))
    await client.start_server()
    try:
        r = await client.get("/api/state")
        st = await r.json()
        assert st["status"] == "offline" and st["queue"] == []

        r = await client.post("/api/control", json={"action": "on"})
        assert r.status == 403
        r = await client.post("/api/control", json={"action": "on"}, headers={"X-Pipabobik-Key": "secret"})
        assert (await r.json())["active"] is True

        for page in ("/widget", "/panel", "/"):
            r = await client.get(page)
            assert r.status == 200
            assert "pipabobik" in await r.text()
    finally:
        await client.close()
