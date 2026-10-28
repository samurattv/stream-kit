"""Мультичат: одна страница для OBS/PRISM с чатом Twitch, YouTube и VK Live одним списком.

StreamKit поднимает два локальных сервера: HTTP со страницей (streamkit/multichat/index.html)
и WebSocket, по которому страница получает сообщения. Сами чаты читает ChatTTS.
"""

import functools
import http.server
import json
import os
import threading
from collections import deque

from .workers import Worker

CHAT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multichat")
HISTORY = 50            # столько последних сообщений показываем при обновлении страницы в OBS


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class MultiChat(Worker):
    """Сервер мультичата. Порт страницы — multichat_port, порт сообщений — на единицу больше."""

    def __init__(self, cfg, log, obs=None):
        super().__init__(cfg, log, obs)
        self.port = int(cfg.get("multichat_port") or 8770)
        self.clients = set()
        self.history = deque(maxlen=HISTORY)
        self.lock = threading.Lock()
        self.http = None
        self.ws = None

    @property
    def page_url(self):
        return f"http://127.0.0.1:{self.port}/index.html?ws={self.port + 1}"

    def push(self, message):
        """Новое сообщение из чата (вызывается из потоков чтения чатов)."""
        payload = json.dumps(message, ensure_ascii=False)
        with self.lock:
            self.history.append(message)
            clients = list(self.clients)
        for conn in clients:
            try:
                conn.send(payload)
            except Exception:
                with self.lock:
                    self.clients.discard(conn)

    def run(self):
        if not os.path.isfile(os.path.join(CHAT_DIR, "index.html")):
            self.log("err", f"мультичат: не нашёл страницу в {CHAT_DIR}")
            return
        try:
            from websockets.sync.server import serve
        except ImportError:
            self.log("err", "мультичат: не установлена библиотека websockets")
            return

        handler = functools.partial(_QuietHandler, directory=CHAT_DIR)
        try:
            self.http = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
            self.ws = serve(self._on_connect, "127.0.0.1", self.port + 1)
        except OSError as e:
            self.log("err", f"мультичат: порт {self.port} занят ({e}), поменяй его на вкладке «Мультичат»")
            self._close()
            return

        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self.log("sys", f"мультичат для OBS: {self.page_url}")
        self.stop_flag.wait()
        self._close()

    def _on_connect(self, conn):
        with self.lock:
            hello = json.dumps({"history": list(self.history)}, ensure_ascii=False)
            self.clients.add(conn)
        try:
            conn.send(hello)
            for _ in conn:          # от страницы ничего не ждём, читаем только чтобы заметить закрытие
                pass
        except Exception:
            pass
        finally:
            with self.lock:
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
