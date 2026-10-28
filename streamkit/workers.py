"""Фоновые модули: озвучка чата, автосцены, автоклипы LoL и Valorant."""

import datetime
import hashlib
import json
import os
import queue
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import sounddevice as sd

from .config import (CLIP_LOG, GAME_PROCS, MODEL_ID, MODEL_URL, SAMPLE_RATE,
                     SPEAKERS)
from .textutils import IGNORED_USERS, IRC_MSG_RE, clean_message, translit
from .voices import (VoiceBook, apply_effect, device_index, faster_rate,
                     normalize_loudness, to_ssml, voice_label)

class Worker(threading.Thread):
    def __init__(self, cfg, log, obs=None):
        super().__init__(daemon=True)
        self.cfg, self.log, self.obs = cfg, log, obs
        self.stop_flag = threading.Event()

    def stop(self):
        self.stop_flag.set()


class ChatTTS(Worker):
    """Читает чаты Twitch, YouTube и VK Live и озвучивает их голосами Silero."""

    def __init__(self, cfg, log, obs=None, speak=True):
        super().__init__(cfg, log, obs)
        self.speak = speak          # False — только читаем чаты (для мультичата), без озвучки
        self.on_chat = None         # сюда подключается мультичат: on_chat({"platform", "user", "parts"})
        self.queue = queue.Queue(maxsize=12)
        self.model = None
        self.on_command = None      # сюда подключается аватар: on_command(user, текст)
        self.voices = VoiceBook()
        self.last_seen = {}         # когда зритель писал в прошлый раз
        self.cmd_seen = {}          # когда зритель использовал команду озвучки
        self.recent = {}            # недавние тексты, чтобы не читать одно и то же

    def download_model(self):
        """Качаем модель сами: torch.hub рисует прогресс в stderr, которого нет под pyw."""
        tmp = f"{MODEL_ID}.part"
        try:
            with urllib.request.urlopen(MODEL_URL, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done, last_shown = 0, -1
                with open(tmp, "wb") as f:
                    while not self.stop_flag.is_set():
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            if pct >= last_shown + 10:
                                last_shown = pct
                                self.log("tts", f"модель: {pct}%")
            if self.stop_flag.is_set():
                os.remove(tmp)
                return False
            os.replace(tmp, f"{MODEL_ID}.pt")
            self.log("tts", "модель скачана")
            return True
        except Exception as e:
            self.log("err", f"модель не скачалась: {e}")
            if os.path.isfile(tmp):
                os.remove(tmp)
            return False

    def run(self):
        if self.speak:
            import torch
            if not os.path.isfile(f"{MODEL_ID}.pt"):
                self.log("tts", "качаю модель озвучки, это разово (~100 МБ)")
                if not self.download_model():
                    return

            torch.set_num_threads(4)
            try:
                self.model = torch.package.PackageImporter(f"{MODEL_ID}.pt") \
                    .load_pickle("tts_models", "model")
            except Exception as e:
                self.log("err", f"модель битая, удали {MODEL_ID}.pt и запусти снова: {e}")
                return
            self.model.to(torch.device("cpu"))
            self.log("tts", "движок озвучки готов")

            threading.Thread(target=self.speak_loop, daemon=True).start()
        if self.cfg.get("youtube_video_id", "").strip():
            threading.Thread(target=self.youtube_loop, daemon=True).start()
        if self.cfg.get("vk_channel", "").strip():
            threading.Thread(target=self.vk_loop, daemon=True).start()
        self.read_loop()

    # --- чтение чата Twitch
    def read_loop(self):
        channel = self.cfg["twitch_channel"].strip().lower()
        if not channel:
            # озвучка может работать и только с YouTube: тогда просто ждём остановки
            if not (self.cfg.get("youtube_video_id", "").strip()
                    or self.cfg.get("vk_channel", "").strip()):
                self.log("err", "не указан ни один чат: Twitch, YouTube или VK Live")
                return
            while not self.stop_flag.wait(1):
                pass
            return

        while not self.stop_flag.is_set():
            sock = None
            try:
                sock = socket.socket()
                sock.settimeout(5)
                sock.connect(("irc.chat.twitch.tv", 6667))
                sock.send(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                sock.send(f"JOIN #{channel}\r\n".encode())
                self.log("chat", f"подключился к каналу {channel}")
                buf = ""

                while not self.stop_flag.is_set():
                    try:
                        data = sock.recv(4096).decode("utf-8", errors="ignore")
                    except socket.timeout:
                        continue
                    if not data:
                        raise ConnectionError("соединение закрыто")
                    buf += data

                    while "\r\n" in buf:
                        line, buf = buf.split("\r\n", 1)
                        if line.startswith("PING"):
                            sock.send(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        m = IRC_MSG_RE.match(line)
                        if not m:
                            continue

                        self.handle_message(m.group("user").lower(), m.group("text").strip())

            except Exception as e:
                if not self.stop_flag.is_set():
                    self.log("err", f"чат отвалился ({e}), переподключаюсь")
                    time.sleep(5)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    # --- чтение чата YouTube (официальный Data API v3)
    def youtube_api(self, path, **params):
        params["key"] = self.cfg["youtube_api_key"].strip()
        url = f"https://www.googleapis.com/youtube/v3/{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.load(resp)

    def youtube_loop(self):
        video_id = self.cfg["youtube_video_id"].strip()
        m = re.search(r"(?:v=|youtu\.be/|live/)([\w-]{11})", video_id)   # можно вставить ссылку
        if m:
            video_id = m.group(1)
        if not self.cfg.get("youtube_api_key", "").strip():
            self.log("err", "для чата YouTube нужен API-ключ")
            return

        chat_id, page_token, first = None, None, True
        while not self.stop_flag.is_set():
            wait = 10.0
            try:
                if not chat_id:
                    data = self.youtube_api("videos", part="liveStreamingDetails", id=video_id)
                    items = data.get("items") or []
                    chat_id = (items[0].get("liveStreamingDetails", {})
                               .get("activeLiveChatId") if items else None)
                    if not chat_id:
                        self.log("err", "у трансляции YouTube пока нет чата, жду")
                        self.stop_flag.wait(15)
                        continue
                    self.log("chat", "подключился к чату YouTube")

                params = {"liveChatId": chat_id, "part": "snippet,authorDetails",
                          "maxResults": 200}
                if page_token:
                    params["pageToken"] = page_token
                data = self.youtube_api("liveChat/messages", **params)
                page_token = data.get("nextPageToken")
                # квота API ограничена, поэтому опрашиваем не чаще раза в 8 секунд
                wait = max(8.0, data.get("pollingIntervalMillis", 10000) / 1000)

                # первая порция — история чата до подключения, её не озвучиваем
                if not first:
                    for item in data.get("items", []):
                        if item["snippet"].get("type") != "textMessageEvent":
                            continue
                        user = item["authorDetails"]["displayName"].lower().lstrip("@")
                        raw = item["snippet"].get("displayMessage", "").strip()
                        self.handle_message(user, raw, "youtube")
                first = False
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                if e.code == 403 and "quota" in body.lower():
                    self.log("err", "квота YouTube API на сегодня закончилась")
                    return
                if e.code in (403, 404):
                    chat_id, page_token, first = None, None, True   # чат закрыт: найдём заново
                self.log("err", f"чат YouTube: HTTP {e.code}, повторю")
                wait = 15
            except Exception as e:
                self.log("err", f"чат YouTube отвалился ({e}), повторю")
                wait = 15
            self.stop_flag.wait(wait)

    # --- чат VK Видео Live (неофициально: тот же websocket, что использует сайт)
    VK_SITE = "https://live.vkvideo.ru/"
    VK_ORIGIN = "https://live.vkvideo.ru"      # без слэша на конце, иначе сервер отвечает 403
    VK_WS = "wss://pubsub.live.vkvideo.ru/connection/websocket?cf_protocol_version=v2"
    VK_CHANNEL_API = "https://api.live.vkvideo.ru/v1/blog/{}/public_video_stream/chat/user/"
    VK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamKit"

    def vk_get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": self.VK_UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    @staticmethod
    def vk_channel_name(raw):
        """Принимает имя канала или ссылку на него и возвращает имя."""
        raw = raw.strip().rstrip("/")
        m = re.search(r"vkvideo\.ru/([\w.\-]+)", raw)
        return (m.group(1) if m else raw).lower()

    VK_LAUGH = ("kek", "lul", "lol", "lmao", "haha", "xaxa", "ха", "смех", "ржак", "joy")

    @classmethod
    def vk_smile(cls, name, channel, mode):
        """Смайл канала VK Live как слово для озвучки. Имена вида «канал+Название», приставку убираем."""
        name = str(name or "")
        if channel and name.lower().startswith(channel) and len(name) > len(channel):
            name = name[len(channel):]
        if mode == "say":
            return name
        if mode == "laugh" and any(w in name.lower() for w in cls.VK_LAUGH):
            return "ха ха"
        return ""

    VK_IMG = "https://images.live.vkvideo.ru/"

    @classmethod
    def vk_message(cls, push, channel="", mode="laugh"):
        """Достаёт (ник, текст для озвучки, части для мультичата) из сообщения чата VK Live.
        Для служебных событий возвращает None."""
        try:
            pub = push["push"]["pub"]["data"]
            if pub.get("type") != "message":
                return None
            msg = pub["data"]
            user = str(msg["author"]["displayName"]).lower().lstrip("@")
            spoken, shown = [], []
            for part in msg.get("data", []):
                kind = part.get("type")
                if kind == "smile":
                    word = cls.vk_smile(part.get("name"), channel, mode)
                    if word:
                        spoken.append(word)
                    img = part.get("mediumUrl") or part.get("smallUrl") or ""
                    if isinstance(img, str) and img.startswith(cls.VK_IMG):
                        shown.append({"img": img, "name": str(part.get("name") or "")})
                    continue
                if kind == "mention" and part.get("displayName"):
                    shown.append({"text": "@" + str(part["displayName"])})
                    continue
                if kind != "text" or not part.get("content"):
                    continue
                content = part["content"]
                try:                                # текст приходит как JSON-массив ["текст", ...]
                    decoded = json.loads(content)
                    content = decoded[0] if isinstance(decoded, list) and decoded else content
                except ValueError:
                    pass
                if isinstance(content, str):
                    spoken.append(content)
                    if content.strip():
                        shown.append({"text": content})
            text = " ".join(spoken).strip()
            return (user, text, shown) if user and (text or shown) else None
        except (KeyError, TypeError, AttributeError):
            return None

    def vk_loop(self):
        from websockets.sync.client import connect
        name = self.vk_channel_name(self.cfg["vk_channel"])
        while not self.stop_flag.is_set():
            try:
                page = self.vk_get(self.VK_SITE)
                m = re.search(r'<script[^>]*id="app-config"[^>]*>(.*?)</script>', page, re.S)
                token = json.loads(m.group(1))["websocket"]["token"]
                info = json.loads(self.vk_get(self.VK_CHANNEL_API.format(urllib.parse.quote(name))))
                channel_id = info["data"]["owner"]["id"]

                with connect(self.VK_WS, origin=self.VK_ORIGIN, user_agent_header=self.VK_UA,
                             open_timeout=15) as ws:
                    ws.send(json.dumps({"connect": {"token": token, "name": "js"}, "id": 1}))
                    ws.send(json.dumps({"subscribe": {"channel": f"channel-chat:{channel_id}"},
                                        "id": 2}))
                    self.log("chat", f"подключился к чату VK Live: {name}")
                    while not self.stop_flag.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue
                        if raw.strip() == "{}":     # пинг сервера, на него надо отвечать
                            ws.send("{}")
                            continue
                        for line in raw.splitlines():
                            if not line.strip():
                                continue
                            parsed = self.vk_message(json.loads(line), name,
                                                     self.cfg.get("tts_emotes", "laugh"))
                            if parsed:
                                self.handle_message(parsed[0], parsed[1], "vk", parsed[2])
            except Exception as e:
                if not self.stop_flag.is_set():
                    self.log("err", f"чат VK Live отвалился ({e}), повторю через 10 секунд")
                    self.stop_flag.wait(10)

    # --- разбор сообщения
    def _num(self, key, default):
        try:
            return int(float(self.cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    def handle_message(self, user, raw, platform="twitch", parts=None):
        """Одно сообщение из чата: команды, фильтры, мультичат, постановка в очередь на озвучку."""
        if raw[:1] == "!":
            if self.speak and self.cfg.get("tts_commands", True) and self.tts_command(user, raw):
                return
            if self.on_command:
                try:
                    self.on_command(user, raw)
                except Exception as e:
                    self.log("err", f"команда чата: {e}")
        if user in IGNORED_USERS or raw[:1] in ("!", "/", "$"):
            return
        if self.on_chat:
            try:
                self.on_chat({"platform": platform, "user": user,
                              "parts": parts or [{"text": raw}]})
            except Exception as e:
                self.log("err", f"мультичат: {e}")
        if not self.speak:
            return
        if self.voices.is_muted(user):
            return
        now = time.time()
        if now - self.last_seen.get(user, 0) < float(self.cfg["user_cooldown"]):
            return

        text = clean_message(raw, numbers=bool(self.cfg.get("tts_numbers", True)),
                             emotes=self.cfg.get("tts_emotes", "laugh"),
                             max_chars=self._num("tts_max_chars", 200))
        if len(text) < 2 or self.blacklisted(raw, text) or self.is_duplicate(text, now):
            return
        self.last_seen[user] = now
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
        self.queue.put((user, text, now))
        self.log("chat", f"{user}: {text}")

    def blacklisted(self, raw, text):
        words = [w.strip().lower() for w in re.split(r"[,;\n]", str(self.cfg.get("tts_blacklist", "")))
                 if w.strip()]
        low_raw, low_text = raw.lower(), text.lower()
        return any(w in low_raw or w in low_text for w in words)

    def is_duplicate(self, text, now):
        """Одно и то же сообщение подряд от разных людей озвучиваем один раз."""
        for key in [k for k, t in self.recent.items() if now - t > 30]:
            del self.recent[key]
        key = text.lower()
        if key in self.recent:
            return True
        self.recent[key] = now
        return False

    def tts_command(self, user, raw):
        """Команды зрителей: !голос, !голос сброс, !тихо, !громко. True — это была наша команда."""
        parts = raw.lower().replace("ё", "е").split()
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        voice_cmds, mute_cmds, unmute_cmds = {"!голос", "!voice"}, {"!тихо", "!mute"}, {"!громко", "!unmute"}
        if cmd in voice_cmds and not self.cfg.get("tts_variety", True):
            return False                        # без разнообразия голоса менять нечем
        if cmd not in voice_cmds | mute_cmds | unmute_cmds:
            return False
        now = time.time()
        if now - self.cmd_seen.get(user, 0) < 10:
            return True                         # не даём засыпать журнал командами
        self.cmd_seen[user] = now
        if cmd in voice_cmds:
            if arg in ("сброс", "reset"):
                self.voices.reset(user)
                self.log("tts", f"{user}: голос сброшен")
            else:
                self.log("tts", f"{user}: новый голос {voice_label(self.voices.reroll(user))}")
        elif cmd in mute_cmds:
            self.voices.set_muted(user, True)
            self.log("tts", f"{user}: попросил не озвучивать его сообщения")
        else:
            self.voices.set_muted(user, False)
            self.log("tts", f"{user}: озвучка его сообщений снова включена")
        return True

    # --- озвучка
    def synthesize(self, phrase, voice, speed_steps=0):
        speaker, rate, pitch = voice
        if speed_steps:
            rate = faster_rate(rate, speed_steps)
        if rate == "medium" and pitch == "medium":
            audio = self.model.apply_tts(text=phrase, speaker=speaker, sample_rate=SAMPLE_RATE)
        else:
            try:
                audio = self.model.apply_tts(ssml_text=to_ssml(phrase, rate, pitch),
                                             speaker=speaker, sample_rate=SAMPLE_RATE)
            except Exception:                   # SSML не получился — читаем обычным текстом
                audio = self.model.apply_tts(text=phrase, speaker=speaker, sample_rate=SAMPLE_RATE)
        return audio.numpy().astype(np.float32)

    def finish_audio(self, wav):
        """Выравнивание громкости, эффект, общая громкость и тишина в конце."""
        if self.cfg.get("tts_normalize", True):
            wav = normalize_loudness(wav)
        wav = apply_effect(wav, self.cfg.get("tts_effect", "none"))
        wav = wav * (float(self.cfg["volume"]) / 100)
        return np.concatenate([wav, np.zeros(int(SAMPLE_RATE * 0.25), np.float32)])

    def play(self, wav):
        dev = device_index(self.cfg.get("tts_device", ""))
        try:
            sd.play(wav, SAMPLE_RATE, device=dev, blocking=True)
        except sd.PortAudioError as e:
            if dev is None:
                raise
            self.log("err", f"устройство озвучки недоступно ({e}), играю на основном")
            sd.play(wav, SAMPLE_RATE, blocking=True)

    def speak_loop(self):
        while not self.stop_flag.is_set():
            try:
                user, text, queued_at = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            backlog, steps = self.queue.qsize(), 0
            if self.cfg.get("tts_catchup", True):
                if backlog >= 6 and time.time() - queued_at > 25:
                    self.log("tts", f"пропустил устаревшее сообщение от {user}")
                    continue
                steps = 2 if backlog >= 6 else 1 if backlog >= 3 else 0
            phrase = f"{translit(user)} пишет. {text}" if self.cfg["say_nickname"] else text
            voice = self.voices.voice_for(user, self.cfg["voice_mode"],
                                          self.cfg.get("tts_variety", True))
            try:
                self.play(self.finish_audio(self.synthesize(phrase, voice, steps)))
            except Exception as e:
                self.log("err", f"озвучка: {e}")


class Director(Worker):
    """Переключает сцены по запущенной игре и держит буфер повтора."""

    def __init__(self, cfg, log, obs=None):
        super().__init__(cfg, log, obs)
        # intro — ждём запуска игры, auto — следим за играми, outro — после «Конец»
        self.mode = "intro" if cfg.get("scene_intro") else "auto"
        self.mode_changed = threading.Event()

    def set_mode(self, mode):
        self.mode = mode
        self.mode_changed.set()

    def run(self):
        import psutil
        scenes = {
            "game": self.cfg["scene_game"],
            "lobby": self.cfg["scene_lobby"],
            "idle": self.cfg["scene_idle"],
        }
        last, buffer_on = None, None
        intro_from = None            # какая роль была в момент включения интро
        self.mode_changed.set()      # первый проход: применить стартовый режим
        self.log("dir", "слежу за играми")

        while not self.stop_flag.is_set():
            try:
                names = {p.info["name"].lower() for p in psutil.process_iter(["name"])
                         if p.info.get("name")}
                role = "idle"
                for pattern, r in GAME_PROCS:
                    if any(pattern in n for n in names):
                        role = r
                        break

                if self.mode_changed.is_set():
                    self.mode_changed.clear()
                    special = {"intro": self.cfg["scene_intro"],
                               "outro": self.cfg["scene_outro"]}.get(self.mode)
                    if special:
                        self.obs.call("set_current_program_scene", special)
                        self.log("dir", f"сцена -> {special}")
                    last = special or None
                    intro_from = role
                    if self.mode == "intro":
                        self.log("dir", "интро включено, жду запуск игры")

                # из интро выходим, когда запустилась игра (роль сменилась с исходной)
                if self.mode == "intro" and role != "idle" and role != intro_from:
                    self.mode, last = "auto", None

                target = scenes.get(role) or ""
                if self.mode == "auto" and target and target != last:
                    self.obs.call("set_current_program_scene", target)
                    self.log("dir", f"сцена -> {target}")
                    last = target

                want = role == "game"
                if want != buffer_on:
                    try:
                        active = self.obs.call("get_replay_buffer_status").output_active
                        if want and not active:
                            self.obs.call("start_replay_buffer")
                            self.log("dir", "буфер повтора включён")
                        elif not want and active:
                            self.obs.call("stop_replay_buffer")
                            self.log("dir", "буфер повтора выключен")
                        buffer_on = want
                    except Exception as e:
                        self.log("err", f"буфер недоступен: {e}")
                        buffer_on = want

            except Exception as e:
                self.log("err", f"режиссёр: {e}")
                last = None
                time.sleep(4)

            self.stop_flag.wait(3)


class Clipper(Worker):
    """Ловит события LoL и просит PRISM сохранить буфер."""

    API = "https://127.0.0.1:2999/liveclientdata"
    CTX = ssl._create_unverified_context()

    WATCH = {
        "ChampionKill": "me", "Multikill": "me", "FirstBlood": "me",
        "DragonKill": "me", "BaronKill": "me", "FirstBrick": "me", "Ace": "any",
    }

    def api(self, path):
        with urllib.request.urlopen(f"{self.API}/{path}", context=self.CTX, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def short(name):
        return (name or "").split("#")[0].strip().lower()

    def describe(self, ev, me):
        name = ev.get("EventName", "")
        killer = self.short(ev.get("KillerName"))
        victim = self.short(ev.get("VictimName"))
        assists = [self.short(a) for a in ev.get("Assisters", [])]

        if self.cfg["save_deaths"] and name == "ChampionKill" and victim == me:
            return f"смерть от {ev.get('KillerName')}"
        rule = self.WATCH.get(name)
        if not rule:
            return None
        if rule == "me" and killer != me and me not in assists:
            return None

        if name == "Multikill":
            titles = {2: "дабл килл", 3: "трипл килл", 4: "квадра килл", 5: "ПЕНТАКИЛЛ"}
            return titles.get(ev.get("KillStreak", 0), "мультикилл")
        if name == "ChampionKill":
            return ("убийство: " if killer == me else "ассист: ") + str(ev.get("VictimName"))
        return {"Ace": "эйс", "FirstBlood": "фирстблад", "BaronKill": "барон",
                "DragonKill": "дракон", "FirstBrick": "первая башня"}.get(name, name)

    def run(self):
        self.log("clip", "жду начала матча")
        while not self.stop_flag.is_set():
            try:
                me = self.short(self.api("activeplayername"))
            except Exception:
                self.stop_flag.wait(4)
                continue
            if not me:
                self.stop_flag.wait(4)
                continue

            self.log("clip", f"матч начался, играю за {me}")
            seen, last_save = set(), 0.0

            while not self.stop_flag.is_set():
                try:
                    events = self.api("eventdata").get("Events", [])
                except Exception:
                    self.log("clip", "матч закончился")
                    break

                for ev in events:
                    eid = ev.get("EventID")
                    if eid in seen:
                        continue
                    seen.add(eid)
                    what = self.describe(ev, me)
                    if not what:
                        continue
                    if time.time() - last_save < float(self.cfg["clip_cooldown"]):
                        continue
                    last_save = time.time()
                    self.save_clip(what)

                self.stop_flag.wait(1)

    def save_clip(self, what):
        time.sleep(3)
        try:
            self.obs.call("save_replay_buffer")
            self.log("clip", f"клип сохранён — {what}")
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(CLIP_LOG, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {what}\n")
        except Exception as e:
            self.log("err", f"клип не сохранился: {e}")


def longest_run(mask):
    """Самая длинная непрерывная горизонтальная полоса True в маске."""
    best = np.zeros(mask.shape[0], dtype=np.int32)
    cur = np.zeros(mask.shape[0], dtype=np.int32)
    for x in range(mask.shape[1]):
        cur = np.where(mask[:, x], cur + 1, 0)
        np.maximum(best, cur, out=best)
    return int(best.max()) if best.size else 0


class ValorantClipper(Worker):
    """Следит за счётчиком убийств на экране: изменился — значит килл."""

    PROC = "valorant-win64"

    def game_running(self):
        import psutil
        for p in psutil.process_iter(["name"]):
            n = p.info.get("name")
            if n and self.PROC in n.lower():
                return True
        return False

    def run(self):
        region = self.cfg.get("val_region") or []
        if len(region) != 4:
            self.log("err", "Valorant: область счётчика не указана — вкладка Valorant")
            return
        try:
            import mss
        except Exception as e:
            self.log("err", f"Valorant: не подключился mss ({e})")
            self.log("err", f"нужен: \"{sys.executable}\" -m pip install mss")
            return

        left, top, width, height = [int(v) for v in region]
        box = {"left": left, "top": top, "width": width, "height": height}
        mode = self.cfg.get("val_mode", "color")
        threshold = float(self.cfg["val_sensitivity"])
        bright = float(self.cfg["val_bright"])
        share_need = float(self.cfg["val_share"])
        target = np.array([float(c) for c in self.cfg["val_color"]])
        tol = float(self.cfg["val_tolerance"])
        color_need = float(self.cfg["val_color_share"])
        run_need = float(self.cfg.get("val_run", 60))
        cooldown = float(self.cfg["val_cooldown"])
        test = bool(self.cfg["val_test"])

        names = {"color": "цвет плашки", "highlight": "подсветка", "change": "изменение"}
        self.log("val", f"жду запуска Valorant (ловим по: {names.get(mode, mode)})")
        waiting = True
        prev, was_on, last_save, last_beat = None, False, 0.0, 0.0

        with mss.mss() as sct:
            while not self.stop_flag.is_set():
                if not self.game_running():
                    if not waiting:
                        self.log("val", "Valorant закрыт, засыпаю")
                        waiting, prev, was_on = True, None, False
                    self.stop_flag.wait(3)
                    continue

                if waiting:
                    self.log("val", "Valorant запущен, слежу за киллфидом")
                    waiting = False

                try:
                    shot = np.asarray(sct.grab(box), dtype=np.float32)
                    rgb = shot[:, :, :3][:, :, ::-1]          # mss отдаёт BGRA
                    frame = rgb.mean(axis=2)
                except Exception as e:
                    self.log("err", f"Valorant: снимок экрана — {e}")
                    self.stop_flag.wait(2)
                    continue

                # считаем метрики: доля цвета, длина сплошной полосы, яркость
                mask = np.abs(rgb - target).max(axis=2) <= tol
                color_share = float(mask.mean() * 100)
                run_len = longest_run(mask)
                bright_share = float((frame > bright).mean() * 100)
                diff = float(np.abs(frame - prev).mean()) if prev is not None else 0.0

                if test and time.time() - last_beat >= 5:
                    last_beat = time.time()
                    level = float(frame.mean())
                    hint = "  ← экран чёрный, включи оконный режим" if level < 3 else ""
                    self.log("val", f"замер: полоса {run_len} px (нужно {run_need:.0f}) · "
                                    f"цвет {color_share:.2f}% · "
                                    f"светлых {bright_share:.2f}% · "
                                    f"изменение {diff:.1f} · "
                                    f"яркость {level:.0f}{hint}")

                if mode == "color":
                    on = run_len >= run_need
                    kind = "смерть" if run_len >= run_need * 3 else "убийство"
                    fired, note = (on and not was_on), f"{kind} (полоса {run_len} px)"
                    was_on = on
                elif mode == "highlight":
                    on = bright_share >= share_need
                    fired, note = (on and not was_on), f"подсветка {bright_share:.2f}%"
                    was_on = on
                else:
                    fired, note = diff > threshold, f"изменение {diff:.0f}"

                if fired and time.time() - last_save > cooldown:
                    last_save = time.time()
                    if test:
                        self.log("val", f"СРАБОТАЛО бы — {note}")
                    else:
                        self.save_clip(note)

                prev = frame
                self.stop_flag.wait(0.2)

    def save_clip(self, what):
        time.sleep(2)
        try:
            self.obs.call("save_replay_buffer")
            self.log("val", "клип сохранён")
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(CLIP_LOG, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  Valorant: {what}\n")
        except Exception as e:
            self.log("err", f"клип не сохранился: {e}")
