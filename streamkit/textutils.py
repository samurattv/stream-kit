"""Подготовка сообщений чата к озвучке: чистка, числа словами, смайлы, транслит, сленг."""

import html
import re

URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:ru|com|net|org|io|tv|me)\S*", re.I)
IRC_MSG_RE = re.compile(r"^:(?P<user>[^!]+)![^ ]+ PRIVMSG #[^ ]+ :(?P<text>.*)$")
ALLOWED_RE = re.compile(r"[^а-яёА-ЯЁa-zA-Z0-9 ,.!?\-:;]")
REPEAT_RE = re.compile(r"(.)\1{3,}")
LAUGH_RE = re.compile(r"(\w{2,4}?)\1{2,}")          # «хахахаха» -> «хаха»
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
PERCENT_RE = re.compile(r"(\d)\s*%")

TRANSLIT = {
    "shch": "щ", "sch": "щ", "sh": "ш", "ch": "ч", "zh": "ж", "ts": "ц", "kh": "х",
    "yo": "ё", "ya": "я", "yu": "ю", "ye": "е", "ck": "к",
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
    "y": "й", "z": "з",
}

WORD_MAP = {
    "gg": "джи джи", "wp": "дабл пи", "ez": "изи", "lol": "лол", "pog": "пог",
    "kekw": "кек", "omg": "о май гад", "gl": "джи эл", "hf": "хорошей игры",
    "afk": "афк", "brb": "скоро вернусь", "ty": "спасибо", "np": "не за что",
    "nice": "найс", "wow": "вау", "hi": "привет", "hello": "привет", "bye": "пока",
    "gj": "хорошая работа", "ggwp": "джи джи дабл пи", "noob": "нуб", "rip": "рип",
    "thx": "спасибо", "pls": "пожалуйста", "plz": "пожалуйста", "idk": "не знаю",
    "imo": "по-моему", "btw": "кстати", "ok": "окей", "okay": "окей", "yes": "да",
    "no": "нет", "good": "хорошо", "bad": "плохо", "clutch": "клатч", "ace": "эйс",
    "pentakill": "пентакилл", "baron": "барон", "dragon": "дракон", "mid": "мид",
    "adc": "эй ди си", "jungle": "джангл", "support": "саппорт", "top": "топ",
    "youtube": "ютуб", "twitch": "твич", "tiktok": "тикток", "telegram": "телеграм",
    "instagram": "инстаграм", "steam": "стим", "valorant": "валорант", "minecraft": "майнкрафт",
    "fortnite": "фортнайт", "dota": "дота", "csgo": "кс го", "cs2": "кс два", "discord": "дискорд",
    "apex": "апекс", "league": "лига", "legends": "легенд", "stream": "стрим", "streamer": "стример",
}

# Смайлы Twitch / 7TV / BTTV: латиницей они читались бы как «каппа каппа каппа»
EMOTES = {
    "kappa", "kappapride", "kappahd", "keepo", "pogchamp", "pogu", "poggers", "pogey", "poggerz",
    "lul", "lulw", "omegalul", "kekw", "kek", "lmao", "lmfao", "monkas", "monka", "sadge",
    "4head", "biblethump", "residentsleeper", "pepehands", "pepega", "pepelaugh", "clap",
    "widepeepohappy", "widepeepo", "catjam", "copium", "hypers", "kreygasm", "jebaited",
    "trihard", "wutface", "gachigasm", "ezy", "weirdchamp", "pausechamp", "aware", "madge",
    "peepohappy", "peeposad", "okayge", "pogslide", "gigachad", "doubt", "cmonbruh", "mrdestructoid",
    "dansgame", "failfish", "notlikethis", "pjsalt", "seemsgood", "smorc", "swiftrage", "tehepelo",
    "heyguys", "cooldoge", "dogkek", "feelsbadman", "feelsgoodman", "feelsstrongman", "forsen",
}
LAUGH_EMOTES = {"lul", "lulw", "omegalul", "kekw", "kek", "lmao", "lmfao", "pepelaugh", "dogkek"}
SAFE_BRANDS = {"youtube", "twitch", "discord", "iphone", "playstation", "xbox", "tiktok",
               "telegram", "instagram", "facebook", "whatsapp", "javascript", "github", "pubg",
               "valorant", "leagueoflegends", "minecraft", "fortnite", "overwatch", "dota"}
CAMEL_RE = re.compile(r"[A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*")

IGNORED_USERS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot"}


# ------------------------------------------------------------------ числа словами
_UNITS_M = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["ноль", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]
_GROUPS = [
    (10 ** 9, ("миллиард", "миллиарда", "миллиардов"), False),
    (10 ** 6, ("миллион", "миллиона", "миллионов"), False),
    (10 ** 3, ("тысяча", "тысячи", "тысяч"), True),
]


def _plural(n, forms):
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def _triplet(n, feminine):
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if 10 <= n <= 19:
        words.append(_TEENS[n - 10])
    else:
        if n >= 20:
            words.append(_TENS[n // 10])
            n %= 10
        if n:
            words.append((_UNITS_F if feminine else _UNITS_M)[n])
    return " ".join(words)


def num_to_words(n):
    """Целое число словами по-русски (мужской род): 1123 -> «одна тысяча сто двадцать три»."""
    if n == 0:
        return "ноль"
    if n >= 10 ** 12:                            # слишком длинное — читаем по цифрам
        return " ".join(_UNITS_M[int(d)] for d in str(n))
    parts = []
    for base, forms, feminine in _GROUPS:
        q, n = divmod(n, base)
        if q:
            parts.append(f"{_triplet(q, feminine)} {_plural(q, forms)}")
    if n:
        parts.append(_triplet(n, False))
    return " ".join(parts)


def _number_repl(m):
    raw = m.group(0)
    if "." in raw or "," in raw:
        whole, frac = re.split(r"[.,]", raw, maxsplit=1)
        return f"{num_to_words(int(whole))} и {num_to_words(int(frac))}" if len(frac) <= 3 else raw
    return num_to_words(int(raw))


def numbers_to_words(text):
    text = PERCENT_RE.sub(r"\1 процентов", text)
    return NUMBER_RE.sub(_number_repl, text)


# ------------------------------------------------------------------ смайлы
def handle_emotes(text, mode):
    """mode: laugh — смех вместо смехового смайла, остальные пропускаем; skip — пропускаем все;
    say — оставляем как есть (будут прочитаны транслитом)."""
    if mode == "say":
        return text
    out = []
    for word in text.split():
        bare = re.sub(r"[^A-Za-z0-9]", "", word)
        low = bare.lower()
        if low in LAUGH_EMOTES and mode == "laugh":
            out.append("ха ха")
        elif low in EMOTES:
            continue
        elif (len(bare) >= 5 and low not in SAFE_BRANDS and CAMEL_RE.fullmatch(bare)
              and sum(c.isupper() for c in bare) >= 2):
            continue                               # PogChamp, WideHardo и подобные
        else:
            out.append(word)
    return " ".join(out)


# ------------------------------------------------------------------ транслит и чистка
def translit(text):
    text = text.lower()
    out, i = [], 0
    while i < len(text):
        for size in (4, 3, 2, 1):
            chunk = text[i:i + size]
            if chunk in TRANSLIT:
                out.append(TRANSLIT[chunk])
                i += size
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def clean_message(text, numbers=True, emotes="laugh", max_chars=200):
    text = html.unescape(text)
    text = URL_RE.sub(" ссылка ", text)
    text = handle_emotes(text, emotes)
    if numbers:
        text = numbers_to_words(text)
    text = ALLOWED_RE.sub(" ", text)
    text = REPEAT_RE.sub(r"\1\1", text)
    text = LAUGH_RE.sub(r"\1\1", text)
    if re.search(r"[a-zA-Z]", text):
        text = translit(" ".join(WORD_MAP.get(w.lower(), w) for w in text.split()))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:                    # режем по границе слова
        text = text[:max_chars].rsplit(" ", 1)[0]
    return text
