import asyncio
import json
import time
from pathlib import Path

import pytest

from pipabobik.commands import Commands
from pipabobik.config import DEFAULTS, Config, load_config, set_values
from pipabobik.jukebox import Jukebox
from pipabobik.music import Track, best_cover, extract_video_id
from pipabobik.twitch import ChatMessage


# ── фейки ──
class FakePlayer:
    def __init__(self):
        self.volume = 60
        self.paused = False
        self.position = 0.0
        self.available = True
        self.played: list[str] = []
        self._stop = asyncio.Event()

    async def play(self, url, headers=None):
        self.played.append(url)
        self._stop.clear()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=0.05)
            return "stopped"
        except asyncio.TimeoutError:
            return "ended"

    async def stop(self):
        self._stop.set()

    async def set_volume(self, v):
        self.volume = max(0, min(100, v))

    async def set_pause(self, p):
        self.paused = p


class FakeMusic:
    def __init__(self, results=None, fallback=None, fail_ids=()):
        self.results = results or []
        self.fallback = fallback or []
        self.fail_ids = set(fail_ids)

    async def search(self, q, only_songs=True):
        return [Track(**t.to_dict()) for t in self.results]

    async def by_id(self, vid):
        return Track(video_id=vid, title="By id", artist="X", duration=200)

    async def resolve_stream(self, t):
        return None if t.video_id in self.fail_ids else (f"https://audio/{t.video_id}", {})

    async def playlist(self, pl, shuffle=True):
        return [Track(**t.to_dict()) for t in self.fallback]


def T(vid, dur=200, title=None, artist="Artist", explicit=False):
    return Track(video_id=vid, title=title or f"Song {vid}", artist=artist, duration=dur, explicit=explicit)


def msg(text, login="viewer", uid="1", badges=()):
    return ChatMessage(message_id="m" + uid, user_id=uid, login=login, display_name=login, text=text, badges=set(badges))


@pytest.fixture
def cfg():
    return Config(raw=json.loads(json.dumps(DEFAULTS)))


@pytest.fixture
def jb(cfg, tmp_path):
    g = dict(cfg["google"], fallback_playlist="")
    return Jukebox(cfg["rules"], g, FakeMusic(), FakePlayer(), tmp_path)


# ── музика ──
@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=lYBUbBu4W08&list=RDAMVM", "lYBUbBu4W08"),
    ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("Океан Ельзи Обійми", None),
])
def test_extract_video_id(url, vid):
    assert extract_video_id(url) == vid


def test_best_cover_upscales_googleusercontent():
    thumbs = [{"url": "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj", "width": 60, "height": 60},
              {"url": "https://lh3.googleusercontent.com/abc=w120-h120-l90-rj", "width": 120, "height": 120}]
    assert best_cover(thumbs) == "https://lh3.googleusercontent.com/abc=w544-h544-l90-rj"
    assert best_cover([], "id") == "https://i.ytimg.com/vi/id/hqdefault.jpg"


# ── twitch ──
def test_chat_message_strips_reply_prefix_and_reads_badges():
    ev = {
        "message_id": "x", "chatter_user_id": "5", "chatter_user_login": "bob", "chatter_user_name": "Bob",
        "message": {"text": "@alice !трек Бумбокс"},
        "badges": [{"set_id": "moderator", "id": "1", "info": ""}],
        "reply": {"parent_user_login": "alice"},
    }
    m = ChatMessage.from_event(ev)
    assert m.text == "!трек Бумбокс"
    assert m.is_mod and not m.is_broadcaster


# ── правила ──
def test_rules(jb):
    assert jb.check(T("a", dur=10_000)) and "задовгий" in jb.check(T("a", dur=10_000))
    assert jb.check(T("a", dur=10_000), privileged=True) is None  # модерам можна довгі
    jb.add(T("a"), "1", "viewer")
    assert "вже є" in jb.check(T("a"))
    jb.rules["banned_words"] = ["10 hours"]
    assert jb.check(T("b", title="Song 10 Hours loop"))
    jb.rules["block_explicit"] = True
    assert jb.check(T("c", explicit=True))


def test_user_limits(jb):
    jb.rules["cooldown"] = 0
    jb.add(T("a"), "1", "v")
    jb.add(T("b"), "1", "v")
    assert "вже 2" in jb.check_user("1", False)
    assert jb.check_user("1", True) is None
    assert jb.check_user("2", False) is None
    jb.rules["cooldown"] = 30
    jb.last_request["3"] = time.time()
    assert "зачекай" in jb.check_user("3", False)


def test_remove_last_and_persistence(jb, cfg, tmp_path):
    jb.rules["cooldown"] = 0
    jb.add(T("a"), "1", "v")
    jb.add(T("b"), "2", "w")
    jb.add(T("c"), "1", "v")
    assert jb.remove_last_by("1").video_id == "c"
    assert [t.video_id for t in jb.queue] == ["a", "b"]
    jb2 = Jukebox(cfg["rules"], cfg["google"], FakeMusic(), FakePlayer(), tmp_path)
    assert [t.video_id for t in jb2.queue] == ["a", "b"]
    assert jb2.queue[0].requested_by == "v"


def test_eta(jb):
    jb.rules["cooldown"] = 0
    jb.now = T("now", dur=100)
    jb.now.source = "request"
    jb.player.position = 40
    jb.add(T("a", dur=200), "1", "v")
    jb.add(T("b", dur=300), "2", "w")
    assert jb.eta(1) == 60
    assert jb.eta(2) == 260


# ── цикл відтворення ──
@pytest.mark.asyncio
async def test_playback_loop_plays_queue_then_fallback(cfg, tmp_path):
    music = FakeMusic(fallback=[T("f1"), T("f2")], fail_ids={"bad"})
    player = FakePlayer()
    said = []
    g = dict(cfg["google"], fallback_playlist="LM", fallback_shuffle=False)
    jb = Jukebox(cfg["rules"], g, music, player, tmp_path)
    jb.rules["cooldown"] = 0

    async def say(t):
        said.append(t)

    jb.say = say
    jb.add(T("a"), "1", "v")
    jb.add(T("bad"), "2", "w")
    jb.activate()
    task = asyncio.create_task(jb.run())
    await asyncio.sleep(1.5)
    task.cancel()
    urls = player.played
    assert urls[0] == "https://audio/a"
    assert "https://audio/f1" in urls  # після черги — фоновий плейлист
    assert any("не вдалося відтворити" in s for s in said)  # недоступний трек пропущено з повідомленням


@pytest.mark.asyncio
async def test_deactivate_keeps_current_request(jb):
    jb.now = T("cur")
    jb.now.source = "request"
    jb.active = True
    await jb.deactivate()
    assert jb.queue[0].video_id == "cur" and not jb.active


# ── команди ──
@pytest.mark.asyncio
async def test_commands_flow(cfg, tmp_path):
    music = FakeMusic(results=[T("long", dur=5000), T("good", dur=180, title="Обійми", artist="Океан Ельзи")])
    jb = Jukebox(cfg["rules"], dict(cfg["google"], fallback_playlist=""), music, FakePlayer(), tmp_path)
    jb.active = True
    out = []

    async def say(text, reply_to=None):
        out.append(text)

    c = Commands(cfg, jb, music, say)

    await c.handle(msg("!трек обійми"))
    assert "«Обійми» — Океан Ельзи" in out[-1] and "№1" in out[-1]  # задовгий пропущено, взято наступний
    assert jb.queue[0].video_id == "good"

    await c.handle(msg("!трек"))
    assert "напиши назву" in out[-1]

    await c.handle(msg("!скіп", login="viewer"))
    assert "лише модератори" in out[-1]

    jb.now = jb.queue.pop(0)
    await c.handle(msg("!шоце"))
    assert "Обійми" in out[-1] and "music.youtube.com" in out[-1]

    await c.handle(msg("!скіп", login="moder", uid="9", badges={"moderator"}))
    assert "скіпнуто" in out[-1]

    await c.handle(msg("!skip", login="streamer", uid="10", badges={"broadcaster"}))
    assert out[-1]  # стрімер теж може

    await c.handle(msg("!гучність 35", uid="9", badges={"moderator"}))
    assert jb.player.volume == 35

    await c.handle(msg("!трек https://youtu.be/dQw4w9WgXcQ", uid="5"))
    assert jb.queue[-1].video_id == "dQw4w9WgXcQ"

    await c.handle(msg("!нетой", uid="5"))
    assert "прибрав" in out[-1]

    await c.handle(msg("просто текст"))
    n = len(out)
    await c.handle(msg("!невідома"))
    assert len(out) == n


@pytest.mark.asyncio
async def test_admins_from_config_can_skip(cfg, tmp_path):
    cfg.raw["twitch"]["admins"] = ["helper"]
    jb = Jukebox(cfg["rules"], cfg["google"], FakeMusic(), FakePlayer(), tmp_path)
    jb.now = T("x")
    out = []

    async def say(t, r=None):
        out.append(t)

    await Commands(cfg, jb, FakeMusic(), say).handle(msg("!скіп", login="helper"))
    assert "скіпнуто" in out[-1]


@pytest.mark.asyncio
async def test_helpers_can_only_skip_and_change_volume(cfg, tmp_path):
    cfg.raw["twitch"]["helpers"] = ["sonya_tea"]
    jb = Jukebox(cfg["rules"], cfg["google"], FakeMusic(), FakePlayer(), tmp_path)
    jb.now = T("x")
    out = []

    async def say(t, r=None):
        out.append(t)

    c = Commands(cfg, jb, FakeMusic(), say)
    await c.handle(msg("!гучність 20", login="Sonya_Tea", uid="7"))
    assert jb.player.volume == 20
    await c.handle(msg("!пауза", login="sonya_tea", uid="7"))
    assert "лише модератори" in out[-1] and not jb.player.paused
    await c.handle(msg("!бантрек", login="sonya_tea", uid="7"))
    assert "лише модератори" in out[-1]
    await c.handle(msg("!музика", login="sonya_tea", uid="7"))
    assert "тобі можна: !скіп, !гучність" in out[-1]
    await c.handle(msg("!скіп", login="sonya_tea", uid="7"))
    assert "скіпнуто" in out[-1]

    await c.handle(msg("!гучність 90", login="viewer", uid="8"))
    assert "помічники" in out[-1] and jb.player.volume == 20

    # прибрали з помічників у застосунку — права зникають одразу
    cfg.raw["twitch"]["helpers"] = []
    await c.handle(msg("!гучність 90", login="sonya_tea", uid="7"))
    assert jb.player.volume == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["!трек", "!sr", "!songrequest", "!request", "!req", "!track", "!замовити", "!ік", "!nhtr", "!SR"])
async def test_request_aliases(cfg, tmp_path, cmd):
    music = FakeMusic(results=[T("good", dur=180)])
    jb = Jukebox(cfg["rules"], cfg["google"], music, FakePlayer(), tmp_path)
    out = []

    async def say(t, r=None):
        out.append(t)

    await Commands(cfg, jb, music, say).handle(msg(f"{cmd} обійми"))
    assert jb.queue and jb.queue[0].video_id == "good"


@pytest.mark.asyncio
async def test_old_skip_command_is_gone(cfg, tmp_path):
    jb = Jukebox(cfg["rules"], cfg["google"], FakeMusic(), FakePlayer(), tmp_path)
    jb.now = T("x")
    out = []

    async def say(t, r=None):
        out.append(t)

    c = Commands(cfg, jb, FakeMusic(), say)
    await c.handle(msg("!хуйня", login="moder", uid="9", badges={"moderator"}))
    assert out == [] and jb.now
    for alias in ("!скип", "!next"):
        jb.now = T("x")
        await c.handle(msg(alias, login="moder", uid="9", badges={"moderator"}))
        assert "скіпнуто" in out[-1]


def test_old_config_commands_are_migrated(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[twitch]\nchannel = "pipa"\nadmins = ["@Boss"]\n\n[commands]\n# мої команди\n'
        'request = ["!трек", "!sr"]\nnow     = ["!шоце", "!song"]\nskip    = ["!хуйня", "!skip"]\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["commands"]["skip"] == DEFAULTS["commands"]["skip"]
    assert cfg["commands"]["request"] == DEFAULTS["commands"]["request"]
    assert cfg["commands"]["now"] == ["!шоце", "!song"]
    assert cfg["twitch"]["admins"] == ["boss"] and cfg["twitch"]["helpers"] == []
    text = p.read_text(encoding="utf-8")
    assert "!хуйня" not in text and "# мої команди" in text  # збережено у файл, коментарі на місці
    assert load_config(p)["commands"]["skip"][0] == "!скіп"


def test_custom_commands_keep_user_choice(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[commands]\nrequest = ["!пісню"]\nskip = ["!хуйня", "!далі"]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg["commands"]["request"] == ["!пісню"]  # власний вибір не чіпаємо
    assert cfg["commands"]["skip"] == ["!скіп", "!далі"]  # прибрали лише стару назву


# ── конфіг ──
def test_set_values_preserves_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# коментар\n[twitch]\nchannel = ""\n\n[server]\nport = 17777\n', encoding="utf-8")
    set_values(p, {"twitch": {"channel": "pipa", "client_id": "abc"}, "google": {"cookies_file": "C:\\x\\c.txt"}})
    text = p.read_text(encoding="utf-8")
    assert "# коментар" in text
    cfg = load_config(p)
    assert cfg.channel == "pipa"
    assert cfg["twitch"]["client_id"] == "abc"
    assert cfg["google"]["cookies_file"] == "C:\\x\\c.txt"
    assert cfg.port == 17777


def test_example_config_is_valid():
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
    assert cfg["commands"]["skip"][0] == "!скіп"
    assert cfg["twitch"]["helpers"] == []
    assert cfg["commands"]["now"][0] == "!шоце"
    assert cfg["commands"]["request"][0] == "!трек"
