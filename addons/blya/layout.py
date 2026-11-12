"""Виправлення тексту, набраного не в тій розкладці (ЙЦУКЕН ↔ QWERTY).

«ghbdsn» → «привіт», «руддщ» → «hello». Кожне слово перевіряється окремо:
міняється лише те, що в іншій розкладці виглядає природніше. Згадки (@нік),
посилання, емоути й числа не чіпаються.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# Стандартна українська розкладка Windows: клавіша → літера
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,./`\\" + 'QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>?~|' + '@#$^&'
_UA = "йцукенгшщзхїфівапролджєячсмитьбю.'ґ" + "ЙЦУКЕНГШЩЗХЇФІВАПРОЛДЖЄЯЧСМИТЬБЮ,₴Ґ" + '"№;:?'
assert len(_EN) == len(_UA)
EN_TO_UA = dict(zip(_EN, _UA))
UA_TO_EN = {u: e for e, u in EN_TO_UA.items()}
# російська розкладка теж трапляється, коли хотіли писати англійською
UA_TO_EN.update({"ы": "s", "Ы": "S", "э": "'", "Э": '"', "ъ": "]", "Ъ": "}", "ё": "`", "Ё": "~"})

LATIN = re.compile(r"[A-Za-z]")
CYR = re.compile(r"[А-Яа-яЇїІіЄєҐґЁёЫыЭэЪъ]")
URL = re.compile(r"(https?://|www\.)|^[\w-]+(\.[\w-]+)*\.(com|net|org|ua|tv|gg|io|me|ly|be|ru)(/|$)", re.I)
MENTION = re.compile(r"^@[A-Za-z0-9_]{3,25}[,.!?:;]*$")
CAMEL = re.compile(r"[a-z][A-Z]")

# Популярні емоути Twitch / BTTV / 7TV — це не слова, їх лишаємо як є
EMOTES = {e.lower() for e in """
Kappa KappaPride Keepo Kreygasm PogChamp Pog PogU POGGERS LUL LULW OMEGALUL KEKW KEK ICANT xdd xd
monkaS monkaW monkaHmm Sadge PepeHands PepeLaugh FeelsBadMan FeelsGoodMan FeelsStrongMan 5Head 3Head
Clap EZ Copium COPIUM Aware Okayge Stare BASED Classic catJAM peepoHappy peepoSad widepeepoHappy
NotLikeThis BibleThump ResidentSleeper SeemsGood VoHiYo TriHard WutFace DansGame SMOrc HeyGuys
GG gg ggwp wp lol lmao rofl omg wtf brb afk gl hf ok okay xD XD D: ^^ owo uwu
""".split()}

# Короткі зразки мов для оцінки «наскільки природно звучить слово»
_UA_SAMPLE = """
привіт усім як справи сьогодні в нас дуже цікавий стрім будемо грати і слухати музику
дякую за підписку друже це було неймовірно круто я навіть не знаю що сказати
давай ще одну пісню будь ласка вона мені дуже подобається а ця трохи сумна
хто знає як пройти цей рівень бо я вже втретє помираю тут на цьому місці
доброго вечора пані та панове сідайте зручніше наливайте чай і поїхали далі
ну що ж почнемо з простого а потім подивимось що буде коли стане складніше
я не розумію чому він так зробив але мабуть у нього були свої причини
скільки ще чекати до кінця черги хочу почути свою пісню поки не пішов спати
можна поставити щось веселе для настрою бо надворі холодно і темно зранку
це найкраща гра яку я бачив за останній рік графіка просто шикарна
слава україні героям слава ми все зможемо разом якщо будемо підтримувати одне одного
завтра буде новий стрім о сьомій вечора не пропустіть бо буде щось особливе
мені здається що треба було йти ліворуч а не праворуч там був скарб
як тебе звати звідки ти пишеш у нас тут уже майже три години ночі
вибачте що запізнився затримався на роботі але тепер я з вами до кінця
обожнюю цю мелодію вона нагадує мені про літо море і пісок під ногами
їжак їхав їсти їжу з'їв яблуко пʼять пірʼя мʼясо м'ята ґудзик ґанок
чекаю на продовження бо історія затягує все більше і більше щодня
хочу купити нову клавіатуру бо ця вже стара і кнопки западають постійно
що відбувається чому все зламалося перезапусти будь ласка трансляцію
дякую всім хто прийшов сьогодні для всіх нових глядачів окреме вітання від усіх нас
вони сміються з наших жартів і це найкраще що могло статися з цим каналом
він усміхається коли бачить своїх друзів у чаті і завжди пише щось приємне
мені потрібно трохи часу щоб розібратися з налаштуваннями нового мікрофона
хлопці не сваріться тут усі свої і ніхто нікого не ображає зрозуміли
ти бачив цей момент коли він стрибнув через прірву і ледь не впав униз
вона співає краще за всіх кого я чув останнім часом просто мурашки по шкірі
коли наступна пісня скільки ще треків у черзі перед моїм замовленням
шкода що так вийшло але наступного разу обовʼязково вийде краще ніж сьогодні
підписуйтесь ставте вподобайки пишіть коментарі і діліться з друзями
чому ти мовчиш напиши щось у чат ми чекаємо на твою відповідь уже довго
нормально все гаразд не хвилюйся просто інтернет трохи підвисає сьогодні
"""
_EN_SAMPLE = """
hello everyone how are you doing today we have a really fun stream with games and music
thanks for the follow my friend that was amazing i do not even know what to say
please play one more song i really like this one but that one is a little sad
does anyone know how to beat this level because i keep dying in the same place
good evening ladies and gentlemen sit back grab some tea and let us get started
well then we will start with something simple and then see what happens later
i do not understand why he did that but maybe he had his own reasons for it
how long until the end of the queue i want to hear my song before going to bed
could you put on something cheerful because it is cold and dark outside today
this is the best game i have seen in the last year the graphics are just great
see you tomorrow at seven do not miss the next stream there will be a surprise
what is going on why is everything broken please restart the stream right now
nice play that was clutch good game well played love this channel so much
the quick brown fox jumps over the lazy dog while the weather keeps changing
i think we should have gone left instead of right there was treasure over there
what is your name where are you from it is almost three in the morning here
sorry i am late work took longer than expected but now i am here until the end
i love this tune it reminds me of summer the sea and sand under my feet
"""

_UA_WORDS = set("""
і й та а але що як це не так ні він вона воно вони ми ви я ти мені тобі його її їх нас вас
в у на з із до від за по про для без під над при через між бо чи вже ще теж тут там де коли
привіт дякую будь ласка добре погано так ні дуже круто клас супер давай пісня музика стрім
гра грати слухати хочу можна треба знаю думаю бачу люблю було буде є був була всім усім
всіх усіх нас вас їм ним ними тебе мене себе свій своя своє ось ну ага ок окей лол кек
""".split())
_EN_WORDS = set("""
a i the and or but is are was were be to of in on at for with from by it this that these those
you he she we they me my your his her our their what why how when where who yes no not do does
did have has had will would can could should hello hi hey thanks thank please good bad nice
great love like play song music stream game see go get now here there ok okay lol gg wp
""".split())


def _model(text: str) -> tuple[dict, dict, int]:
    big, uni = Counter(), Counter()
    for w in text.split():
        w = f"^{w}$"
        for a, b in zip(w, w[1:]):
            big[a + b] += 1
            uni[a] += 1
    return big, uni, len(set(text.replace(" ", "").replace("\n", ""))) + 2


_UA_MODEL = _model(_UA_SAMPLE)
_EN_MODEL = _model(_EN_SAMPLE)


def _score(word: str, model, vocab: set[str]) -> float:
    """Середній логарифм імовірності буквосполучень; більше — природніше."""
    letters = "".join(ch for ch in word.lower() if ch.isalpha() or ch in "'ʼ")
    letters = letters.replace("ʼ", "'")
    if not letters:
        return -99.0
    big, uni, v = model
    w = f"^{letters}$"
    total = 0.0
    for a, b in zip(w, w[1:]):
        total += math.log((big[a + b] + 0.1) / (uni[a] + 0.1 * v))
    s = total / (len(w) - 1)
    if letters in vocab:
        s += 2.5
    return s


def _translate(token: str, table: dict[str, str]) -> str:
    return "".join(table.get(ch, ch) for ch in token)


def _punct_only(token: str, table: dict[str, str]) -> str:
    """Токен без літер: міняємо лише знаки, що й після заміни лишаються знаками («&» → «?»)."""
    out = _translate(token, table)
    return token if any(ch.isalpha() for ch in out) else out


def _keep(token: str) -> bool:
    bare = token.strip(".,!?:;()\"'")
    return (
        not token
        or URL.search(token) is not None
        or MENTION.match(token) is not None
        or token.startswith("#")
        or bare.lower() in EMOTES
        or CAMEL.search(bare) is not None  # catJAM, peepoHappy — емоути, не слова
    )


CONTRACTION = re.compile(r"[A-Za-z]'(t|s|re|ll|ve|d|m)\b", re.I)


def _layout_marks(token: str) -> int:
    """Скільки в латинському слові знаків, які в українській розкладці — літери (х ї ж є б ю ').

    «ecs[» чи «[jxe» англійською не пишуть — це майже напевно «усіх», «хочу».
    Знаки в кінці слова не рахуємо: це може бути звичайна кома чи крапка.
    """
    core = token.rstrip(",.?!")
    n = sum(core.count(c) for c in "[];`")
    n += sum(1 for i, ch in enumerate(core) if ch in ",." and 0 < i)
    if "'" in core and not CONTRACTION.search(core):
        n += core.count("'")
    return n


def _variants(token: str) -> list[str]:
    """Кома чи крапка в кінці: це «б»/«ю», чи звичайний знак? Перевіряємо обидва варіанти."""
    conv = _translate(token, EN_TO_UA)
    m = re.search(r"[,.]+$", token)
    if m and m.start() > 0:
        return [conv, _translate(token[: m.start()], EN_TO_UA) + token[m.start():]]
    return [conv]


def _decide(token: str) -> tuple[str, float] | None:
    """Варіант заміни для слова й наскільки він кращий за оригінал (>0 — кращий)."""
    lat, cyr = len(LATIN.findall(token)), len(CYR.findall(token))
    if lat and lat >= cyr:
        conv = max(_variants(token), key=lambda c: _score(c, _UA_MODEL, _UA_WORDS))
        gain = _score(conv, _UA_MODEL, _UA_WORDS) - _score(token, _EN_MODEL, _EN_WORDS)
        return conv, gain + 1.5 * _layout_marks(token)
    if cyr:
        conv = _translate(token, UA_TO_EN)
        return conv, _score(conv, _EN_MODEL, _EN_WORDS) - _score(token, _UA_MODEL, _UA_WORDS)
    return None


STRONG = 0.6  # наскільки має бути краще, щоб слово точно замінити


def fix_text(text: str) -> tuple[str, bool]:
    """Повертає (виправлений текст, чи щось змінилося). Пробіли зберігаються."""
    parts = re.split(r"(\s+)", text)
    cand: dict[int, tuple[str, float]] = {}
    for i, tok in enumerate(parts):
        if i % 2 or _keep(tok):
            continue
        d = _decide(tok)
        if d:
            cand[i] = d
    if not cand:
        return text, False
    strong = [gain for _, gain in cand.values() if abs(gain) >= STRONG]
    # короткі слова на кшталт «z», «ns» вирішуємо за більшістю впевнених
    majority = sum(1 for g in strong if g > 0) > len(strong) / 2 if strong else max(g for _, g in cand.values()) > 0
    out = list(parts)
    changed = False
    for i, (conv, gain) in cand.items():
        if gain >= STRONG or (abs(gain) < STRONG and majority and gain > -STRONG):
            out[i] = conv
            changed = changed or conv != parts[i]
    if changed:
        # окремі знаки поруч із виправленими словами теж були набрані не в тій розкладці
        direction = EN_TO_UA if any(LATIN.search(parts[i]) and out[i] != parts[i] for i in cand) else UA_TO_EN
        for i, tok in enumerate(parts):
            if i % 2 == 0 and i not in cand and tok and not _keep(tok) and not any(ch.isalpha() for ch in tok):
                out[i] = _punct_only(tok, direction)
    return "".join(out), changed
