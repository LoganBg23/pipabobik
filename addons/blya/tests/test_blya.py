import time

import pytest

import addons.blya as blya
from addons.blya import BlyaAddon, install
from addons.blya.layout import fix_text
from pipabobik.twitch import ChatMessage


@pytest.mark.parametrize("wrong,right", [
    ("ghbdsn dcsv", "привіт всім"),
    ("zr cghfdb&", "як справи?"),
    ("ghbdsn, zr cghfdb&", "привіт, як справи?"),
    ("lzre. pf gscy.", "дякую за пісню"),
    ("nb ltq ckfdf ecs[", "ти дей слава усіх"),
    ("Ckfdf Erhf]ys!", "Слава Україні!"),
    ("yfcnegyf gscyz ,elt rhfof", "наступна пісня буде краща"),
    ("v`zcj", "м'ясо"),
    ("GHBDSN", "ПРИВІТ"),
    ("руддщ цщкдв", "hello world"),  # хотіли англійською, а була українська розкладка
    ("ghbdsn KEKW", "привіт KEKW"),  # емоути не чіпаємо
    ("ghbdsn @some_one", "привіт @some_one"),  # згадки теж
    ("ghbdsn https://youtu.be/x", "привіт https://youtu.be/x"),
    ("ghbdsn :)", "привіт :)"),
])
def test_fix_wrong_layout(wrong, right):
    assert fix_text(wrong) == (right, True)


@pytest.mark.parametrize("ok", ["hello world", "how are you", "don't stop me now", "i'm fine thanks", "привіт усім",
                                "hello, world.", "ok", "gg wp", "1 2 3", "catJAM catJAM"])
def test_correct_text_is_left_alone(ok):
    assert fix_text(ok) == (ok, False)


# ── інтеграція з ботом ──
def ev(text, login="pipa_fan", name="Pipa_Fan", mid="m1", uid="1", fragments=None, reply=None):
    e = {
        "message_id": mid, "chatter_user_id": uid, "chatter_user_login": login, "chatter_user_name": name,
        "message": {"text": text, "fragments": fragments or [{"type": "text", "text": text}]}, "badges": [],
    }
    if reply:
        e["reply"] = reply
    return e


class FakeAuth:
    login = "pipabobik_bot"


class FakeChat:
    handler = None


class FakeBot:
    def __init__(self):
        self.auth, self.chat, self.out, self.passed = FakeAuth(), FakeChat(), [], []

        async def main_handler(cm):
            self.passed.append(cm.text)

        self.chat.handler = main_handler

    async def say(self, text, reply_to=None):
        self.out.append((text, reply_to))


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(blya, "USER_COOLDOWN", 0)
    monkeypatch.setattr(blya, "GLOBAL_COOLDOWN", 0)
    b = FakeBot()
    install(b)
    return b


async def send(bot, e):
    await bot.chat.handler(ChatMessage.from_event(e))


async def test_own_last_message(bot):
    await send(bot, ev("ghbdsn dcsv", mid="a1"))
    await send(bot, ev("!бля", mid="a2"))
    assert bot.out[-1] == ("Pipa_Fan хотів гавкнуть: привіт всім", "a1")  # реплай саме на те повідомлення
    assert bot.passed == ["ghbdsn dcsv"]  # звичайні повідомлення йдуть далі в бота, «!бля» — ні


async def test_command_typed_in_wrong_layout_too(bot):
    await send(bot, ev("nfr", mid="a1"))
    await send(bot, ev("!,kz", mid="a2"))
    assert bot.out[-1][0] == "Pipa_Fan хотів гавкнуть: так"


async def test_reply_to_someone_else_and_nick_is_not_translated(bot):
    # Ghbdsn_Nick — нік, схожий на «привіт» у неправильній розкладці; його не можна перекладати.
    # Він відповідав комусь, тож Twitch додав «@someone» на початок — це теж не частина повідомлення.
    await send(bot, ev("@someone ,elm kfcrf", login="ghbdsn_nick", name="Ghbdsn_Nick", mid="b1", uid="2",
                       fragments=[{"type": "mention", "text": "@someone"}, {"type": "text", "text": " ,elm kfcrf"}],
                       reply={"parent_message_id": "x0", "parent_user_login": "someone", "parent_user_name": "someone"}))
    reply = {"parent_message_id": "b1", "parent_user_login": "ghbdsn_nick", "parent_user_name": "Ghbdsn_Nick",
             "parent_message_body": "@someone ,elm kfcrf"}
    await send(bot, ev("@ghbdsn_nick !бля", mid="b2", uid="3", login="helper", name="Helper",
                       fragments=[{"type": "mention", "text": "@ghbdsn_nick"}, {"type": "text", "text": " !бля"}], reply=reply))
    assert bot.out[-1] == ("Ghbdsn_Nick хотів гавкнуть: будь ласка", "b1")


async def test_reply_to_unknown_message_uses_twitch_reply_body(bot):
    reply = {"parent_message_id": "old", "parent_user_login": "anna", "parent_user_name": "Anna", "parent_message_body": "lzre. @pipa_fan"}
    await send(bot, ev("@anna !бля", mid="c2", fragments=[{"type": "mention", "text": "@anna"}, {"type": "text", "text": " !бля"}], reply=reply))
    assert bot.out[-1] == ("Anna хотів гавкнуть: дякую @pipa_fan", "old")


async def test_emotes_and_mentions_inside_are_kept(bot):
    frags = [{"type": "text", "text": "ghbdsn "}, {"type": "mention", "text": "@Zr_Cghfdb"},
             {"type": "text", "text": " "}, {"type": "emote", "text": "pipaboLove"}, {"type": "text", "text": " zr cghfdb&"}]
    await send(bot, ev("ghbdsn @Zr_Cghfdb pipaboLove zr cghfdb&", mid="d1", fragments=frags))
    await send(bot, ev("!бля", mid="d2"))
    assert bot.out[-1][0] == "Pipa_Fan хотів гавкнуть: привіт @Zr_Cghfdb pipaboLove як справи?"


async def test_by_nick_and_inline_text(bot):
    await send(bot, ev("ckfdf erhf]ys", login="anna", name="Anna", mid="e1", uid="5"))
    await send(bot, ev("!бля @Anna", mid="e2"))
    assert bot.out[-1] == ("Anna хотів гавкнуть: слава україні", "e1")
    await send(bot, ev("!бля ghbdsn", mid="e3"))
    assert bot.out[-1] == ("Pipa_Fan хотів гавкнуть: привіт", "e3")


async def test_nothing_to_fix(bot):
    await send(bot, ev("!бля", mid="f1"))
    assert "не бачу" in bot.out[-1][0]
    await send(bot, ev("усе нормально", mid="f2"))
    await send(bot, ev("!бля", mid="f3"))
    assert bot.out[-1] == ("у Pipa_Fan і так усе зрозуміло 🐶", "f3")


async def test_old_messages_and_cooldown(monkeypatch):
    b = FakeBot()
    addon = install(b)
    await send(b, ev("ghbdsn", mid="g1"))
    addon.last["pipa_fan"].at -= blya.MAX_AGE + 1
    await send(b, ev("!бля", mid="g2"))
    assert "не бачу" in b.out[-1][0]
    n = len(b.out)
    await send(b, ev("!бля", mid="g3"))  # одразу вдруге — мовчимо, щоб не спамити
    assert len(b.out) == n


async def test_bot_own_messages_are_not_fixed(bot):
    reply = {"parent_message_id": "z1", "parent_user_login": "pipabobik_bot", "parent_user_name": "pipabobik_bot", "parent_message_body": "ghbdsn"}
    await send(bot, ev("!бля", mid="z2", reply=reply))
    assert bot.out == []


def test_main_command_table_is_untouched():
    from pipabobik.config import DEFAULTS

    assert not any(n in blya.NAMES for names in DEFAULTS["commands"].values() for n in names)
