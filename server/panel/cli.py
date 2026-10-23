#!/usr/bin/env python3
"""Консольное управление панелью (запускать от root на сервере).

  cli.py invite <логин> [--admin]   создать аккаунт или сбросить пароль и 2FA, выдать ссылку
  cli.py list                       показать пользователей
  cli.py delete <логин>             удалить пользователя вместе с его ключами
"""

import os
import shutil
import sys
import time

import core

PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL", "https://localhost:8444").rstrip("/")


def fix_owner():
    """База принадлежит пользователю панели, даже если команду запустили от root."""
    try:
        shutil.chown(core.DB_PATH, user="streampanel", group="streampanel")
    except (LookupError, PermissionError, OSError):
        pass


def cmd_invite(args):
    if not args or not core.USER_RE.fullmatch(args[0]):
        sys.exit("Логин: латиница нижнего регистра, цифры и _, от 3 до 24 символов.")
    username, role = args[0], "admin" if "--admin" in args[1:] else "user"
    db = core.connect()
    u = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if u:
        db.execute("""UPDATE users SET pw_hash=NULL, totp_secret=NULL, totp_enabled=0,
                      sess_ver=sess_ver+1, failed=0, locked_until=0 WHERE id=?""", (u["id"],))
        uid = u["id"]
        if "--admin" in args[1:]:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
    else:
        uid = db.execute("INSERT INTO users (username, role, created) VALUES (?,?,?)",
                         (username, role, time.time())).lastrowid
    token = core.make_invite(db, uid)
    core.audit(db, "cli", f"invite:{username}")
    db.commit()
    fix_owner()
    print(f"Ссылка для {username} (одноразовая, 24 часа):\n{PUBLIC_URL}/invite/{token}")


def cmd_list(_args):
    db = core.connect()
    for u in db.execute("SELECT * FROM users ORDER BY id"):
        state = "активен" if u["totp_enabled"] else "ждёт активации"
        print(f"{u['username']:<24} {u['role']:<6} {state}")


def cmd_delete(args):
    if not args:
        sys.exit("Укажи логин.")
    db = core.connect()
    db.execute("DELETE FROM users WHERE username=?", (args[0],))
    core.audit(db, "cli", f"deleted:{args[0]}")
    db.commit()
    core.touch_trigger()
    print("Удалено.")


COMMANDS = {"invite": cmd_invite, "list": cmd_list, "delete": cmd_delete}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])
