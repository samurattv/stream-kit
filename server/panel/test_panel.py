"""Проверки панели: вход, 2FA, приглашения, защита форм и безопасность конфига nginx.

Запуск: PANEL_DATA=<пустая папка> COOKIE_SECURE=0 python -m unittest test_panel
"""

import os
import re
import tempfile
import time
import unittest

_tmp = tempfile.mkdtemp()
os.environ["PANEL_DATA"] = _tmp
os.environ["COOKIE_SECURE"] = "0"
os.environ["PANEL_PUBLIC_URL"] = "https://test.example:8444"
os.environ["RTMP_HOST"] = "1.2.3.4"
os.environ["RTMP_USERS_CONF"] = os.path.join(_tmp, "users.conf")

import apply as apply_mod  # noqa: E402
import app as app_mod  # noqa: E402
import core  # noqa: E402

PW = "correct horse battery staple"


def csrf_of(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class Base(unittest.TestCase):
    def setUp(self):
        for f in (core.TRIGGER_PATH, core.STATUS_PATH):
            if os.path.exists(f):
                os.remove(f)
        self.db = core.connect()
        self.addCleanup(self.db.close)
        for table in ("audit", "streams", "users"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()
        self.c = app_mod.app.test_client()

    def make_user(self, name="liza", role="user", activate=True):
        uid = self.db.execute("INSERT INTO users (username, role, created) VALUES (?,?,?)",
                              (name, role, time.time())).lastrowid
        token = core.make_invite(self.db, uid)
        self.db.commit()
        if activate:
            self.activate(token, name)
        return uid, token

    def activate(self, token, name):
        c = app_mod.app.test_client()
        page = c.get(f"/invite/{token}").get_data(as_text=True)
        r = c.post(f"/invite/{token}", data={"csrf": csrf_of(page), "password": PW, "password2": PW})
        self.assertEqual(r.status_code, 302)
        page = c.get(f"/invite/{token}").get_data(as_text=True)
        secret = re.search(r'class="mono">([A-Z2-7]{32})<', page).group(1)
        code = core._totp_at(secret, int(time.time() // 30))
        r = c.post(f"/invite/{token}", data={"csrf": csrf_of(page), "code": code})
        self.assertEqual(r.status_code, 302)
        self.secrets = getattr(self, "secrets", {})
        self.secrets[name] = secret

    def login(self, client, name="liza", password=PW, code=None, step_offset=0, fresh=True):
        # fresh=True: считаем, что прошло время и коды прошлых входов уже не мешают
        if fresh:
            self.db.execute("UPDATE users SET totp_last_step=0 WHERE username=?", (name,))
            self.db.commit()
        page = client.get("/login").get_data(as_text=True)
        if code is None:
            secret = self.secrets[name]
            code = core._totp_at(secret, int(time.time() // 30) + step_offset)
        return client.post("/login", data={"csrf": csrf_of(page), "username": name,
                                           "password": password, "code": code})


class AuthTests(Base):
    def test_no_access_without_login(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.get("/admin").status_code, 302)

    def test_login_ok_and_replay_blocked(self):
        self.make_user()
        r = self.login(self.c)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)
        # тот же код второй раз не принимается (защита от повтора)
        other = app_mod.app.test_client()
        r = self.login(other, fresh=False)
        self.assertEqual(r.status_code, 401)

    def test_wrong_password_and_unknown_user_look_the_same(self):
        self.make_user()
        a = self.login(self.c, password="wrong password here")
        b = self.login(app_mod.app.test_client(), name="ghost", code="123456")
        self.assertEqual(a.status_code, 401)
        strip = lambda r: re.sub(rb'value="[\w-]{40,}"', b"", r.get_data())
        self.assertEqual(strip(a), strip(b))

    def test_lockout_after_failures(self):
        self.make_user()
        for _ in range(core.LOCK_AFTER):
            self.login(app_mod.app.test_client(), password="bad password!!!")
        # даже верные данные не пускают, пока аккаунт заблокирован
        r = self.login(app_mod.app.test_client())
        self.assertEqual(r.status_code, 401)

    def test_user_cannot_open_admin(self):
        self.make_user()
        self.login(self.c)
        self.assertEqual(self.c.get("/admin").status_code, 404)

    def test_csrf_required(self):
        self.make_user()
        self.login(self.c)
        r = self.c.post("/keys", data={"yt": "abcd-efgh-ijkl"})
        self.assertEqual(r.status_code, 400)

    def test_foreign_origin_rejected(self):
        self.make_user()
        self.login(self.c)
        page = self.c.get("/").get_data(as_text=True)
        r = self.c.post("/rotate", data={"csrf": csrf_of(page)},
                        headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 400)

    def test_browser_origin_null_with_same_origin_is_allowed(self):
        # Firefox шлёт «Origin: null» из-за Referrer-Policy: no-referrer
        self.make_user()
        self.login(self.c)
        page = self.c.get("/").get_data(as_text=True)
        r = self.c.post("/rotate", data={"csrf": csrf_of(page)},
                        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 302)

    def test_cross_site_fetch_rejected(self):
        self.make_user()
        self.login(self.c)
        page = self.c.get("/").get_data(as_text=True)
        r = self.c.post("/rotate", data={"csrf": csrf_of(page)},
                        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 400)

    def test_invite_is_single_use_and_expires(self):
        _, token = self.make_user()
        self.assertEqual(app_mod.app.test_client().get(f"/invite/{token}").status_code, 404)
        uid, token2 = self.make_user("bob", activate=False)
        self.db.execute("UPDATE users SET invite_expires=? WHERE id=?", (time.time() - 1, uid))
        self.db.commit()
        self.assertEqual(app_mod.app.test_client().get(f"/invite/{token2}").status_code, 404)

    def test_short_password_rejected(self):
        uid, token = self.make_user("bob", activate=False)
        c = app_mod.app.test_client()
        page = c.get(f"/invite/{token}").get_data(as_text=True)
        r = c.post(f"/invite/{token}", data={"csrf": csrf_of(page), "password": "short",
                                             "password2": "short"})
        self.assertEqual(r.status_code, 400)

    def test_security_headers(self):
        r = self.c.get("/login")
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Cache-Control"], "no-store")


class KeysTests(Base):
    def setUp(self):
        super().setUp()
        self.make_user()
        self.login(self.c)

    def post_keys(self, **data):
        page = self.c.get("/").get_data(as_text=True)
        return self.c.post("/keys", data={"csrf": csrf_of(page), **data})

    def test_save_masks_and_triggers_apply(self):
        r = self.post_keys(yt="abcd-efgh-ijkl-mnop", tw="live_123456_AbCdEfGh")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(os.path.exists(core.TRIGGER_PATH))
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("…mnop", html)
        self.assertNotIn("abcd-efgh-ijkl-mnop", html)
        self.assertNotIn("live_123456_AbCdEfGh", html)

    def test_config_injection_rejected(self):
        for bad in ("x;exec_push rm -rf /;", "abc def ghi jkl", "a/b/c/d/e/f/g/h", "short",
                    "key\nexec_push foo", "k" * 200):
            r = self.post_keys(yt=bad)
            self.assertEqual(r.status_code, 400, bad)
        row = self.db.execute("SELECT yt_key FROM streams").fetchone()
        self.assertTrue(row is None or row["yt_key"] == "")

    def test_vk_needs_both_fields_and_is_validated(self):
        self.assertEqual(self.post_keys(vk_url="rtmp://ovsu.example.ru/input/").status_code, 400)
        self.assertEqual(self.post_keys(vk="abc123_def.456").status_code, 400)
        for bad_url in ("rtmps://a.b/c", "rtmp://a.b/c;exec_push x", "http://a.b/c",
                        "rtmp://a b/c", "rtmp://a.b/c\nx"):
            r = self.post_keys(vk_url=bad_url, vk="abc123_def.456")
            self.assertEqual(r.status_code, 400, bad_url)
        for bad_key in ("short", "has space in it", "key;exec_push", "a/b/c/d/e/f"):
            r = self.post_keys(vk_url="rtmp://ovsu.example.ru/input/", vk=bad_key)
            self.assertEqual(r.status_code, 400, bad_key)
        r = self.post_keys(vk_url="rtmp://ovsu.example.ru/input/", vk="1234567_abc.def-8")
        self.assertEqual(r.status_code, 302)
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("…ef-8", html)
        self.assertNotIn("1234567_abc.def-8", html)
        self.post_keys(clear_vk="1")
        row = self.db.execute("SELECT * FROM streams").fetchone()
        self.assertEqual((row["vk_url"], row["vk_key"]), ("", ""))

    def test_empty_field_keeps_old_key_and_clear_removes_it(self):
        self.post_keys(yt="abcd-efgh-ijkl-mnop")
        self.post_keys(yt="", tw="live_123456_AbCdEfGh")
        row = self.db.execute("SELECT * FROM streams").fetchone()
        self.assertEqual(row["yt_key"], "abcd-efgh-ijkl-mnop")
        self.post_keys(clear_yt="1")
        row = self.db.execute("SELECT * FROM streams").fetchone()
        self.assertEqual(row["yt_key"], "")

    def test_rotate_changes_token(self):
        self.c.get("/")
        old = self.db.execute("SELECT publish_token FROM streams").fetchone()[0]
        page = self.c.get("/").get_data(as_text=True)
        self.c.post("/rotate", data={"csrf": csrf_of(page)})
        new = self.db.execute("SELECT publish_token FROM streams").fetchone()[0]
        self.assertNotEqual(old, new)

    def test_password_change_logs_out_other_sessions(self):
        other = app_mod.app.test_client()
        self.login(other)
        self.assertEqual(other.get("/").status_code, 200)
        page = self.c.get("/password").get_data(as_text=True)
        secret = self.secrets["liza"]
        code = core._totp_at(secret, int(time.time() // 30) + 1)
        self.db.execute("UPDATE users SET totp_last_step=0")
        self.db.commit()
        new_pw = "another long passphrase 42"
        r = self.c.post("/password", data={"csrf": csrf_of(page), "old": PW, "password": new_pw,
                                           "password2": new_pw, "code": code})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        self.assertEqual(other.get("/").status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)


class AdminTests(Base):
    def test_admin_invites_and_deletes(self):
        self.make_user("boss", role="admin")
        self.login(self.c, "boss")
        page = self.c.get("/admin").get_data(as_text=True)
        r = self.c.post("/admin/invite", data={"csrf": csrf_of(page), "username": "lisa"})
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.get_data(as_text=True), r"https://test\.example:8444/invite/[\w-]{40,}")
        # в базе только хеш приглашения
        row = self.db.execute("SELECT invite_hash FROM users WHERE username='lisa'").fetchone()
        self.assertEqual(len(row[0]), 64)
        page = self.c.get("/admin").get_data(as_text=True)
        r = self.c.post("/admin/invite", data={"csrf": csrf_of(page), "username": "../etc"})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.db.execute("SELECT 1 FROM users WHERE username='../etc'").fetchone())

    def test_admin_cannot_reset_self(self):
        self.make_user("boss", role="admin")
        self.login(self.c, "boss")
        page = self.c.get("/admin").get_data(as_text=True)
        r = self.c.post("/admin/invite", data={"csrf": csrf_of(page), "username": "boss"})
        self.assertEqual(r.status_code, 400)


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_vk_columns(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE streams (user_id INTEGER PRIMARY KEY, publish_token TEXT NOT NULL,
                yt_key TEXT NOT NULL DEFAULT '', tw_key TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL);
            INSERT INTO streams VALUES (1, 'tok', 'yt', 'tw', 0);""")
        old.commit()
        old.close()
        db = core.connect(path)
        row = db.execute("SELECT * FROM streams").fetchone()
        self.assertEqual((row["yt_key"], row["vk_url"], row["vk_key"]), ("yt", "", ""))
        db.close()


class ApplyTests(Base):
    def test_render_is_safe_and_skips_inactive(self):
        uid, _ = self.make_user("liza")
        self.db.execute("INSERT INTO streams (user_id, publish_token, yt_key, tw_key, updated)"
                        " VALUES (?,?,?,?,0)", (uid, "a" * 24, "abcd-efgh-ijkl", "live_1_xyzxyzxyz"))
        # подделанные данные в базе (как будто панель взломали): должны быть отброшены
        bad = self.db.execute("INSERT INTO users (username, totp_enabled, created) VALUES (?,1,0)",
                              ("evil", )).lastrowid
        self.db.execute("INSERT INTO streams (user_id, publish_token, yt_key, tw_key, updated)"
                        " VALUES (?,?,?,?,0)", (bad, "b" * 24, "x; exec_push calc", "ok_key_12345"))
        pending = self.db.execute("INSERT INTO users (username, created) VALUES ('pending', 0)").lastrowid
        self.db.execute("INSERT INTO streams (user_id, publish_token, yt_key, updated)"
                        " VALUES (?,?,?,0)", (pending, "c" * 24, "abcd-efgh-ijkl"))
        self.db.commit()
        self.db.execute("UPDATE streams SET vk_url='rtmp://ovsu.example.ru/input/', "
                        "vk_key='1234567_abc.def' WHERE user_id=?", (uid,))
        self.db.execute("UPDATE streams SET vk_url='rtmp://x.y/z;exec_push calc', "
                        "vk_key='1234567_abc' WHERE user_id=?", (bad,))
        self.db.commit()
        out = apply_mod.render(core.connect(readonly=True))
        self.assertIn("push rtmp://ovsu.example.ru/input/1234567_abc.def;", out)
        self.assertEqual(out.count("push rtmp://ovsu"), 1)
        self.assertIn("application u_" + "a" * 24, out)
        self.assertIn("push rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl;", out)
        self.assertIn("deny play all;", out)
        self.assertNotIn("exec_push", out)
        self.assertNotIn("x;", out)
        self.assertIn("push rtmp://live.twitch.tv/app/ok_key_12345;", out)
        self.assertNotIn("c" * 24, out)


if __name__ == "__main__":
    unittest.main()
