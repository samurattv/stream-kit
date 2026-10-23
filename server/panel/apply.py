#!/usr/bin/env python3
"""Собирает конфиг nginx-rtmp из базы панели. Запускается от root по сигналу от панели.

Панель сама работает без прав root и конфиг не трогает. Здесь содержимое базы
считается недоверенным: каждое значение проверяется заново, а при ошибке nginx -t
прежний конфиг возвращается на место.
"""

import os
import subprocess
import sys
import time

import core

OUT = os.environ.get("RTMP_USERS_CONF", "/etc/nginx/rtmp.d/users.conf")
RELOAD_CMD = os.environ.get("RELOAD_CMD", "nginx -s reload").split()

APP = """application u_{token} {{
    live on;
    record off;
    # смотреть поток снаружи нельзя, только публиковать
    allow play 127.0.0.1;
    deny play all;
    # {username}
{pushes}}}
"""


def render(db):
    out = ["# Файл создан автоматически (apply.py), руками не править.\n"]
    for row in db.execute("""SELECT u.username, s.publish_token, s.yt_key, s.tw_key
                             FROM users u JOIN streams s ON s.user_id = u.id
                             WHERE u.totp_enabled = 1 ORDER BY u.id"""):
        name, token, yt, tw = row["username"], row["publish_token"], row["yt_key"], row["tw_key"]
        if not (core.USER_RE.fullmatch(name) and core.TOKEN_RE.fullmatch(token)):
            continue
        pushes = ""
        if yt and core.KEY_RE.fullmatch(yt):
            pushes += f"    push rtmp://a.rtmp.youtube.com/live2/{yt};\n"
        if tw and core.KEY_RE.fullmatch(tw):
            pushes += f"    push rtmp://live.twitch.tv/app/{tw};\n"
        out.append(APP.format(token=token, username=name, pushes=pushes))
    return "\n".join(out)


def write_status(ok, msg=""):
    tmp = core.STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"{'ok' if ok else 'error'} {time.time()} {msg}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, core.STATUS_PATH)


def main():
    if not os.path.isfile(core.DB_PATH):
        # первый запуск: панель ещё не создала базу, применять нечего
        write_status(True)
        return 0
    db = core.connect(readonly=True)
    new = render(db)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    old = None
    if os.path.isfile(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = f.read()

    if old != new:
        fd = os.open(OUT + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(OUT + ".tmp", OUT)

        test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if test.returncode != 0:
            # откат: возвращаем предыдущий рабочий конфиг
            if old is None:
                os.remove(OUT)
            else:
                with open(OUT, "w", encoding="utf-8") as f:
                    f.write(old)
            write_status(False, "nginx -t не прошёл, конфиг откатан")
            print(test.stderr, file=sys.stderr)
            return 1
        if subprocess.run(RELOAD_CMD).returncode != 0:
            write_status(False, "reload nginx не удался")
            return 1
    write_status(True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
