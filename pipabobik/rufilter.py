"""Фільтр російської музики.

Три перевірки, від найдешевшої до найточнішої:
  1. мова назви: літери ы/э/ъ/ё або типово російські слова (без українських і/ї/є/ґ);
  2. вбудований список російських виконавців (кирилицею та латиницею) + ваш список із config.toml;
  3. країна виконавця з відкритої бази MusicBrainz (кешується в data/artist_country.json).

Фільтр евристичний: щось може проскочити (наприклад, назва транслітом у маловідомого
виконавця) або рідко спрацювати зайве — тоді трек можна «Розбанити» в панелі,
і фільтр більше його не чіпатиме.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("pipabobik.rufilter")

RU_ONLY_LETTERS = set("ыэъёЫЭЪЁ")
UA_LETTERS = set("іїєґІЇЄҐ")
BY_LETTERS = set("ўЎ")  # білоруська теж має ы/э/ё — її не чіпаємо за мовою

# Слова, яких немає в українській (перевіряються лише якщо в тексті немає і/ї/є/ґ)
RU_WORDS = {
    "тебя", "меня", "себя", "любовь", "когда", "сегодня", "только", "нет", "что", "чтобы", "всегда",
    "никогда", "сердце", "зачем", "почему", "потому", "теперь", "снова", "ночь", "глаза", "жизнь",
    "детка", "пожалуйста", "спасибо", "привет", "пока", "девочка", "мальчик", "моей", "твоей", "кто",
    "она", "они", "мой", "твой", "будет", "если", "тоже", "очень", "сейчас", "здесь", "где", "надо",
    "любить", "хочешь", "знаешь", "можно", "нельзя", "солнце", "звезда", "дождь", "война", "лето",
    "небеса", "навсегда", "вместе", "ничего", "нужна", "нужен", "сказал",
}

# Російські виконавці (у нормалізованому вигляді: нижній регістр, ё→е).
# Короткі неоднозначні латинські назви (Basta, Aria, Face…) сюди не внесені — їх перевіряє MusicBrainz.
RU_ARTISTS = {
    # кирилицею
    "моргенштерн", "инстасамка", "егор крид", "баста", "тимати", "гуф", "кизару", "мот", "мияги",
    "эндшпиль", "хаммали", "наваи", "клава кока", "ленинград", "сергей шнуров", "кино", "виктор цой",
    "король и шут", "би-2", "земфира", "руки вверх", "сплин", "ддт", "ария", "мумий тролль",
    "филипп киркоров", "ольга бузова", "полина гагарина", "шаман", "дима билан", "сергей лазарев",
    "алла пугачева", "григорий лепс", "стас михайлов", "ирина аллегрова", "любэ", "сектор газа",
    "ласковый май", "мираж", "звери", "нервы", "ночные снайперы", "диана арбенина", "многоточие",
    "каста", "элджей", "федук", "мальбэк", "даня милохин", "ваня дмитриенко", "монеточка", "нойз мс",
    "оксимирон", "хаски", "фараон", "слава мэрлоу", "маркул", "рамиль", "зиверт", "валерия",
    "николай басков", "тату", "серебро", "винтаж", "градусы", "звонкий", "бьянка", "артур пирожков",
    "ак-47", "триагрутрика", "кровосток", "каспийский груз", "агата кристи", "наутилус помпилиус",
    "чайф", "алиса", "пикник", "аквариум", "борис гребенщиков", "машина времени", "сурганова",
    "лсп", "uma2rman", "ума турман", "мачете", "пицца", "банд'эрос", "ленинград", "дискотека авария",
    "иванушки international", "мельница", "кукрыниксы", "louna", "лауна", "тараканы",
    # латиницею
    "morgenshtern", "instasamka", "egor kreed", "timati", "kizaru", "miyagi",
    "endspiel", "hammali", "navai", "klava koka", "little big", "leningrad", "zemfira",
    "bi-2", "splean", "ddt", "mumiy troll", "philipp kirkorov", "olga buzova", "polina gagarina",
    "dima bilan", "sergey lazarev", "alla pugacheva", "grigory leps", "t.a.t.u.", 
    "serebro", "oxxxymiron", "pharaoh", "boulevard depo", "slava marlow", "markul", "ramil'",
    "zivert", "eldzhey", "feduk", "monetochka", "noize mc", "big baby tape", "gone.fludd", 
    "obladaet", "mayot", "platina", "og buda", "soda luv", "lil krystalll", "kasta", 
    "artur pirozhkov", "niletto", "dabro", "matrang", "tanir", "tyomcha", "mary gu", 
    "ic3peak", "shortparis", "aigel", "tatarka", "cream soda", "the hatters", "kis-kis", "anacondaz",
    "scally milano", "kai angel", "9mice", "lovv66", "toxi$", "uglystephan", "aarne", "bushido zho",
    "instasamka", "hensy", "xolidayboy", "macan", "mband", "the limba", "wildways",
}

# Області MusicBrainz, які означають Росію
RU_AREAS = {
    "russia", "moscow", "saint petersburg", "st. petersburg", "yekaterinburg", "novosibirsk", "kazan",
    "samara", "rostov-on-don", "ufa", "krasnodar", "nizhny novgorod", "chelyabinsk", "omsk", "perm",
    "voronezh", "volgograd", "krasnoyarsk", "tyumen", "irkutsk", "vladivostok", "khabarovsk",
    "izhevsk", "ulyanovsk", "saratov", "tolyatti", "yaroslavl", "tula", "kaliningrad", "sochi",
}

_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|\bfeat\.?|\bft\.?|\bx\b|\bх\b|\bи\b|\band\b)\s*", re.IGNORECASE)
_WORD_RE = re.compile(r"[а-яёіїєґ']+", re.IGNORECASE)


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е").strip()
    s = re.sub(r"\s+", " ", s)
    return s.removesuffix(" - topic").strip(" .")


def split_artists(artist: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(artist or "") if p and p.strip()]
    return parts or ([artist] if artist else [])


def russian_language(text: str) -> bool:
    if not text:
        return False
    chars = set(text)
    if chars & UA_LETTERS or chars & BY_LETTERS:
        return False
    if chars & RU_ONLY_LETTERS:
        return True
    low = text.lower()
    words = set(_WORD_RE.findall(low))
    if words & RU_WORDS:
        return True
    return "люблю тебя" in low


class RuFilter:
    def __init__(self, cache_path: Path, extra_artists: list[str] | None = None,
                 session: aiohttp.ClientSession | None = None):
        self.cache_path = cache_path
        self.session = session
        self.extra = {norm(a) for a in (extra_artists or []) if a}
        self.cache: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._last_req = 0.0
        self._offline_until = 0.0
        try:
            if cache_path.exists():
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def quick_check(self, title: str, artist: str, album: str = "") -> str | None:
        """Перевірки без інтернету: мова і список виконавців."""
        if russian_language(f"{title} {album}") or russian_language(artist):
            return "російською мовою"
        for name in [artist, *split_artists(artist)]:  # спершу повна назва («Король и Шут»), потім частини
            n = norm(name)
            if n in RU_ARTISTS or n in self.extra:
                return f"російський виконавець ({name})"
        return None

    async def check(self, title: str, artist: str, album: str = "") -> str | None:
        reason = self.quick_check(title, artist, album)
        if reason:
            return reason
        ua_text = bool(set(f"{title}{artist}") & UA_LETTERS)
        for name in split_artists(artist)[:3]:
            country = await self.country(name)
            if country == "RU" or (country == "SU" and not ua_text):
                return f"російський виконавець ({name})"
        return None

    async def country(self, name: str) -> str | None:
        key = norm(name)
        if not key:
            return None
        if key in self.cache:
            return self.cache[key] or None
        if not self.session or time.time() < self._offline_until:
            return None
        async with self._lock:
            if key in self.cache:
                return self.cache[key] or None
            wait = 1.1 - (time.time() - self._last_req)  # MusicBrainz дозволяє 1 запит на секунду
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req = time.time()
            try:
                async with self.session.get(
                    "https://musicbrainz.org/ws/2/artist/",
                    params={"query": f'artist:"{name}"', "fmt": "json", "limit": "5"},
                    headers={"User-Agent": "pipabobik/1.2 ( https://github.com/LoganBg23/pipabobik )"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        raise aiohttp.ClientError(f"HTTP {r.status}")
                    data = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                log.info("MusicBrainz недоступний (%s) — перевіряю лише за мовою і списком", e)
                self._offline_until = time.time() + 300
                return None
        country = self._pick_country(key, data.get("artists") or [])
        self.cache[key] = country or ""
        self._save()
        return country

    @staticmethod
    def _pick_country(key: str, artists: list[dict]) -> str | None:
        for a in artists:
            if int(a.get("score", 0)) < 90:
                continue
            names = [a.get("name", "")] + [al.get("name", "") for al in a.get("aliases") or []]
            if key not in {norm(n) for n in names}:
                continue
            c = (a.get("country") or "").upper()
            if c:
                return c
            for area_key in ("area", "begin-area"):
                area = norm((a.get(area_key) or {}).get("name", ""))
                if area in RU_AREAS:
                    return "RU"
                if area == "soviet union":
                    return "SU"
            return None
        return None
