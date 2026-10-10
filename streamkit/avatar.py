"""Аватар-енот: отдаёт страницу с енотом и пересылает ей смену одежды.

Страница (streamkit/avatar/index.html) сама следит за лицом через камеру.
StreamKit поднимает два локальных сервера: HTTP со страницей и WebSocket,
по которому приходят команды вида {"look": {"hat": "beanie"}}.
"""

import functools
import http.server
import json
import os
import threading
import time

from .workers import Worker

AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatar")

# должны совпадать со списками в index.html
HOODIES = ["indigo", "red", "green", "orange", "black"]
HATS = ["beanie", "cap", "crown"]
GLASSES = ["round", "sun"]
DEFAULT_LOOK = {"hoodie": "indigo", "hat": "none", "glasses": "none", "scarf": False}

ALIASES = {
    "индиго": "indigo", "синее": "indigo", "синий": "indigo",
    "красное": "red", "красный": "red",
    "зелёное": "green", "зеленое": "green", "зелёный": "green", "зеленый": "green",
    "оранжевое": "orange", "оранжевый": "orange",
    "чёрное": "black", "черное": "black", "чёрный": "black", "черный": "black",
    "бини": "beanie", "шапка": "beanie", "шапку": "beanie",
    "кепка": "cap", "кепку": "cap", "бейсболка": "cap",
    "корона": "crown", "корону": "crown",
    "круглые": "round", "очки": "round",
    "тёмные": "sun", "темные": "sun", "солнцезащитные": "sun",
}
OFF_WORDS = {"снять", "нет", "без", "off", "none", "убрать", "долой"}
RESET_WORDS = {"сброс", "reset", "обычный", "по-умолчанию"}

# слово команды -> что меняем
COMMANDS = {
    "шапка": "hat", "hat": "hat", "кепка": "hat", "cap": "hat",
    "очки": "glasses", "glasses": "glasses",
    "худи": "hoodie", "hoodie": "hoodie",
    "шарф": "scarf", "scarf": "scarf",
    "образ": "reset", "reset": "reset",
}
OPTIONS = {"hat": HATS, "glasses": GLASSES, "hoodie": HOODIES}


def normalize_look(look):
    out = dict(DEFAULT_LOOK)
    if isinstance(look, dict):
        for key, value in look.items():
            if key == "hoodie" and value in HOODIES:
                out[key] = value
            elif key == "hat" and (value == "none" or value in HATS):
                out[key] = value
            elif key == "glasses" and (value == "none" or value in GLASSES):
                out[key] = value
            elif key == "scarf":
                out[key] = bool(value)
    return out


def parse_command(text, look):
    """Разбирает чат-команду. Возвращает (патч для look, подпись) или None."""
    parts = text.strip().lstrip("!").lower().replace("ё", "е").split()
    if not parts or parts[0] not in {k.replace("ё", "е") for k in COMMANDS}:
        return None
    what = next(v for k, v in COMMANDS.items() if k.replace("ё", "е") == parts[0])
    arg = parts[1] if len(parts) > 1 else ""
    if parts[0] in ("кепка", "cap") and not arg:
        arg = "cap"              # «!кепка» сразу надевает кепку
    aliases = {k.replace("ё", "е"): v for k, v in ALIASES.items()}

    if what == "reset":
        return dict(DEFAULT_LOOK), "образ сброшен"
    if what == "scarf":
        if arg in OFF_WORDS:
            value = False
        elif arg in ("вкл", "on", "надеть"):
            value = True
        else:
            value = not look.get(what)
        return {what: value}, f"{what}: {'вкл' if value else 'выкл'}"

    options = OPTIONS[what]
    if arg in OFF_WORDS:
        value = "none" if what != "hoodie" else "indigo"
    elif arg in options:
        value = arg
    elif aliases.get(arg) in options:
        value = aliases[arg]
    else:
        # без аргумента — следующий по кругу
        cycle = (["none"] if what != "hoodie" else []) + options
        cur = look.get(what, "none")
        value = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
    return {what: value}, f"{what}: {value}"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    # на некоторых Windows реестр отдаёт для этих типов не то, и браузер отказывается их грузить
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mjs": "text/javascript", ".js": "text/javascript",
                      ".wasm": "application/wasm", ".task": "application/octet-stream"}

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class Avatar(Worker):
    """Сервер аватара. Порт страницы — avatar_port, порт команд — на единицу больше."""

    def __init__(self, cfg, log, obs=None):
        super().__init__(cfg, log, obs)
        self.port = int(cfg.get("avatar_port") or 8765)
        self.look = normalize_look(cfg.get("avatar_look"))
        self.clients = set()
        self.lock = threading.Lock()
        self.last_cmd = 0.0
        self.http = None
        self.ws = None

    @property
    def page_url(self):
        """Страница-трекер: открывается в Chrome, следит за лицом по камере."""
        return f"http://127.0.0.1:{self.port}/index.html?ws={self.port + 1}"

    @property
    def view_url(self):
        """Страница-зритель для PRISM: без камеры и без панели, прозрачный фон."""
        return f"{self.page_url}&view&hide&bg=transparent"

    # --- управление образом
    def set_look(self, patch):
        with self.lock:
            self.look = normalize_look({**self.look, **patch})
            self.cfg["avatar_look"] = dict(self.look)
            payload = json.dumps({"look": self.look})
            clients = list(self.clients)
        for conn in clients:
            try:
                conn.send(payload)
            except Exception:
                self.clients.discard(conn)

    def handle_chat(self, user, raw):
        """Команды из чата. True — сообщение было командой и озвучивать его не надо."""
        if not self.cfg.get("avatar_chat", True):
            return False
        parsed = parse_command(raw, self.look)
        if parsed is None:
            return False
        now = time.time()
        if now - self.last_cmd < 3:
            return True
        self.last_cmd = now
        patch, label = parsed
        self.set_look(patch)
        self.log("avatar", f"{user}: {label}")
        return True

    # --- серверы
    def run(self):
        if not os.path.isfile(os.path.join(AVATAR_DIR, "index.html")):
            self.log("err", f"аватар: не нашёл страницу в {AVATAR_DIR}")
            return
        try:
            from websockets.sync.server import serve
        except ImportError:
            self.log("err", "аватар: не установлена библиотека websockets")
            return

        handler = functools.partial(_QuietHandler, directory=AVATAR_DIR)
        try:
            self.http = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
            self.ws = serve(self._on_connect, "127.0.0.1", self.port + 1)
        except OSError as e:
            self.log("err", f"аватар: порт {self.port} занят ({e}), поменяй его на вкладке «Аватар»")
            self._close()
            return

        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self.log("avatar", f"в Chrome (камера): {self.page_url}")
        self.log("avatar", f"в PRISM (источник): {self.view_url}")
        self.stop_flag.wait()
        self._close()

    def _on_connect(self, conn):
        with self.lock:
            self.clients.add(conn)
            hello = json.dumps({"look": self.look})
        try:
            conn.send(hello)
            for raw in conn:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if "face" in msg:           # движения лица от страницы-трекера — всем остальным
                    self._relay(conn, raw)
                elif isinstance(msg.get("look"), dict):    # одежду выбрали на странице
                    self.set_look(msg["look"])
        except Exception:
            pass
        finally:
            self.clients.discard(conn)

    def _relay(self, sender, raw):
        with self.lock:
            others = [c for c in self.clients if c is not sender]
        for conn in others:
            try:
                conn.send(raw)
            except Exception:
                self.clients.discard(conn)

    def _close(self):
        for server in (self.http, self.ws):
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    pass
        if self.http is not None:
            try:
                self.http.server_close()
            except Exception:
                pass
