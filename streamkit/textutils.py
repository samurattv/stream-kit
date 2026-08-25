"""Подготовка сообщений чата к озвучке: чистка, транслит, замена сленга."""

import html
import re

URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:ru|com|net|org|io|tv|me)\S*", re.I)
IRC_MSG_RE = re.compile(r"^:(?P<user>[^!]+)![^ ]+ PRIVMSG #[^ ]+ :(?P<text>.*)$")
ALLOWED_RE = re.compile(r"[^а-яёА-ЯЁa-zA-Z0-9 ,.!?\-:;]")
REPEAT_RE = re.compile(r"(.)\1{3,}")

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
}

IGNORED_USERS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot"}


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


def clean_message(text):
    text = html.unescape(text)
    text = URL_RE.sub(" ссылка ", text)
    text = ALLOWED_RE.sub(" ", text)
    text = REPEAT_RE.sub(r"\1\1", text)
    if re.search(r"[a-zA-Z]", text):
        text = translit(" ".join(WORD_MAP.get(w.lower(), w) for w in text.split()))
    return re.sub(r"\s+", " ", text).strip()[:250]


IGNORED_USERS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot"}
