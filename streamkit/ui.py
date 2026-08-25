"""Окно программы: вкладки настроек, кнопка запуска, журнал событий."""

import datetime
import queue
import re
import tkinter as tk
from tkinter import ttk, messagebox

import sounddevice as sd

from . import config as cfg_store
from .config import DEFAULTS
from .obs import ObsLink
from .workers import ChatTTS, Clipper, Director, ValorantClipper

class RegionPicker(tk.Toplevel):
    """Полупрозрачное окно на весь экран: обводим мышкой нужный кусок."""

    def __init__(self, master, on_done):
        super().__init__(master)
        self.on_done = on_done
        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.3)
        self.attributes("-topmost", True)
        self.configure(bg="black", cursor="crosshair")

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.winfo_screenwidth() // 2, 60,
            text="Обведи счётчик своих убийств, Esc — отмена",
            fill="white", font=("Segoe UI", 16),
        )

        self.start = None
        self.rect = None
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def on_press(self, ev):
        self.start = (ev.x, ev.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(ev.x, ev.y, ev.x, ev.y,
                                                 outline="#5fd39a", width=2)

    def on_drag(self, ev):
        if self.start and self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], ev.x, ev.y)

    def on_release(self, ev):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = ev.x, ev.y
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        self.destroy()
        if width > 4 and height > 4:
            self.on_done([left, top, width, height])


# ============================================================ окно программы

COLORS = {
    "bg": "#1b1d23", "panel": "#23262e", "text": "#dfe3ea",
    "chat": "#7fb2ff", "tts": "#9d8cff", "dir": "#5fd39a",
    "clip": "#ffc861", "val": "#ff9d6e", "err": "#ff7b72", "sys": "#8b93a3",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Stream Kit")
        self.geometry("760x620")
        self.configure(bg=COLORS["bg"])

        self.cfg = cfg_store.load()

        self.vars = {}
        self.workers = []
        self.obs = None
        self.log_queue = queue.Queue()
        self.running = False

        self.build_ui()
        self.after(100, self.drain_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- интерфейс
    def build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        for widget in ("TLabel", "TCheckbutton", "TRadiobutton", "TFrame",
                       "TLabelframe", "TLabelframe.Label"):
            style.configure(widget, background=COLORS["panel"],
                            foreground=COLORS["text"])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["bg"],
                        foreground=COLORS["text"], padding=(16, 7))
        style.map("TNotebook.Tab", background=[("selected", COLORS["panel"])])

        nb = ttk.Notebook(self)
        nb.pack(fill="x", padx=10, pady=(10, 0))
        self.tab_main(nb)
        self.tab_lol(nb)
        self.tab_valorant(nb)

        # --- нижняя панель со стартом
        bar = tk.Frame(self, bg=COLORS["panel"], pady=8, padx=10)
        bar.pack(fill="x", padx=10)
        left = tk.Frame(bar, bg=COLORS["panel"])
        left.pack(side="left")
        for label, key in (("Озвучка чата", "enable_tts"),
                           ("Автосцены", "enable_director")):
            var = tk.BooleanVar(value=bool(self.cfg[key]))
            self.vars[key] = var
            ttk.Checkbutton(left, text=label, variable=var).pack(side="left", padx=(0, 14))

        self.start_btn = tk.Button(bar, text="СТАРТ", command=self.toggle,
                                   bg="#2f9e63", fg="white", relief="flat",
                                   font=("Segoe UI", 11, "bold"), width=14,
                                   cursor="hand2")
        self.start_btn.pack(side="right", ipady=4)

        # --- лог
        frame = tk.Frame(self, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_box = tk.Text(frame, bg="#15171c", fg=COLORS["text"], relief="flat",
                               font=("Consolas", 9), wrap="word", state="disabled")
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.config(yscrollcommand=sb.set)
        for tag, color in COLORS.items():
            self.log_box.tag_config(tag, foreground=color)

    # ---------------------------------------------------------------- вкладки
    def tab_main(self, nb):
        page = tk.Frame(nb, bg=COLORS["panel"], padx=12, pady=10)
        nb.add(page, text="Основное")

        box = ttk.LabelFrame(page, text="Twitch", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.field(box, "Канал", "twitch_channel", 0, width=24)
        self.check(box, "Называть ник", "say_nickname", 0, 2)

        row = tk.Frame(box, bg=COLORS["panel"])
        row.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Label(row, text="Голоса:").pack(side="left")
        self.vars["voice_mode"] = tk.StringVar(value=self.cfg["voice_mode"])
        ttk.Radiobutton(row, text="случайный", value="random",
                        variable=self.vars["voice_mode"]).pack(side="left", padx=6)
        ttk.Radiobutton(row, text="свой на зрителя", value="user",
                        variable=self.vars["voice_mode"]).pack(side="left")
        ttk.Label(row, text="   Громкость:").pack(side="left")
        self.vars["volume"] = tk.IntVar(value=self.cfg["volume"])
        ttk.Scale(row, from_=10, to=100, variable=self.vars["volume"],
                  length=120).pack(side="left", padx=6)

        box = ttk.LabelFrame(page, text="PRISM / OBS — WebSocket", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.field(box, "Хост", "ws_host", 0, width=12)
        self.field(box, "Порт", "ws_port", 2, width=8)
        self.field(box, "Пароль", "ws_password", 4, width=22, show="\u2022")

        box = ttk.LabelFrame(page, text="Сцены", padding=8)
        box.pack(fill="x")
        self.combo(box, "В игре", "scene_game", 0)
        self.combo(box, "В лобби", "scene_lobby", 2)
        self.combo(box, "Простой", "scene_idle", 4)
        tk.Button(box, text="Загрузить из PRISM", command=self.load_scenes,
                  bg=COLORS["bg"], fg=COLORS["text"], relief="flat", cursor="hand2",
                  activebackground=COLORS["panel"]).grid(row=1, column=0, columnspan=2,
                                                         sticky="w", pady=(8, 0))

    def tab_lol(self, nb):
        page = tk.Frame(nb, bg=COLORS["panel"], padx=12, pady=10)
        nb.add(page, text="League of Legends")

        var = tk.BooleanVar(value=bool(self.cfg["enable_clipper"]))
        self.vars["enable_clipper"] = var
        ttk.Checkbutton(page, text="Автоклипы League of Legends",
                        variable=var).pack(anchor="w")

        ttk.Label(page, justify="left",
                  text="События берутся из официального API игры — точно и без нагрузки.\n"
                       "Ловятся убийства, ассисты, мультикиллы, драконы, бароны, эйсы."
                  ).pack(anchor="w", pady=(6, 12))

        row = tk.Frame(page, bg=COLORS["panel"])
        row.pack(anchor="w")
        var = tk.BooleanVar(value=bool(self.cfg["save_deaths"]))
        self.vars["save_deaths"] = var
        ttk.Checkbutton(row, text="Сохранять и свои смерти", variable=var).pack(side="left")
        ttk.Label(row, text="     Пауза между клипами, сек:").pack(side="left")
        self.vars["clip_cooldown"] = tk.StringVar(value=str(self.cfg["clip_cooldown"]))
        tk.Entry(row, textvariable=self.vars["clip_cooldown"], width=5,
                 bg=COLORS["bg"], fg=COLORS["text"], relief="flat",
                 insertbackground=COLORS["text"]).pack(side="left", padx=6)

    def tab_valorant(self, nb):
        page = tk.Frame(nb, bg=COLORS["panel"], padx=12, pady=10)
        nb.add(page, text="Valorant")

        var = tk.BooleanVar(value=bool(self.cfg["enable_valorant"]))
        self.vars["enable_valorant"] = var
        ttk.Checkbutton(page, text="Автоклипы Valorant", variable=var).pack(anchor="w")

        ttk.Label(page, justify="left",
                  text="API у Valorant нет, поэтому смотрим на киллфид в правом верхнем\n"
                       "углу: строки с твоим участием игра подсвечивает светлой плашкой.\n"
                       "Ловятся убийства, ассисты и смерти — всё, что тебя касается."
                  ).pack(anchor="w", pady=(6, 12))

        row = tk.Frame(page, bg=COLORS["panel"])
        row.pack(anchor="w", pady=(0, 10))
        tk.Button(row, text="Указать область киллфида", command=self.pick_region,
                  bg=COLORS["bg"], fg=COLORS["text"], relief="flat", cursor="hand2",
                  activebackground=COLORS["panel"]).pack(side="left")
        self.region_label = ttk.Label(row, text=self.region_text())
        self.region_label.pack(side="left", padx=12)

        row = tk.Frame(page, bg=COLORS["panel"])
        row.pack(anchor="w", pady=(0, 8))
        ttk.Label(row, text="Ловим по:").pack(side="left")
        self.vars["val_mode"] = tk.StringVar(value=self.cfg.get("val_mode", "color"))
        for text, value in (("цвету плашки", "color"),
                            ("яркости", "highlight"),
                            ("изменению", "change")):
            ttk.Radiobutton(row, text=text, value=value,
                            variable=self.vars["val_mode"]).pack(side="left", padx=6)

        row = tk.Frame(page, bg=COLORS["panel"])
        row.pack(anchor="w", pady=(0, 6))
        ttk.Label(row, text="Цвет плашки R,G,B:").pack(side="left")
        self.vars["val_color"] = tk.StringVar(
            value=",".join(str(c) for c in self.cfg["val_color"]))
        tk.Entry(row, textvariable=self.vars["val_color"], width=12,
                 bg=COLORS["bg"], fg=COLORS["text"], relief="flat",
                 insertbackground=COLORS["text"]).pack(side="left", padx=(4, 12))
        for label, key in (("Допуск:", "val_tolerance"),
                           ("  Полоса, px:", "val_run"),
                           ("  Доля, %:", "val_color_share")):
            ttk.Label(row, text=label).pack(side="left")
            self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
            tk.Entry(row, textvariable=self.vars[key], width=5,
                     bg=COLORS["bg"], fg=COLORS["text"], relief="flat",
                     insertbackground=COLORS["text"]).pack(side="left", padx=(4, 0))

        row = tk.Frame(page, bg=COLORS["panel"])
        row.pack(anchor="w")
        for label, key in (("Яркость:", "val_bright"),
                           ("  Светлых, %:", "val_share"),
                           ("  Изменение:", "val_sensitivity"),
                           ("  Пауза, сек:", "val_cooldown")):
            ttk.Label(row, text=label).pack(side="left")
            self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
            tk.Entry(row, textvariable=self.vars[key], width=5,
                     bg=COLORS["bg"], fg=COLORS["text"], relief="flat",
                     insertbackground=COLORS["text"]).pack(side="left", padx=(4, 0))

        var = tk.BooleanVar(value=bool(self.cfg["val_test"]))
        self.vars["val_test"] = var
        ttk.Checkbutton(page, text="Режим проверки — писать в лог, но не сохранять",
                        variable=var).pack(anchor="w", pady=(12, 0))

    def region_text(self):
        r = self.cfg.get("val_region") or []
        if len(r) == 4:
            return f"область: {r[2]}×{r[3]} px в точке {r[0]},{r[1]}"
        return "область не указана"

    def pick_region(self):
        def done(region):
            self.cfg["val_region"] = region
            self.region_label.config(text=self.region_text())
            self.collect()          # сразу пишем в config.json, чтобы не потерялось
            self.log("sys", f"область киллфида сохранена: {region}")
            self.deiconify()

        self.iconify()
        self.after(400, lambda: RegionPicker(self, done))
    def field(self, parent, label, key, col, width=20, show=None):
        ttk.Label(parent, text=label).grid(row=0, column=col, sticky="w", padx=(0, 6))
        var = tk.StringVar(value=str(self.cfg[key]))
        self.vars[key] = var
        tk.Entry(parent, textvariable=var, width=width, bg=COLORS["bg"],
                 fg=COLORS["text"], relief="flat", insertbackground=COLORS["text"],
                 show=show).grid(row=0, column=col + 1, sticky="w", padx=(0, 16))

    def combo(self, parent, label, key, col):
        ttk.Label(parent, text=label).grid(row=0, column=col, sticky="w", padx=(0, 6))
        var = tk.StringVar(value=str(self.cfg[key]))
        self.vars[key] = var
        cb = ttk.Combobox(parent, textvariable=var, width=16, state="normal")
        cb.grid(row=0, column=col + 1, sticky="w", padx=(0, 16))
        setattr(self, f"combo_{key}", cb)

    def check(self, parent, label, key, row, col):
        var = tk.BooleanVar(value=bool(self.cfg[key]))
        self.vars[key] = var
        ttk.Checkbutton(parent, text=label, variable=var).grid(
            row=row, column=col, sticky="w", padx=(0, 12))

    # ---------------------------------------------------------------- логика
    def collect(self):
        for key, var in self.vars.items():
            self.cfg[key] = var.get()

        # цвет вводится строкой "248,104,80" — превращаем в список чисел
        raw = self.cfg.get("val_color")
        if isinstance(raw, str):
            try:
                parts = [int(float(p)) for p in re.split(r"[,;\s]+", raw.strip()) if p]
                self.cfg["val_color"] = parts[:3] if len(parts) >= 3 else DEFAULTS["val_color"]
            except ValueError:
                self.log("err", "цвет плашки введён неверно, беру значение по умолчанию")
                self.cfg["val_color"] = DEFAULTS["val_color"]

        err = cfg_store.save(self.cfg)
        if err:
            self.log("err", f"настройки не сохранились: {err}")

    def log(self, tag, text):
        self.log_queue.put((tag, text))

    def drain_log(self):
        while not self.log_queue.empty():
            tag, text = self.log_queue.get()
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_box.config(state="normal")
            self.log_box.insert("end", f"{stamp}  {text}\n", tag)
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(150, self.drain_log)

    def load_scenes(self):
        self.collect()
        try:
            obs = ObsLink(self.cfg, self.log)
            version = obs.connect()
            names = obs.scenes()
            for key in ("scene_game", "scene_lobby", "scene_idle"):
                getattr(self, f"combo_{key}")["values"] = names
            self.log("sys", f"PRISM {version}: сцены загружены ({len(names)} шт.)")
        except Exception as e:
            self.log("err", f"не подключился к PRISM: {e}")
            messagebox.showerror("PRISM", f"Не удалось подключиться:\n{e}\n\n"
                                          "Проверь, что PRISM запущен, WebSocket включён "
                                          "и пароль совпадает.")

    def toggle(self):
        if self.running:
            self.stop_all()
        else:
            self.start_all()

    def start_all(self):
        self.collect()
        need_obs = (self.cfg["enable_director"] or self.cfg["enable_clipper"]
                    or self.cfg["enable_valorant"])
        self.obs = ObsLink(self.cfg, self.log)

        if need_obs:
            try:
                version = self.obs.connect()
                self.log("sys", f"PRISM подключён, версия {version}")
            except Exception as e:
                self.log("err", f"PRISM недоступен: {e}")
                messagebox.showerror("PRISM", f"Не удалось подключиться:\n{e}")
                return

        self.workers = []
        if self.cfg["enable_tts"]:
            self.workers.append(ChatTTS(self.cfg, self.log))
        if self.cfg["enable_director"]:
            self.workers.append(Director(self.cfg, self.log, self.obs))
        if self.cfg["enable_clipper"]:
            self.workers.append(Clipper(self.cfg, self.log, self.obs))
        if self.cfg["enable_valorant"]:
            self.workers.append(ValorantClipper(self.cfg, self.log, self.obs))

        if not self.workers:
            self.log("err", "ни один модуль не включён")
            return

        for w in self.workers:
            w.start()

        self.running = True
        self.start_btn.config(text="СТОП", bg="#b3453c")
        self.log("sys", "поехали")

    def stop_all(self):
        for w in self.workers:
            w.stop()
        try:
            sd.stop()
        except Exception:
            pass
        self.workers = []
        self.running = False
        self.start_btn.config(text="СТАРТ", bg="#2f9e63")
        self.log("sys", "остановлено")

    def on_close(self):
        if self.running:
            self.stop_all()
        self.collect()
        self.destroy()
