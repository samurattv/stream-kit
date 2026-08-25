"""Фоновые модули: озвучка чата, автосцены, автоклипы LoL и Valorant."""

import datetime
import hashlib
import json
import os
import queue
import random
import socket
import ssl
import sys
import threading
import time
import urllib.request

import numpy as np
import sounddevice as sd

from .config import (CLIP_LOG, GAME_PROCS, MODEL_ID, MODEL_URL, SAMPLE_RATE,
                     SPEAKERS)
from .textutils import IGNORED_USERS, IRC_MSG_RE, clean_message, translit

class Worker(threading.Thread):
    def __init__(self, cfg, log, obs=None):
        super().__init__(daemon=True)
        self.cfg, self.log, self.obs = cfg, log, obs
        self.stop_flag = threading.Event()

    def stop(self):
        self.stop_flag.set()


class ChatTTS(Worker):
    """Читает чат Twitch и озвучивает его голосами Silero."""

    def __init__(self, cfg, log, obs=None):
        super().__init__(cfg, log, obs)
        self.queue = queue.Queue(maxsize=12)
        self.model = None

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
        self.read_loop()

    # --- чтение чата
    def read_loop(self):
        channel = self.cfg["twitch_channel"].strip().lower()
        if not channel:
            self.log("err", "не указан канал Twitch")
            return
        last_seen = {}

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

                        user = m.group("user").lower()
                        raw = m.group("text").strip()
                        if user in IGNORED_USERS or raw[:1] in ("!", "/", "$"):
                            continue
                        now = time.time()
                        if now - last_seen.get(user, 0) < float(self.cfg["user_cooldown"]):
                            continue

                        text = clean_message(raw)
                        if len(text) < 2:
                            continue
                        last_seen[user] = now
                        if self.queue.full():
                            try:
                                self.queue.get_nowait()
                            except queue.Empty:
                                pass
                        self.queue.put((user, text))
                        self.log("chat", f"{user}: {text}")

            except Exception as e:
                if not self.stop_flag.is_set():
                    self.log("err", f"чат отвалился ({e}), переподключаюсь")
                    time.sleep(5)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    # --- озвучка
    def pick_voice(self, user):
        if self.cfg["voice_mode"] == "random":
            return random.choice(SPEAKERS)
        h = hashlib.md5(user.encode("utf-8")).hexdigest()
        return SPEAKERS[int(h, 16) % len(SPEAKERS)]

    def speak_loop(self):
        while not self.stop_flag.is_set():
            try:
                user, text = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            phrase = f"{translit(user)} пишет. {text}" if self.cfg["say_nickname"] else text
            try:
                audio = self.model.apply_tts(
                    text=phrase, speaker=self.pick_voice(user), sample_rate=SAMPLE_RATE
                )
                wav = audio.numpy().astype(np.float32) * (int(self.cfg["volume"]) / 100)
                wav = np.concatenate([wav, np.zeros(int(SAMPLE_RATE * 0.25), np.float32)])
                sd.play(wav, SAMPLE_RATE, blocking=True)
            except Exception as e:
                self.log("err", f"озвучка: {e}")


class Director(Worker):
    """Переключает сцены по запущенной игре и держит буфер повтора."""

    def run(self):
        import psutil
        scenes = {
            "game": self.cfg["scene_game"],
            "lobby": self.cfg["scene_lobby"],
            "idle": self.cfg["scene_idle"],
        }
        last, buffer_on = None, None
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

                target = scenes.get(role) or ""
                if target and target != last:
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
