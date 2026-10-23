"""Общее для панели, CLI и скрипта применения: база, пароли, 2FA, проверка ключей."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import struct
import time

DATA_DIR = os.environ.get("PANEL_DATA", "/var/lib/streamkit-panel")
DB_PATH = os.path.join(DATA_DIR, "panel.db")
TRIGGER_PATH = os.path.join(DATA_DIR, "apply.trigger")
STATUS_PATH = os.path.join(DATA_DIR, "apply.status")

# Ключи попадают в конфиг nginx, поэтому только безопасные символы: без пробелов,
# точки с запятой, слэшей и кавычек.
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,80}$")
USER_RE = re.compile(r"^[a-z0-9_]{3,24}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{24}$")

MIN_PASSWORD = 12
LOCK_AFTER = 5
LOCK_SECONDS = 15 * 60
INVITE_TTL = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    pw_hash TEXT,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0,
    invite_hash TEXT,
    invite_expires REAL,
    sess_ver INTEGER NOT NULL DEFAULT 1,
    failed INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS streams (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    publish_token TEXT NOT NULL,
    yt_key TEXT NOT NULL DEFAULT '',
    tw_key TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    ts REAL NOT NULL,
    username TEXT,
    event TEXT NOT NULL,
    ip TEXT
);
"""


def connect(path=None, readonly=False):
    path = path or DB_PATH
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        db.executescript(SCHEMA)
    return db


def new_token():
    return secrets.token_hex(12)


def hash_invite(token):
    return hashlib.sha256(token.encode()).hexdigest()


def make_invite(db, user_id):
    """Одноразовая ссылка-приглашение. В базе только хеш, сам токен показывается один раз."""
    token = secrets.token_urlsafe(32)
    db.execute("UPDATE users SET invite_hash=?, invite_expires=? WHERE id=?",
               (hash_invite(token), time.time() + INVITE_TTL, user_id))
    return token


def touch_trigger():
    """Сигнал для apply.py: systemd видит файл, запускает применение и сам его удаляет."""
    try:
        with open(TRIGGER_PATH, "a"):
            pass
    except PermissionError:
        pass  # файл уже лежит (создан root-ом) и ждёт обработки


def audit(db, username, event, ip=""):
    now = time.time()
    db.execute("INSERT INTO audit VALUES (?,?,?,?)", (now, username, event, ip))
    db.execute("DELETE FROM audit WHERE ts < ?", (now - 90 * 86400,))


# ---------------------------------------------------------------- пароли
_N, _R, _P = 2 ** 15, 8, 1


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                        maxmem=128 * 1024 * 1024)
    return "scrypt${}${}".format(base64.b64encode(salt).decode(),
                                 base64.b64encode(dk).decode())


def verify_password(password, stored):
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt, want = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                             maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(got, want)
    except Exception:
        return False


# Для несуществующих логинов считаем хеш впустую, чтобы время ответа не выдавало, есть ли такой аккаунт.
DUMMY_HASH = hash_password("dummy-password-for-timing")


# ---------------------------------------------------------------- 2FA (RFC 6238)
def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _totp_at(secret, step):
    key = base64.b32decode(secret)
    h = hmac.new(key, struct.pack(">Q", step), "sha1").digest()
    o = h[-1] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_check(secret, code, last_step=0):
    """Возвращает номер шага, если код верный и не использовался раньше, иначе None."""
    if not secret or not re.fullmatch(r"\d{6}", code or ""):
        return None
    now = int(time.time() // 30)
    for step in (now - 1, now, now + 1):
        if step > last_step and hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None


def totp_uri(secret, username):
    return f"otpauth://totp/StreamKit:{username}?secret={secret}&issuer=StreamKit"


# ---------------------------------------------------------------- ключи
def mask(key):
    return ("…" + key[-4:]) if key else ""
