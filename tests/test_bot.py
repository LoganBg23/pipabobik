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

    async def play(self, url, headers=None, start=0.0):
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

    async def seek(self, pos):
        self.position = pos
        return True


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
    assert "лише" in out[-1] and "модератори" in out[-1]

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
    assert "лише" in out[-1] and "модератори" in out[-1] and not jb.player.paused
    await c.handle(msg("!бантрек", login="sonya_tea", uid="7"))
    assert "лише" in out[-1] and "модератори" in out[-1]
    await c.handle(msg("!музика", login="sonya_tea", uid="7"))
    assert "!скіп — пропустити" in out[-1] and "!гучність" in out[-1] and "!бантрек" not in out[-1]
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


# ── ролі, дозволи, ліміти ──
from pipabobik.access import Access  # noqa: E402
from pipabobik.commands import parse_seek  # noqa: E402


def test_roles_and_most_generous_limits(cfg):
    cfg.raw["twitch"]["helpers"] = ["helpy"]
    cfg.raw["rules"].update(max_per_user=2, cooldown=30, max_duration=420)
    cfg.raw["limits"]["subs"].update(custom=True, max_per_user=4, cooldown=10, max_duration=300)
    cfg.raw["limits"]["vip"].update(custom=True, max_per_user=3, cooldown=60, max_duration=0)
    a = Access(cfg)
    viewer, sub = msg("x"), msg("x", badges={"subscriber"})
    subvip = msg("x", badges={"subscriber", "vip"})
    assert a.roles_of(viewer) == {"all"}
    assert a.roles_of(msg("x", login="Helpy")) == {"all", "helpers"}
    assert a.limits(viewer) == {"max_per_user": 2, "cooldown": 30, "max_duration": 420, "bypass_queue": False}
    # у саба свої ліміти, але довжина треку береться щедріша — від «усіх»
    assert a.limits(sub) == {"max_per_user": 4, "cooldown": 10, "max_duration": 420, "bypass_queue": False}
    # саб + VIP: найщедріше з обох ролей (0 = без обмежень)
    assert a.limits(subvip) == {"max_per_user": 4, "cooldown": 10, "max_duration": 0, "bypass_queue": False}
    assert a.limits(msg("x", badges={"moderator"}))["bypass_queue"] is True
    assert a.limits(msg("x", badges={"broadcaster"}))["max_per_user"] == 0
    # роль без своїх лімітів = як у всіх
    assert a.limits(msg("x", login="helpy")) == a.limits(viewer)


@pytest.mark.asyncio
async def test_permissions_per_role(cfg, tmp_path):
    jb = Jukebox(cfg["rules"], cfg["google"], FakeMusic(results=[T("a")]), FakePlayer(), tmp_path)
    jb.now = T("x")
    out = []

    async def say(t, r=None):
        out.append(t)

    c = Commands(cfg, jb, FakeMusic(results=[T("a")]), say)
    cfg.raw["permissions"]["request"] = ["subs", "vip"]
    cfg.raw["permissions"]["skip"] = ["vip"]
    await c.handle(msg("!трек a"))
    assert "не всім" in out[-1] and not jb.queue
    await c.handle(msg("!трек a", uid="2", badges={"subscriber"}))
    assert jb.queue
    await c.handle(msg("!скіп", uid="3", badges={"moderator"}))
    assert out[-1] == "це можуть лише VIP і стрімер 😌"
    await c.handle(msg("!скіп", uid="4", badges={"vip"}))
    assert "скіпнуто" in out[-1]
    cfg.raw["permissions"]["skip"] = []  # нікому, крім стрімера
    jb.now = T("y")
    await c.handle(msg("!скіп", uid="4", badges={"vip"}))
    assert out[-1] == "це можуть лише стрімер 😌"
    await c.handle(msg("!скіп", uid="5", badges={"broadcaster"}))
    assert "скіпнуто" in out[-1]


@pytest.mark.asyncio
async def test_role_limits_apply_to_requests(cfg, tmp_path):
    music = FakeMusic(results=[T("long", dur=500)])
    jb = Jukebox(cfg["rules"], cfg["google"], music, FakePlayer(), tmp_path)
    out = []

    async def say(t, r=None):
        out.append(t)

    c = Commands(cfg, jb, music, say)
    cfg.raw["rules"]["max_duration"] = 420
    await c.handle(msg("!трек long"))
    assert "задовгий" in out[-1]
    cfg.raw["limits"]["subs"].update(custom=True, max_duration=0)
    await c.handle(msg("!трек long", uid="2", badges={"subscriber"}))
    assert jb.queue and jb.queue[0].video_id == "long"

    cfg.raw["rules"]["max_queue"] = 1
    music.results = [T("b")]
    await c.handle(msg("!трек b", uid="3", badges={"vip"}))
    assert "переповнена" in out[-1]
    await c.handle(msg("!трек b", uid="4", badges={"moderator"}))
    assert len(jb.queue) == 2  # модераторам можна в переповнену чергу


def test_old_who_can_request_becomes_permission(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[rules]\nwho_can_request = "subs"\n', encoding="utf-8")
    assert load_config(p)["permissions"]["request"] == ["subs", "vip", "mods"]
    p.write_text('[rules]\nwho_can_request = "subs"\n\n[permissions]\nrequest = ["VIP", "nonsense"]\n', encoding="utf-8")
    assert load_config(p)["permissions"]["request"] == ["vip"]  # нове налаштування важливіше за старе


@pytest.mark.parametrize("arg,cur,expected", [
    ("1:30", 10, 90), ("90", 10, 90), ("+30", 10, 40), ("-15", 40, 25), ("+1:00", 5, 65),
    ("1:02:03", 0, 3723), ("0,5", 0, 0.5), ("", 0, None), ("abc", 0, None), ("1:-2", 0, None),
])
def test_parse_seek(arg, cur, expected):
    assert parse_seek(arg, cur) == expected


@pytest.mark.asyncio
async def test_seek_command(cfg, tmp_path):
    jb = Jukebox(cfg["rules"], cfg["google"], FakeMusic(), FakePlayer(), tmp_path)
    out = []

    async def say(t, r=None):
        out.append(t)

    c = Commands(cfg, jb, FakeMusic(), say)
    mod = {"moderator"}
    await c.handle(msg("!перемотка 1:00", badges=mod))
    assert "нічого не грає" in out[-1]
    jb.now = T("x", dur=200)
    await c.handle(msg("!перемотка 1:30", badges=mod))
    assert jb.player.position == 90 and "1:30 / 3:20" in out[-1]
    await c.handle(msg("!мотай +30", badges=mod))
    assert jb.player.position == 120
    await c.handle(msg("!seek 9:00", badges=mod))
    assert jb.player.position == 198  # не далі кінця треку
    await c.handle(msg("!перемотка вперед", badges=mod))
    assert "формат" in out[-1]
    await c.handle(msg("!перемотка 0:10"))
    assert "лише модератори" in out[-1] and jb.player.position == 198


# ── пропуски й обриви ──
class BlockingPlayer(FakePlayer):
    """Як справжній mpv: play() триває, доки трек не дограє або його не зупинять."""

    def __init__(self, cut_first_at=None):
        super().__init__()
        self.starts = []
        self.last_played = 0.0
        self.last_error = ""
        self._ev = None
        self.cut_first_at = cut_first_at

    async def play(self, url, headers=None, start=0.0):
        await self.stop()
        self.starts.append((url, start))
        if self.cut_first_at is not None and len(self.starts) == 1:
            self.last_played = self.cut_first_at  # YouTube обірвав потік
            return "ended"
        self._ev = asyncio.Event()
        try:
            await asyncio.wait_for(self._ev.wait(), timeout=0.5)
            res = "stopped"
        except asyncio.TimeoutError:
            res = "ended"
        self._ev = None
        self.last_played = 200.0
        return res

    async def stop(self):
        if self._ev:
            self._ev.set()
            await asyncio.sleep(0.01)


class SlowMusic(FakeMusic):
    async def resolve_stream(self, t):
        await asyncio.sleep(0.05)
        return f"https://audio/{t.video_id}", {}


async def _run_until(jb, cond, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond() and asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_late_skip_does_not_drop_next_request(cfg, tmp_path):
    p = BlockingPlayer()
    music = SlowMusic(fallback=[T("fb1"), T("fb2")])
    jb = Jukebox(dict(cfg["rules"], interrupt_fallback=True), dict(cfg["google"], fallback_playlist="LM", fallback_shuffle=False),
                 music, p, tmp_path)
    jb.activate()
    task = asyncio.create_task(jb.run())
    try:
        await _run_until(jb, lambda: jb.now and jb.now.video_id == "fb1" and p._ev)
        fb = jb.now
        jb.add(T("r1"), "1", "a")  # перебиває фонову музику
        await _run_until(jb, lambda: jb.now and jb.now.video_id == "r1")
        # запізнілий скіп, адресований фоновому треку (напр. друге замовлення чи кнопка «Зараз»)
        assert await jb.skip("запізнілий", only=fb) is None
        await asyncio.sleep(0.15)
        assert jb.now and jb.now.video_id == "r1" and p._ev  # замовлення грає далі
        assert [u for u, _ in p.starts] == ["https://audio/fb1", "https://audio/r1"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_two_requests_at_once_during_fallback(cfg, tmp_path):
    p = BlockingPlayer()
    music = SlowMusic(fallback=[T("fb1")])
    jb = Jukebox(dict(cfg["rules"], interrupt_fallback=True), dict(cfg["google"], fallback_playlist="LM", fallback_shuffle=False),
                 music, p, tmp_path)
    jb.activate()
    task = asyncio.create_task(jb.run())
    try:
        await _run_until(jb, lambda: jb.now and jb.now.video_id == "fb1" and p._ev)
        jb.add(T("r1"), "1", "a")
        jb.add(T("r2"), "2", "b")  # два «скіпи» фонового треку одночасно
        await _run_until(jb, lambda: jb.now and jb.now.video_id == "r1" and p._ev)
        await asyncio.sleep(0.2)
        assert jb.now.video_id == "r1" and [t.video_id for t in jb.queue] == ["r2"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_cut_stream_resumes_from_same_place(cfg, tmp_path):
    p = BlockingPlayer(cut_first_at=40.0)
    music = SlowMusic()
    jb = Jukebox(cfg["rules"], dict(cfg["google"], fallback_playlist=""), music, p, tmp_path)
    jb.activate()
    jb.add(T("r1", dur=200), "1", "a")
    task = asyncio.create_task(jb.run())
    try:
        await _run_until(jb, lambda: len(p.starts) == 2)
        assert p.starts == [("https://audio/r1", 0.0), ("https://audio/r1", 38.0)]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_skip_logs_who_did_it(cfg, tmp_path, caplog):
    jb = Jukebox(cfg["rules"], cfg["google"], FakeMusic(), FakePlayer(), tmp_path)
    jb.now = T("x", title="Обійми")
    out = []

    async def say(t, r=None):
        out.append(t)

    with caplog.at_level("INFO", logger="pipabobik.jukebox"):
        await Commands(cfg, jb, FakeMusic(), say).handle(msg("!skip", login="moder", badges={"moderator"}))
    assert "Пропущено «Обійми» — Artist — команда !skip від moder" in caplog.text
