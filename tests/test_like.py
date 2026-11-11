import pytest
from aiohttp.test_utils import TestClient, TestServer

from pipabobik.app import Bot
from pipabobik.config import load_config
from pipabobik.music import LoginRequired, MusicSource, Track


def test_authed_client_signs_requests_and_likes(monkeypatch):
    m = MusicSource("firefox", "")
    monkeypatch.setattr(m, "_google_cookies", lambda: {"SAPISID": "abc", "__Secure-3PAPISID": "abc", "SID": "x"})
    monkeypatch.setattr("ytmusicapi.ytmusic.get_visitor_id", lambda *a, **k: {"X-Goog-Visitor-Id": "test"})  # без мережі
    ytm = m._authed_ytm()
    assert ytm.headers["authorization"].startswith("SAPISIDHASH ")
    assert "SAPISID=abc" in ytm.headers["cookie"]
    sent = {}
    monkeypatch.setattr(ytm, "_send_request", lambda endpoint, body, *a, **k: sent.update(endpoint=endpoint, body=body) or {})
    m._rate_sync("dQw4w9WgXcQ", True)
    assert sent["endpoint"] == "like/like" and sent["body"]["target"]["videoId"] == "dQw4w9WgXcQ"
    m._rate_sync("dQw4w9WgXcQ", False)
    assert sent["endpoint"] == "like/removelike"


def test_like_without_login_raises(monkeypatch):
    m = MusicSource("", "")
    with pytest.raises(LoginRequired):
        m._authed_ytm()


@pytest.mark.asyncio
async def test_like_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    bot = Bot(load_config(tmp_path / "none.toml"))
    bot.jukebox.data_dir = tmp_path
    bot.server.key = "k"
    calls = []

    async def fake_rate(vid, like):
        calls.append((vid, like))

    bot.music.rate = fake_rate
    bot.jukebox.now = Track("abcdefghijk", "Обійми", "Океан Ельзи")
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        H = {"X-Pipabobik-Key": "k"}
        st = await (await client.post("/api/control", json={"action": "like"}, headers=H)).json()
        assert st["now"]["liked"] is True and calls[-1] == ("abcdefghijk", True)
        st = await (await client.post("/api/control", json={"action": "like"}, headers=H)).json()
        assert st["now"]["liked"] is False and calls[-1] == ("abcdefghijk", False)

        async def no_login(vid, like):
            raise LoginRequired()

        bot.music.rate = no_login
        r = await client.post("/api/control", json={"action": "like", "like": True}, headers=H)
        assert r.status == 409 and "Firefox" in (await r.json())["error"]
    finally:
        await client.close()
