"""Веб-панель StreamKit: стример сам вводит свои ключи YouTube и Twitch."""

import os
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (Flask, abort, g, redirect, render_template, request, session,
                   url_for)

import core

def load_secret():
    path = os.environ.get("PANEL_SECRET_FILE")
    if not path:
        # без файла секрета (например, в тестах) ключ случайный и живёт до перезапуска
        return secrets.token_bytes(32)
    try:
        # O_EXCL: если два процесса стартуют одновременно, секрет создаст только один
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    for _ in range(30):
        with open(path, "rb") as f:
            data = f.read().strip()
        if data:
            return data
        time.sleep(0.1)
    raise RuntimeError("не удалось прочитать секрет сессий")


COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret(),
    SESSION_COOKIE_NAME="__Host-sk" if COOKIE_SECURE else "sk",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=16 * 1024,
)

# перед панелью стоит только наш nginx, он перезаписывает X-Forwarded-For
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL", "https://localhost:8444").rstrip("/")
RTMP_HOST = os.environ.get("RTMP_HOST", "localhost")


# ---------------------------------------------------------------- служебное
def db():
    if "db" not in g:
        g.db = core.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.commit()
        d.close()


def client_ip():
    return request.remote_addr or ""


@app.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'")
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.filters["ts"] = lambda t: time.strftime("%d.%m %H:%M", time.localtime(t or 0))


@app.before_request
def csrf_protect():
    if request.method != "POST":
        return
    sent = request.form.get("csrf", "")
    have = session.get("csrf", "")
    if not have or not secrets.compare_digest(sent, have):
        abort(400)
    # второй рубеж: запрос должен идти с нашего же сайта. Браузер сам ставит Sec-Fetch-Site;
    # при Referrer-Policy: no-referrer Firefox шлёт «Origin: null», это не подделка.
    site = request.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        abort(400)
    origin = request.headers.get("Origin")
    if origin and origin != "null" and urlparse(origin).netloc != request.host:
        abort(400)


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["sess_ver"] != session.get("ver") or not u["pw_hash"] or not u["totp_enabled"]:
        session.clear()
        return None
    return u


def login_required(admin=False):
    def deco(fn):
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login"))
            if admin and u["role"] != "admin":
                abort(404)
            g.user = u
            return fn(*a, **kw)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def start_session(user):
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["ver"] = user["sess_ver"]
    csrf_token()


def password_problem(pw, pw2):
    if pw != pw2:
        return "Пароли не совпадают."
    if len(pw) < core.MIN_PASSWORD:
        return f"Пароль слишком короткий: нужно минимум {core.MIN_PASSWORD} символов."
    if len(pw) > 200:
        return "Пароль слишком длинный."
    return None


# ---------------------------------------------------------------- вход
@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("home"))
    return render_template("login.html", error=None)


@app.post("/login")
def login_post():
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")[:200]
    code = re.sub(r"\s", "", request.form.get("code", ""))
    u = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    now = time.time()
    locked = bool(u and u["locked_until"] > now)

    pw_ok = core.verify_password(password, u["pw_hash"] if u and u["pw_hash"]
                                 else core.DUMMY_HASH)
    step = None
    if u and u["totp_enabled"]:
        step = core.totp_check(u["totp_secret"], code, u["totp_last_step"])

    if u and u["pw_hash"] and pw_ok and step and not locked:
        db().execute("UPDATE users SET failed=0, totp_last_step=? WHERE id=?", (step, u["id"]))
        core.audit(db(), username, "login_ok", client_ip())
        start_session(u)
        return redirect(url_for("home"))

    if u:
        failed = u["failed"] + 1
        if failed >= core.LOCK_AFTER:
            db().execute("UPDATE users SET failed=0, locked_until=? WHERE id=?",
                         (now + core.LOCK_SECONDS, u["id"]))
        else:
            db().execute("UPDATE users SET failed=? WHERE id=?", (failed, u["id"]))
    core.audit(db(), username[:24], "login_fail", client_ip())
    # одинаковый ответ на любую ошибку, чтобы не выдавать, что именно неверно
    return render_template("login.html",
                           error="Неверные данные или аккаунт временно заблокирован."), 401


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- приглашение
def invite_user(token):
    if not token or len(token) > 100:
        return None
    u = db().execute("SELECT * FROM users WHERE invite_hash=?",
                     (core.hash_invite(token),)).fetchone()
    if not u or (u["invite_expires"] or 0) < time.time():
        return None
    return u


@app.get("/invite/<token>")
def invite(token):
    u = invite_user(token)
    if not u:
        abort(404)
    if u["pw_hash"]:
        return render_template("invite_2fa.html", u=u, token=token, error=None,
                               secret=u["totp_secret"],
                               uri=core.totp_uri(u["totp_secret"], u["username"]))
    return render_template("invite_pw.html", u=u, token=token, error=None)


@app.post("/invite/<token>")
def invite_post(token):
    u = invite_user(token)
    if not u:
        abort(404)

    if not u["pw_hash"]:
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        problem = password_problem(pw, pw2)
        if problem:
            return render_template("invite_pw.html", u=u, token=token, error=problem), 400
        db().execute("UPDATE users SET pw_hash=?, totp_secret=?, totp_enabled=0 WHERE id=?",
                     (core.hash_password(pw), core.new_totp_secret(), u["id"]))
        return redirect(url_for("invite", token=token))

    code = re.sub(r"\s", "", request.form.get("code", ""))
    step = core.totp_check(u["totp_secret"], code, 0)
    if not step:
        return render_template("invite_2fa.html", u=u, token=token, secret=u["totp_secret"],
                               uri=core.totp_uri(u["totp_secret"], u["username"]),
                               error="Код не подошёл. Проверь время на телефоне и попробуй ещё раз."), 400
    db().execute("""UPDATE users SET totp_enabled=1, totp_last_step=?, invite_hash=NULL,
                    invite_expires=NULL, sess_ver=sess_ver+1 WHERE id=?""", (step, u["id"]))
    core.audit(db(), u["username"], "account_activated", client_ip())
    fresh = db().execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    start_session(fresh)
    return redirect(url_for("home"))


# ---------------------------------------------------------------- кабинет
def read_status():
    try:
        with open(core.STATUS_PATH, encoding="utf-8") as f:
            state, ts, *msg = f.read().strip().split(" ", 2)
        return state, float(ts), (msg[0] if msg else "")
    except (OSError, ValueError):
        return None, 0, ""


def get_stream(user_id):
    s = db().execute("SELECT * FROM streams WHERE user_id=?", (user_id,)).fetchone()
    if not s:
        db().execute("INSERT INTO streams (user_id, publish_token, updated) VALUES (?,?,?)",
                     (user_id, core.new_token(), time.time()))
        s = db().execute("SELECT * FROM streams WHERE user_id=?", (user_id,)).fetchone()
    return s


@app.get("/")
@login_required()
def home():
    s = get_stream(g.user["id"])
    state, ts, msg = read_status()
    applied = state == "ok" and ts >= s["updated"]
    return render_template("home.html", u=g.user, s=s, yt=core.mask(s["yt_key"]),
                           tw=core.mask(s["tw_key"]), vk=core.mask(s["vk_key"]),
                           rtmp_host=RTMP_HOST,
                           applied=applied, apply_failed=(state == "error" and ts >= s["updated"]),
                           apply_msg=msg, notice=request.args.get("ok"), error=None)


def keys_error(s, message):
    return render_template(
        "home.html", u=g.user, s=s, yt=core.mask(s["yt_key"]), tw=core.mask(s["tw_key"]),
        vk=core.mask(s["vk_key"]), rtmp_host=RTMP_HOST, applied=False, apply_failed=False,
        apply_msg="", notice=None, error=message), 400


@app.post("/keys")
@login_required()
def keys():
    s = get_stream(g.user["id"])
    new = {"yt_key": s["yt_key"], "tw_key": s["tw_key"],
           "vk_url": s["vk_url"], "vk_key": s["vk_key"]}
    if request.form.get("clear_vk"):
        new["vk_url"] = new["vk_key"] = ""
    vk_url = request.form.get("vk_url", "").strip()
    if vk_url:
        if not core.VK_URL_RE.fullmatch(vk_url):
            return keys_error(s, "Адрес сервера VK должен выглядеть как rtmp://адрес/путь, "
                                 "без пробелов и лишних символов.")
        new["vk_url"] = vk_url
    for field, form, clear in (("yt_key", "yt", "clear_yt"), ("tw_key", "tw", "clear_tw"),
                               ("vk_key", "vk", None)):
        if clear and request.form.get(clear):
            new[field] = ""
            continue
        value = request.form.get(form, "").strip()
        if value:
            pattern = core.VK_KEY_RE if field == "vk_key" else core.KEY_RE
            if not pattern.fullmatch(value):
                return keys_error(s, "Ключ выглядит неверно: только буквы, цифры и знаки «-», «_» "
                                     "(для VK ещё «.»), от 6 до 120 символов. Скопируй его целиком.")
            new[field] = value
    if bool(new["vk_url"]) != bool(new["vk_key"]):
        return keys_error(s, "Для VK Live нужны и адрес сервера, и ключ потока.")
    db().execute("""UPDATE streams SET yt_key=?, tw_key=?, vk_url=?, vk_key=?, updated=?
                    WHERE user_id=?""",
                 (new["yt_key"], new["tw_key"], new["vk_url"], new["vk_key"], time.time(),
                  g.user["id"]))
    core.audit(db(), g.user["username"], "keys_changed", client_ip())
    db().commit()
    core.touch_trigger()
    return redirect(url_for("home", ok="keys"))


@app.post("/rotate")
@login_required()
def rotate():
    get_stream(g.user["id"])
    db().execute("UPDATE streams SET publish_token=?, updated=? WHERE user_id=?",
                 (core.new_token(), time.time(), g.user["id"]))
    core.audit(db(), g.user["username"], "token_rotated", client_ip())
    db().commit()
    core.touch_trigger()
    return redirect(url_for("home", ok="rotate"))


@app.get("/password")
@login_required()
def password():
    return render_template("password.html", error=None)


@app.post("/password")
@login_required()
def password_post():
    u = g.user
    old = request.form.get("old", "")[:200]
    code = re.sub(r"\s", "", request.form.get("code", ""))
    step = core.totp_check(u["totp_secret"], code, u["totp_last_step"])
    problem = password_problem(request.form.get("password", ""),
                               request.form.get("password2", ""))
    if not core.verify_password(old, u["pw_hash"]) or not step:
        problem = "Текущий пароль или код неверны."
    if problem:
        return render_template("password.html", error=problem), 400
    db().execute("UPDATE users SET pw_hash=?, totp_last_step=?, sess_ver=sess_ver+1 WHERE id=?",
                 (core.hash_password(request.form["password"]), step, u["id"]))
    core.audit(db(), u["username"], "password_changed", client_ip())
    db().commit()
    fresh = db().execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    start_session(fresh)  # остальные сессии этого аккаунта сразу перестают работать
    return redirect(url_for("home", ok="password"))


# ---------------------------------------------------------------- админ
def render_admin(link=None, link_user=None):
    users = db().execute("""SELECT u.*, s.yt_key <> '' AS has_yt, s.tw_key <> '' AS has_tw
                            FROM users u LEFT JOIN streams s ON s.user_id=u.id
                            ORDER BY u.id""").fetchall()
    log = db().execute("SELECT * FROM audit ORDER BY ts DESC LIMIT 30").fetchall()
    return render_template("admin.html", users=users, log=log, link=link,
                           link_user=link_user)


@app.get("/admin")
@login_required(admin=True)
def admin():
    return render_admin()


@app.post("/admin/invite")
@login_required(admin=True)
def admin_invite():
    username = request.form.get("username", "").strip().lower()
    if not core.USER_RE.fullmatch(username):
        return redirect(url_for("admin"))
    u = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if u and u["id"] == g.user["id"]:
        abort(400)  # себя через панель не сбросить, иначе можно остаться без входа
    if u:
        # повторное приглашение = сброс пароля и 2FA (потеряла телефон и т. п.)
        db().execute("""UPDATE users SET pw_hash=NULL, totp_secret=NULL, totp_enabled=0,
                        sess_ver=sess_ver+1, failed=0, locked_until=0 WHERE id=?""", (u["id"],))
        uid = u["id"]
    else:
        uid = db().execute("INSERT INTO users (username, created) VALUES (?,?)",
                           (username, time.time())).lastrowid
    token = core.make_invite(db(), uid)
    core.audit(db(), g.user["username"], f"invite:{username}", client_ip())
    db().commit()
    return render_admin(link=f"{PUBLIC_URL}/invite/{token}", link_user=username)


@app.post("/admin/delete/<int:uid>")
@login_required(admin=True)
def admin_delete(uid):
    if uid == g.user["id"]:
        abort(400)
    u = db().execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    if u:
        db().execute("DELETE FROM users WHERE id=?", (uid,))
        core.audit(db(), g.user["username"], f"deleted:{u['username']}", client_ip())
        db().commit()
        core.touch_trigger()
    return redirect(url_for("admin"))
