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


@pytest.mark.asyncio
async def test_panel_api_add_settings(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from pipabobik.music import Track

    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    cfg_path = tmp_path / "config.toml"
    shutil.copy(Path(__file__).resolve().parent.parent / "config.example.toml", cfg_path)
    bot = Bot(load_config(cfg_path))
    bot.jukebox.data_dir = tmp_path
    bot.server.key = "k"
    H = {"X-Pipabobik-Key": "k"}

    async def fake_search(q, only_songs=True):
        return [Track("abcdefghijk", "Обійми", "Океан Ельзи", duration=236)]

    bot.music.search = fake_search
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        r = await client.post("/api/search", json={"q": "обійми"}, headers=H)
        res = (await r.json())["results"]
        assert res[0]["title"] == "Обійми"

        await client.post("/api/add", json={"track": res[0], "mode": "end"}, headers=H)
        t2 = dict(res[0], video_id="zzzzzzzzzzz", title="Друга")
        st = await (await client.post("/api/add", json={"track": t2, "mode": "next"}, headers=H)).json()
        assert [t["title"] for t in st["queue"]] == ["Друга", "Обійми"]

        r = await client.post("/api/settings", json={"rules": {"cooldown": 45, "who_can_request": "subs", "banned_words": "a, B"},
                                                     "chat": {"announce_tracks": True}}, headers=H)
        assert r.status == 200
        assert bot.cfg["rules"]["cooldown"] == 45 and bot.jukebox.rules["cooldown"] == 45  # застосовано одразу
        assert bot.jukebox.announce is True
        again = load_config(cfg_path)  # і збережено у файл
        assert again["rules"]["who_can_request"] == "subs" and again["rules"]["banned_words"] == ["a", "b"]

        r = await client.post("/api/settings", json={"twitch": {"helpers": ["@Sonya_Tea", "sonya_tea", "кирилиця", " mod2 "],
                                                                "client_secret": "hack"}}, headers=H)
        assert r.status == 200
        assert bot.cfg["twitch"]["helpers"] == ["sonya_tea", "mod2"]
        assert bot.cfg["twitch"]["client_secret"] != "hack"  # змінювати можна лише список помічників
        s = await (await client.get("/api/settings", headers=H)).json()
        assert s["twitch"] == {"helpers": ["sonya_tea", "mod2"]} and s["commands"]["skip"] == "!скіп"
        assert load_config(cfg_path)["twitch"]["helpers"] == ["sonya_tea", "mod2"]

        st = await (await client.post("/api/control", json={"action": "clear"}, headers=H)).json()
        assert st["queue"] == []
        assert (await client.post("/api/settings", json={}, headers={})).status == 403
    finally:
        await client.close()
