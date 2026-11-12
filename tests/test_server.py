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

        r = await client.post("/api/settings", json={"rules": {"cooldown": 45, "banned_words": "a, B"},
                                                     "chat": {"announce_tracks": True}}, headers=H)
        assert r.status == 200
        assert bot.cfg["rules"]["cooldown"] == 45 and bot.jukebox.rules["cooldown"] == 45  # застосовано одразу
        assert bot.jukebox.announce is True
        again = load_config(cfg_path)  # і збережено у файл
        assert again["rules"]["banned_words"] == ["a", "b"]

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


@pytest.mark.asyncio
async def test_access_api(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from pipabobik.twitch import ChatMessage

    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    cfg_path = tmp_path / "config.toml"
    shutil.copy(Path(__file__).resolve().parent.parent / "config.example.toml", cfg_path)
    bot = Bot(load_config(cfg_path))
    bot.jukebox.data_dir = tmp_path
    bot.server.key = "k"
    H = {"X-Pipabobik-Key": "k"}

    async def say(text, reply_to=None):
        pass

    from pipabobik.commands import Commands

    bot.commands = Commands(bot.cfg, bot.jukebox, bot.music, say)
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        a = await (await client.get("/api/access", headers=H)).json()
        keys = [c["key"] for c in a["commands"]]
        assert keys[0] == "request" and "seek" in keys
        assert a["limits"]["all"]["max_per_user"] == 2 and a["limits"]["mods"]["bypass_queue"] is True

        # перейменування, нова назва, ролі
        r = await client.post("/api/access", json={"commands": {
            "skip": {"names": ["Далі", "!skip"], "roles": ["vip", "helpers", "зайве"]},
            "request": {"roles": ["subs", "all"]},
        }}, headers=H)
        a = await r.json()
        skip = next(c for c in a["commands"] if c["key"] == "skip")
        assert skip["names"] == ["!далі", "!skip"] and skip["roles"] == ["vip", "helpers"]
        assert next(c for c in a["commands"] if c["key"] == "request")["roles"] == ["all"]
        assert "!далі" in bot.commands.table and "!скіп" not in bot.commands.table  # діє одразу

        # конфлікти і погані назви не зберігаються
        r = await client.post("/api/access", json={"commands": {"volume": {"names": ["!далі"]}}}, headers=H)
        assert r.status == 400 and "«Пропустити»" in (await r.json())["error"]
        assert (await client.post("/api/access", json={"commands": {"volume": {"names": ["!дві слова"]}}}, headers=H)).status == 400
        assert (await client.post("/api/access", json={"commands": {"volume": {"names": []}}}, headers=H)).status == 400
        assert bot.cfg["commands"]["volume"][0] == "!гучність"

        # ліміти ролей і помічники
        r = await client.post("/api/access", json={
            "limits": {"all": {"cooldown": 60}, "subs": {"custom": True, "max_per_user": 5, "max_duration": "abc"}},
            "max_queue": 50, "helpers": ["@Pipa_Fan", "погано"],
        }, headers=H)
        a = await r.json()
        assert a["limits"]["all"]["cooldown"] == 60 and a["limits"]["subs"]["max_per_user"] == 5
        assert a["limits"]["subs"]["max_duration"] == 0 and a["max_queue"] == 50 and a["helpers"] == ["pipa_fan"]
        fan = ChatMessage("m", "1", "pipa_fan", "Pipa_Fan", "!далі")
        assert bot.commands.can(fan, "skip")

        # усе збережено в config.toml
        again = load_config(cfg_path)
        assert again["commands"]["skip"] == ["!далі", "!skip"] and again["permissions"]["skip"] == ["vip", "helpers"]
        assert again["limits"]["subs"]["custom"] is True and again["limits"]["subs"]["max_per_user"] == 5
        assert again["rules"]["cooldown"] == 60 and again["rules"]["max_queue"] == 50
        assert again["twitch"]["helpers"] == ["pipa_fan"]
        assert (await client.get("/api/access")).status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_control_seek(tmp_path, monkeypatch):
    from pipabobik.music import Track

    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    bot = Bot(load_config(tmp_path / "none.toml"))
    bot.server.key = "k"
    H = {"X-Pipabobik-Key": "k"}
    seeks = []

    async def fake_seek(pos):
        seeks.append(pos)
        return True

    bot.player.seek = fake_seek
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        assert (await client.post("/api/control", json={"action": "seek", "position": 30}, headers=H)).status == 409
        bot.jukebox.now = Track("abcdefghijk", "Обійми", "Океан Ельзи", duration=236)
        assert (await client.post("/api/control", json={"action": "seek", "position": 90}, headers=H)).status == 200
        await client.post("/api/control", json={"action": "seek", "position": 999}, headers=H)
        assert seeks == [90, 234]
        assert (await client.post("/api/control", json={"action": "seek", "position": "x"}, headers=H)).status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_panel_skip_is_addressed_to_one_track(tmp_path, monkeypatch):
    from pipabobik.music import Track

    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    bot = Bot(load_config(tmp_path / "none.toml"))
    bot.server.key = "k"
    H = {"X-Pipabobik-Key": "k"}
    skipped = []

    async def fake_stop():
        skipped.append(bot.jukebox.now.video_id)

    bot.player.stop = fake_stop
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        bot.jukebox.now = Track("aaaaaaaaaaa", "Перша", "X", duration=200)
        r = await (await client.post("/api/control", json={"action": "skip", "video_id": "aaaaaaaaaaa"}, headers=H)).json()
        assert not r.get("ignored") and skipped == ["aaaaaaaaaaa"]
        bot.jukebox.now = Track("bbbbbbbbbbb", "Друга", "X", duration=200)
        # друге натискання ще про першу пісню — другу не чіпаємо
        r = await (await client.post("/api/control", json={"action": "skip", "video_id": "aaaaaaaaaaa"}, headers=H)).json()
        assert r["ignored"] and skipped == ["aaaaaaaaaaa"]
        # док OBS без адресата: повтор одразу після скіпу теж ігнорується
        r = await (await client.post("/api/control", json={"action": "skip"}, headers=H)).json()
        assert r["ignored"] and skipped == ["aaaaaaaaaaa"]
        assert "boot" in r
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restart_action(tmp_path, monkeypatch):
    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    bot = Bot(load_config(tmp_path / "none.toml"))
    bot.server.key = "k"
    called = []
    bot.restart = lambda: called.append(True)
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        r = await client.post("/api/control", json={"action": "restart"}, headers={"X-Pipabobik-Key": "k"})
        assert r.status == 200
        await asyncio.sleep(0.4)
        assert called == [True]
    finally:
        await client.close()


@pytest.mark.parametrize("text,pid", [
    ("LM", "LM"), ("lm", "LM"),
    ("https://music.youtube.com/playlist?list=PLabc_DEF-123&si=x", "PLabc_DEF-123"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDCLAK5uy_kXyz", "RDCLAK5uy_kXyz"),
    ("https://music.youtube.com/browse/VLPLabc123", "PLabc123"),
    ("OLAK5uy_mAbc", "OLAK5uy_mAbc"), ("", None), ("https://example.com/x y", None),
])
def test_playlist_id(text, pid):
    from pipabobik.playlists import playlist_id

    assert playlist_id(text) == pid


@pytest.mark.asyncio
async def test_playlists_api(tmp_path, monkeypatch):
    from pipabobik.music import Track

    monkeypatch.setattr("pipabobik.app.DATA_DIR", tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[google]\nfallback_playlist = "https://music.youtube.com/playlist?list=PLold"\n', encoding="utf-8")
    bot = Bot(load_config(cfg_path))
    bot.server.key = "k"
    H = {"X-Pipabobik-Key": "k"}
    skipped = []

    async def fake_skip(reason="", only=None):
        skipped.append(reason)

    async def fake_title(pid):
        return "Чілл на стрім"

    bot.jukebox.skip = fake_skip
    bot.music.playlist_title = fake_title
    client = TestClient(TestServer(bot.server.app))
    await client.start_server()
    try:
        p = await (await client.get("/api/playlists", headers=H)).json()
        # перший запуск: «Вподобані» + плейлист, що вже був у config.toml
        assert [x["id"] for x in p["items"]] == ["LM", "PLold"] and p["active"] == "PLold"

        p = await (await client.post("/api/playlists", json={"action": "add", "link": "https://music.youtube.com/playlist?list=PLchill"}, headers=H)).json()
        assert p["items"][-1] == {"id": "PLchill", "name": "Чілл на стрім"}
        r = await client.post("/api/playlists", json={"action": "add", "link": "просто текст"}, headers=H)
        assert r.status == 400

        bot.jukebox.now = Track("fbfbfbfbfbf", "Фон", "X", source="fallback")
        bot.jukebox._fallback = [Track("old00000000", "Старий", "X", source="fallback")]
        p = await (await client.post("/api/playlists", json={"action": "select", "id": "PLchill"}, headers=H)).json()
        assert p["active"] == "PLchill" and bot.cfg["google"]["fallback_playlist"] == "PLchill"
        assert skipped == ["перемкнули фоновий плейлист"] and bot.jukebox._fallback == []  # новий плейлист — одразу
        assert load_config(cfg_path)["google"]["fallback_playlist"] == "PLchill"
        st = await (await client.get("/api/state", headers=H)).json()
        assert st["fallback"] == "PLchill"

        p = await (await client.post("/api/playlists", json={"action": "select", "id": ""}, headers=H)).json()
        assert p["active"] == ""  # тиша
        assert (await client.post("/api/playlists", json={"action": "select", "id": "PLnope"}, headers=H)).status == 400

        await client.post("/api/playlists", json={"action": "select", "id": "PLchill"}, headers=H)
        p = await (await client.post("/api/playlists", json={"action": "remove", "id": "PLchill"}, headers=H)).json()
        assert "PLchill" not in [x["id"] for x in p["items"]] and p["active"] == "LM"  # прибрали те, що грало

        p = await (await client.post("/api/playlists", json={"action": "shuffle", "value": False}, headers=H)).json()
        assert p["shuffle"] is False and load_config(cfg_path)["google"]["fallback_shuffle"] is False

        # список пережив перезапуск
        assert [x["id"] for x in Bot(load_config(cfg_path)).playlists.items] == ["LM", "PLold"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_cache():
    from pipabobik.music import MusicSource, Track

    m = MusicSource()
    calls = []

    def fake(q, only_songs, limit=8):
        calls.append(q)
        return [Track("aaaaaaaaaaa", "Обійми", "Океан Ельзи")]

    m._search_sync = fake
    a = await m.search("Обійми  океан")
    b = await m.search("обійми океан")
    assert calls == ["Обійми  океан"] and a[0].title == b[0].title
    b[0].requested_by = "x"  # черга змінює трек — кеш від цього не псується
    assert (await m.search("обійми океан"))[0].requested_by == ""
