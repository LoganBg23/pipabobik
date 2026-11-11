import asyncio
import json

import pytest

from pipabobik.commands import Commands
from pipabobik.config import DEFAULTS, Config
from pipabobik.jukebox import Jukebox
from pipabobik.music import Track
from pipabobik.rufilter import RuFilter, russian_language, split_artists
from tests.test_bot import FakeMusic, FakePlayer, msg


@pytest.mark.parametrize("text,ru", [
    ("Звезда по имени Солнце", True), ("Ты не один", True), ("Мне нужна только ты", True), ("Тебя люблю", True),
    ("Обійми", False), ("Я тебе не вірю", False), ("Город", False), ("Shape of You", False),
    ("Ой у лузі червона калина", False), ("Мама", False), ("Купалінка ў полі", False),
])
def test_language(text, ru):
    assert russian_language(text) is ru


def test_artists_list_and_split(tmp_path):
    f = RuFilter(tmp_path / "c.json", extra_artists=["Нова Зірка Росії"])
    assert f.quick_check("Кукла колдуна", "Король и Шут")
    assert f.quick_check("Cadillac", "MORGENSHTERN, Элджей")
    assert f.quick_check("Song", "нова зірка росії")
    assert f.quick_check("Стефанія", "Kalush Orchestra") is None
    assert split_artists("Мияги & Эндшпиль feat. Рем Дигга") == ["Мияги", "Эндшпиль", "Рем Дигга"]


def test_pick_country():
    artists = [
        {"name": "Other", "score": 100, "country": "US"},
        {"name": "Zivert", "score": 98, "country": "RU"},
    ]
    assert RuFilter._pick_country("zivert", artists) == "RU"
    assert RuFilter._pick_country("zivert", [{"name": "Zivert", "score": 50, "country": "RU"}]) is None
    assert RuFilter._pick_country("x", [{"name": "X", "score": 100, "area": {"name": "Moscow"}}]) == "RU"
    assert RuFilter._pick_country("y", [{"name": "Y", "score": 100, "aliases": [{"name": "y"}], "country": "UA"}]) == "UA"


class FakeResp:
    def __init__(self, data):
        self.status, self._d = 200, data

    async def json(self, content_type=None):
        return self._d

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, data):
        self.data, self.calls = data, 0

    def get(self, *a, **k):
        self.calls += 1
        return FakeResp(self.data)


@pytest.mark.asyncio
async def test_musicbrainz_lookup_is_cached(tmp_path):
    sess = FakeSession({"artists": [{"name": "Unknown Rapper", "score": 100, "country": "RU"}]})
    f = RuFilter(tmp_path / "c.json", session=sess)
    assert await f.check("Track", "Unknown Rapper") == "російський виконавець (Unknown Rapper)"
    assert await f.check("Track 2", "Unknown Rapper")
    assert sess.calls == 1  # з кешу
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["unknown rapper"] == "RU"


@pytest.mark.asyncio
async def test_request_of_russian_song_is_rejected_and_banned(tmp_path):
    cfg = Config(raw=json.loads(json.dumps(DEFAULTS)))
    music = FakeMusic(results=[Track("ruuuuuuuuuu", "Группа крови", "Кино", duration=200),
                               Track("uaaaaaaaaaa", "Обійми", "Океан Ельзи", duration=200)])
    jb = Jukebox(cfg["rules"], dict(cfg["google"], fallback_playlist=""), music, FakePlayer(), tmp_path)
    rf = RuFilter(tmp_path / "c.json")
    jb.content_filter = lambda t: rf.check(t.title, t.artist)
    out = []

    async def say(t, r=None):
        out.append(t)

    await Commands(cfg, jb, music, say).handle(msg("!трек группа крови"))
    assert "Російське тут не граємо" in out[-1]
    assert jb.queue == []  # не підсунули інший трек
    assert "ruuuuuuuuuu" in jb.banned_ids

    jb.unban("ruuuuuuuuuu")  # стрімер розбанив → фільтр більше не чіпає
    await Commands(cfg, jb, music, say).handle(msg("!трек группа крови", uid="2"))
    assert jb.queue and jb.queue[0].video_id == "ruuuuuuuuuu"


@pytest.mark.asyncio
async def test_fallback_skips_russian(tmp_path):
    cfg = Config(raw=json.loads(json.dumps(DEFAULTS)))
    music = FakeMusic(fallback=[Track("r1aaaaaaaaa", "Ты", "Кто-то"), Track("u1aaaaaaaaa", "Обійми", "Океан Ельзи")])
    jb = Jukebox(cfg["rules"], dict(cfg["google"], fallback_shuffle=False), music, FakePlayer(), tmp_path)
    rf = RuFilter(tmp_path / "c.json")
    jb.content_filter = lambda t: rf.check(t.title, t.artist)
    t = await jb._next_fallback()
    assert t.video_id == "u1aaaaaaaaa" and "r1aaaaaaaaa" in jb.banned_ids


@pytest.mark.asyncio
async def test_shutdown_keeps_current_track_in_queue(tmp_path):
    cfg = Config(raw=json.loads(json.dumps(DEFAULTS)))
    player = FakePlayer()
    jb = Jukebox(cfg["rules"], dict(cfg["google"], fallback_playlist=""), FakeMusic(), player, tmp_path)
    jb.rules["cooldown"] = 0
    for v in ("a", "b", "c"):
        jb.add(Track(v * 11, v, "x", duration=200), "1", "u")
    # повільний «плеєр», щоб трек грав у момент вимкнення
    async def slow_play(url, headers=None):
        player._stop.clear()
        await player._stop.wait()
        return "stopped"
    player.play = slow_play
    jb.activate()
    task = asyncio.create_task(jb.run())
    await asyncio.sleep(0.1)
    assert jb.now.video_id == "a" * 11
    await jb.deactivate()  # те, що робить бот при закритті OBS / панелі
    await asyncio.sleep(0.1)
    task.cancel()
    assert [t.video_id for t in jb.queue] == ["a" * 11, "b" * 11, "c" * 11]
    saved = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert [t["video_id"] for t in saved] == ["a" * 11, "b" * 11, "c" * 11]
