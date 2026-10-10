"""Окно программы: вкладки настроек, кнопка запуска, журнал событий."""

import datetime
import queue
import re
import tkinter as tk
import webbrowser
from tkinter import messagebox

import customtkinter as ctk
import sounddevice as sd

from . import config as cfg_store
from .avatar import Avatar
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

# Цвета заданы парой (светлая тема, тёмная тема): customtkinter сам переключает их.
BG = ("#f5f1ea", "#1f1d1a")
CARD = ("#fffdf9", "#2a2723")
LINE = ("#e6dfd2", "#3a352f")
TEXT = ("#2b2620", "#ece6dc")
TEXT2 = ("#7a7165", "#9a9185")
FIELD = ("#f1ece2", "#1f1d1a")
ACCENT = ("#c96442", "#d9774f")
ACCENT_HOVER = ("#b3573a", "#c4663f")
STOP = ("#a8403a", "#b3453c")
STOP_HOVER = ("#8f342f", "#9c3a33")
LOG_BG = ("#efe9dd", "#191714")
OK = ("#5f8a52", "#8fb27f")
BAD = ("#c0392b", "#ff7b72")

# Цвета строк журнала: (светлая, тёмная). У tk-тегов нет пар, поэтому подставляем вручную.
LOG_COLORS = {
    "chat": ("#2f6fb8", "#7fb2ff"), "tts": ("#6b57c9", "#9d8cff"),
    "dir": ("#3f7d57", "#5fd39a"), "clip": ("#a8741a", "#ffc861"),
    "val": ("#c4572b", "#ff9d6e"), "avatar": ("#b24f86", "#e6a0c4"),
    "err": ("#c0392b", "#ff7b72"), "sys": ("#7a7165", "#9a9185"),
}

LOOK_NAMES = {
    "hoodie": {"indigo": "индиго", "red": "красное", "green": "зелёное",
               "orange": "оранжевое", "black": "чёрное"},
    "hat": {"none": "без шапки", "beanie": "бини", "cap": "кепка", "crown": "корона"},
    "glasses": {"none": "без очков", "round": "круглые", "sun": "тёмные"},
}

SCENE_KEYS = ("scene_game", "scene_lobby", "scene_idle", "scene_intro", "scene_outro")


class App(ctk.CTk):
    def __init__(self):
        self.cfg = cfg_store.load()
        self.theme = self.cfg.get("theme") if self.cfg.get("theme") in ("dark", "light") else "dark"
        ctk.set_appearance_mode(self.theme)
        super().__init__(fg_color=BG)
        self.title("Stream Kit")
        # окно не выше экрана: на небольших мониторах вкладки прокручиваются
        height = min(880, max(620, self.winfo_screenheight() - 90))
        self.geometry(f"860x{height}")
        self.minsize(780, 600)

        self.f_body = ctk.CTkFont(family="Segoe UI", size=13)
        self.f_small = ctk.CTkFont(family="Segoe UI", size=12)
        self.f_bold = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.f_title = ctk.CTkFont(family="Segoe UI", size=20, weight="bold")
        self.f_mono = ctk.CTkFont(family="Consolas", size=12)

        self.vars = {}
        self.workers = []
        self.obs = None
        self.log_queue = queue.Queue()
        self.running = False

        self.build_ui()
        self.retag_log()
        self.after(100, self.drain_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- заготовки
    def label(self, parent, text, dim=False, **kw):
        return ctk.CTkLabel(parent, text=text, font=self.f_small if dim else self.f_body,
                            text_color=TEXT2 if dim else TEXT, anchor="w", **kw)

    def card(self, parent, title, pady=(0, 10), action=None):
        """Скруглённая карточка с заголовком. Возвращает контейнер для содержимого.

        action=(текст, команда) — кнопка справа в заголовке, её не сожмёт нехватка места."""
        outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14,
                             border_width=1, border_color=LINE)
        outer.pack(fill="x", pady=pady)
        if title or action:
            head = ctk.CTkFrame(outer, fg_color="transparent")
            head.pack(fill="x", padx=16, pady=(10, 0))
            ctk.CTkLabel(head, text=title, font=self.f_small, text_color=TEXT2,
                         anchor="w").pack(side="left")
            if action:
                self.button(head, action[0], action[1], width=170, height=28).pack(side="right")
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(8, 14))
        return body

    def row(self, parent, pady=(0, 0)):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(anchor="w", fill="x", pady=pady)
        return f

    def entry(self, parent, key, width=100, show=None, value=None, padx=(0, 14)):
        text = str(self.cfg[key]) if value is None else value
        var = tk.StringVar(value=text)
        self.vars[key] = var
        e = ctk.CTkEntry(parent, textvariable=var, width=width, height=32, corner_radius=8,
                         fg_color=FIELD, border_color=LINE, text_color=TEXT, font=self.f_body,
                         show=show)
        e.pack(side="left", padx=padx)
        return e

    def combo(self, parent, var, values, width=150, state="normal", command=None):
        return ctk.CTkComboBox(parent, variable=var, values=values or [""], width=width,
                               height=32, corner_radius=8, state=state, font=self.f_body,
                               dropdown_font=self.f_body, fg_color=FIELD, border_color=LINE,
                               button_color=LINE, button_hover_color=ACCENT, text_color=TEXT,
                               dropdown_fg_color=CARD, dropdown_text_color=TEXT,
                               dropdown_hover_color=FIELD, command=command)

    def check(self, parent, text, var, command=None):
        return ctk.CTkCheckBox(parent, text=text, variable=var, command=command, font=self.f_body,
                               text_color=TEXT, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                               border_color=LINE, checkmark_color="#ffffff",
                               corner_radius=6, checkbox_width=20, checkbox_height=20)

    def radio(self, parent, text, var, value):
        return ctk.CTkRadioButton(parent, text=text, variable=var, value=value, font=self.f_body,
                                  text_color=TEXT, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                  border_color=LINE, radiobutton_width=18, radiobutton_height=18)

    def switch(self, parent, text, key):
        var = tk.BooleanVar(value=bool(self.cfg[key]))
        self.vars[key] = var
        return ctk.CTkSwitch(parent, text=text, variable=var, font=self.f_body, text_color=TEXT,
                             progress_color=ACCENT, button_color=("#ffffff", "#ece6dc"),
                             button_hover_color=("#f1ece2", "#ffffff"), fg_color=LINE)

    def button(self, parent, text, command, width=0, height=32, **kw):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                             corner_radius=8, font=self.f_body, fg_color=FIELD,
                             hover_color=LINE, text_color=TEXT, border_width=1,
                             border_color=LINE, **kw)

    def bool_var(self, key):
        var = tk.BooleanVar(value=bool(self.cfg[key]))
        self.vars[key] = var
        return var

    # ---------------------------------------------------------------- интерфейс
    def build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=3)
        self.grid_rowconfigure(3, weight=1, minsize=120)

        # --- шапка
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))
        ctk.CTkLabel(head, text="Stream Kit", font=self.f_title, text_color=TEXT).pack(side="left")
        self.theme_btn = self.button(head, self.theme_text(), self.toggle_theme, width=130)
        self.theme_btn.pack(side="right")

        # --- вкладки
        self.tabs = ctk.CTkTabview(
            self, fg_color="transparent", corner_radius=14, border_width=0, height=500,
            segmented_button_fg_color=FIELD, segmented_button_selected_color=CARD,
            segmented_button_selected_hover_color=CARD,
            segmented_button_unselected_color=FIELD,
            segmented_button_unselected_hover_color=LINE, text_color=TEXT)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 0))
        for name, builder in (("Основное", self.tab_main),
                              ("League of Legends", self.tab_lol),
                              ("Valorant", self.tab_valorant),
                              ("Аватар", self.tab_avatar)):
            # прокрутка внутри вкладки: в маленьком окне содержимое не обрезается, а прокручивается
            scroll = ctk.CTkScrollableFrame(self.tabs.add(name), fg_color="transparent",
                                            scrollbar_button_color=LINE,
                                            scrollbar_button_hover_color=TEXT2)
            scroll.pack(fill="both", expand=True)
            builder(scroll)

        # --- нижняя панель со стартом
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 8))
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left")
        self.switch(left, "Озвучка чата", "enable_tts").pack(side="left", padx=(0, 18))
        self.switch(left, "Автосцены", "enable_director").pack(side="left")

        self.start_btn = ctk.CTkButton(
            bar, text="Старт", command=self.toggle, width=150, height=42, corner_radius=10,
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff")
        self.start_btn.pack(side="right")
        for text, mode in (("Конец", "outro"), ("Интро", "intro")):
            self.button(bar, text, lambda m=mode: self.show_scene(m), width=84,
                        height=42).pack(side="right", padx=(0, 8))

        # --- журнал
        self.log_box = ctk.CTkTextbox(self, fg_color=LOG_BG, text_color=TEXT, corner_radius=12,
                                      font=self.f_mono, wrap="word", state="disabled",
                                      border_width=0, height=110)
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 18))

    # ---------------------------------------------------------------- вкладки
    def tab_main(self, page):
        body = self.card(page, "Twitch")
        r = self.row(body)
        self.label(r, "Канал").pack(side="left", padx=(0, 8))
        self.entry(r, "twitch_channel", width=220)
        self.check(r, "Называть ник", self.bool_var("say_nickname")).pack(side="left")

        r = self.row(body, pady=(10, 0))
        self.label(r, "Голоса").pack(side="left", padx=(0, 10))
        self.vars["voice_mode"] = tk.StringVar(value=self.cfg["voice_mode"])
        self.radio(r, "случайный", self.vars["voice_mode"], "random").pack(side="left", padx=(0, 12))
        self.radio(r, "свой на зрителя", self.vars["voice_mode"], "user").pack(side="left", padx=(0, 20))
        self.label(r, "Громкость").pack(side="left", padx=(0, 8))
        self.vars["volume"] = tk.IntVar(value=int(self.cfg["volume"]))
        ctk.CTkSlider(r, from_=10, to=100, number_of_steps=18, variable=self.vars["volume"],
                      width=140, progress_color=ACCENT, button_color=ACCENT,
                      button_hover_color=ACCENT_HOVER, fg_color=LINE).pack(side="left")

        body = self.card(page, "PRISM / OBS")
        r = self.row(body)
        self.label(r, "Хост").pack(side="left", padx=(0, 8))
        self.entry(r, "ws_host", width=130)
        self.label(r, "Порт").pack(side="left", padx=(0, 8))
        self.entry(r, "ws_port", width=80)
        self.label(r, "Пароль").pack(side="left", padx=(0, 8))
        self.entry(r, "ws_password", width=170, show="•")
        self.prism_status = ctk.CTkLabel(body, text="●  не подключено", font=self.f_small,
                                         text_color=TEXT2, anchor="w")
        self.prism_status.pack(anchor="w", pady=(8, 0))

        body = self.card(page, "Сцены", action=("Загрузить из PRISM", self.load_scenes))
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(anchor="w")
        for i, (text, key) in enumerate((("В игре", "scene_game"), ("В лобби", "scene_lobby"),
                                         ("Простой", "scene_idle"), ("Интро", "scene_intro"),
                                         ("Конец", "scene_outro"))):
            r, c = divmod(i, 3)
            self.label(grid, text).grid(row=r, column=c * 2, sticky="w", padx=(0, 8),
                                        pady=(0 if r == 0 else 10, 0))
            var = tk.StringVar(value=str(self.cfg[key]))
            self.vars[key] = var
            cb = self.combo(grid, var, [str(self.cfg[key])], width=150)
            cb.grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 18),
                    pady=(0 if r == 0 else 10, 0))
            setattr(self, f"combo_{key}", cb)

    def tab_lol(self, page):
        body = self.card(page, "Автоклипы")
        self.check(body, "Автоклипы League of Legends", self.bool_var("enable_clipper")).pack(anchor="w")
        self.label(body, "События берутся из официального API игры — точно и без нагрузки.\n"
                         "Ловятся убийства, ассисты, мультикиллы, драконы, бароны, эйсы.",
                   dim=True, justify="left").pack(anchor="w", pady=(8, 10))
        r = self.row(body)
        self.check(r, "Сохранять и свои смерти", self.bool_var("save_deaths")).pack(side="left", padx=(0, 24))
        self.label(r, "Пауза между клипами, сек").pack(side="left", padx=(0, 8))
        self.entry(r, "clip_cooldown", width=64)

    def tab_valorant(self, page):
        body = self.card(page, "Автоклипы")
        self.check(body, "Автоклипы Valorant", self.bool_var("enable_valorant")).pack(anchor="w")
        self.label(body, "API у Valorant нет, поэтому смотрим на киллфид в правом верхнем углу:\n"
                         "строки с твоим участием игра подсвечивает светлой плашкой.\n"
                         "Ловятся убийства, ассисты и смерти — всё, что тебя касается.",
                   dim=True, justify="left").pack(anchor="w", pady=(8, 10))
        r = self.row(body)
        self.button(r, "Указать область киллфида", self.pick_region, width=200).pack(side="left")
        self.region_label = self.label(r, self.region_text(), dim=True)
        self.region_label.pack(side="left", padx=14)

        body = self.card(page, "Как ловим")
        r = self.row(body)
        self.label(r, "Ловим по").pack(side="left", padx=(0, 10))
        self.vars["val_mode"] = tk.StringVar(value=self.cfg.get("val_mode", "color"))
        for text, value in (("цвету плашки", "color"), ("яркости", "highlight"), ("изменению", "change")):
            self.radio(r, text, self.vars["val_mode"], value).pack(side="left", padx=(0, 14))

        r = self.row(body, pady=(10, 0))
        self.label(r, "Цвет плашки R,G,B").pack(side="left", padx=(0, 8))
        self.entry(r, "val_color", width=110, value=",".join(str(c) for c in self.cfg["val_color"]))
        for label, key in (("Допуск", "val_tolerance"), ("Полоса, px", "val_run"),
                           ("Доля, %", "val_color_share")):
            self.label(r, label).pack(side="left", padx=(0, 8))
            self.entry(r, key, width=60)

        r = self.row(body, pady=(10, 0))
        for label, key in (("Яркость", "val_bright"), ("Светлых, %", "val_share"),
                           ("Изменение", "val_sensitivity"), ("Пауза, сек", "val_cooldown")):
            self.label(r, label).pack(side="left", padx=(0, 8))
            self.entry(r, key, width=60)

        self.check(body, "Режим проверки — писать в журнал, но не сохранять",
                   self.bool_var("val_test")).pack(anchor="w", pady=(12, 0))

    def tab_avatar(self, page):
        body = self.card(page, "Аватар-енот")
        self.check(body, "Включить аватара", self.bool_var("enable_avatar")).pack(anchor="w")
        self.label(body, "1. Нажми «Старт», потом «Открыть для камеры»: страница откроется в Chrome.\n"
                         "    Разреши камеру и оставь вкладку открытой — она следит за твоим лицом.\n"
                         "2. В PRISM добавь источник «Веб-страница» со ссылкой из кнопки «Ссылка для PRISM».\n"
                         "    Она показывает енота без панели и без камеры, фон прозрачный.",
                   dim=True, justify="left").pack(anchor="w", pady=(8, 10))
        r = self.row(body)
        self.label(r, "Порт").pack(side="left", padx=(0, 8))
        self.entry(r, "avatar_port", width=80)
        self.button(r, "Открыть для камеры", self.open_avatar, width=170).pack(side="left", padx=(0, 8))
        self.button(r, "Ссылка для PRISM", self.copy_avatar_url, width=160).pack(side="left")

        body = self.card(page, "Одежда")
        look = self.cfg.get("avatar_look") or {}
        self.look_vars = {}
        self.look_disp = {}
        r = self.row(body)
        for label, key, default in (("Худи", "hoodie", "indigo"),
                                    ("Шапка", "hat", "none"),
                                    ("Очки", "glasses", "none")):
            self.label(r, label).pack(side="left", padx=(0, 8))
            v = tk.StringVar(value=look.get(key, default))
            self.look_vars[key] = v                  # то, что понимает аватар (indigo, cap…)
            names = LOOK_NAMES[key]
            disp = tk.StringVar(value=names.get(v.get(), v.get()))
            self.look_disp[key] = (disp, names)      # то, что видит человек (индиго, кепка…)
            self.combo(r, disp, list(names.values()), width=130, state="readonly",
                       command=lambda _l, k=key: self.pick_look(k)).pack(side="left", padx=(0, 16))
        r = self.row(body, pady=(10, 0))
        v = tk.BooleanVar(value=bool(look.get("scarf", False)))
        self.look_vars["scarf"] = v
        self.check(r, "Шарф", v, command=self.apply_look).pack(side="left")

        body = self.card(page, "Чат")
        self.check(body, "Команды из чата: !шапка, !кепка, !очки, !худи, !шарф, !образ",
                   self.bool_var("avatar_chat")).pack(anchor="w")
        self.label(body, "Команды читает озвучка чата, поэтому она должна быть включена.",
                   dim=True).pack(anchor="w", pady=(6, 0))

    # ---------------------------------------------------------------- тема
    def theme_text(self):
        return "Светлая тема" if self.theme == "dark" else "Тёмная тема"

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        ctk.set_appearance_mode(self.theme)
        self.cfg["theme"] = self.theme
        self.theme_btn.configure(text=self.theme_text())
        self.retag_log()

    def retag_log(self):
        idx = 1 if self.theme == "dark" else 0
        for tag, colors in LOG_COLORS.items():
            self.log_box.tag_config(tag, foreground=colors[idx])

    def set_prism_status(self, text, ok=None):
        color = TEXT2 if ok is None else (OK if ok else BAD)
        self.prism_status.configure(text=f"●  {text}", text_color=color)

    # ---------------------------------------------------------------- аватар
    def avatar_url(self):
        try:
            port = int(self.vars["avatar_port"].get())
        except ValueError:
            port = int(self.cfg["avatar_port"])
        return f"http://127.0.0.1:{port}/index.html?ws={port + 1}"

    def open_avatar(self):
        if not any(isinstance(w, Avatar) for w in self.workers):
            self.log("err", "аватар не запущен: включи галочку и нажми «Старт»")
            return
        webbrowser.open(self.avatar_url())

    def copy_avatar_url(self):
        url = f"{self.avatar_url()}&view&hide&bg=transparent"
        self.clipboard_clear()
        self.clipboard_append(url)
        self.log("sys", f"ссылка для PRISM скопирована: {url}")

    def pick_look(self, key):
        disp, names = self.look_disp[key]
        by_label = {label: value for value, label in names.items()}
        self.look_vars[key].set(by_label.get(disp.get(), "none"))
        self.apply_look()

    def apply_look(self):
        """Выбор в списках «Одежда» — сразу на енота и в настройки."""
        look = {k: v.get() for k, v in self.look_vars.items()}
        self.cfg["avatar_look"] = look
        avatar = next((w for w in self.workers if isinstance(w, Avatar)), None)
        if avatar:
            avatar.set_look(look)

    def sync_look(self):
        """Команды из чата меняют образ — показываем это и в списках «Одежда»."""
        avatar = next((w for w in self.workers if isinstance(w, Avatar)), None)
        if not avatar or not hasattr(self, "look_vars"):
            return
        for key, var in self.look_vars.items():
            value = avatar.look.get(key)
            if value is not None and var.get() != value:
                var.set(value)
                if key in self.look_disp:
                    disp, names = self.look_disp[key]
                    disp.set(names.get(value, value))

    # ---------------------------------------------------------------- Valorant
    def region_text(self):
        r = self.cfg.get("val_region") or []
        if len(r) == 4:
            return f"область: {r[2]}×{r[3]} px в точке {r[0]},{r[1]}"
        return "область не указана"

    def pick_region(self):
        def done(region):
            self.cfg["val_region"] = region
            self.region_label.configure(text=self.region_text())
            self.collect()          # сразу пишем в config.json, чтобы не потерялось
            self.log("sys", f"область киллфида сохранена: {region}")
            self.deiconify()

        self.iconify()
        self.after(400, lambda: RegionPicker(self, done))

    # ---------------------------------------------------------------- логика
    def collect(self):
        for key, var in self.vars.items():
            self.cfg[key] = var.get()
        self.cfg["theme"] = self.theme

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
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{stamp}  {text}\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.sync_look()
        self.after(150, self.drain_log)

    def load_scenes(self):
        self.collect()
        try:
            obs = ObsLink(self.cfg, self.log)
            version = obs.connect()
            names = obs.scenes()
            for key in SCENE_KEYS:
                getattr(self, f"combo_{key}").configure(values=names or [""])
            self.set_prism_status(f"подключено, версия {version}", True)
            self.log("sys", f"PRISM {version}: сцены загружены ({len(names)} шт.)")
        except Exception as e:
            self.set_prism_status("не удалось подключиться", False)
            self.log("err", f"не подключился к PRISM: {e}")
            messagebox.showerror("PRISM", f"Не удалось подключиться:\n{e}\n\n"
                                          "Проверь, что PRISM запущен, WebSocket включён "
                                          "и пароль совпадает.")

    def show_scene(self, mode):
        """Кнопки «Интро» и «Конец»: включают сцену и ставят автосцены на паузу."""
        self.collect()
        key = "scene_intro" if mode == "intro" else "scene_outro"
        target = self.cfg[key]
        if not target:
            self.log("err", f"сцена «{'Интро' if mode == 'intro' else 'Конец'}» не выбрана")
            return
        director = next((w for w in self.workers if isinstance(w, Director)), None)
        if director:
            director.set_mode(mode)      # сама переключит сцену
            return
        try:
            (self.obs or ObsLink(self.cfg, self.log)).call("set_current_program_scene", target)
            self.log("dir", f"сцена -> {target}")
        except Exception as e:
            self.log("err", f"не удалось переключить сцену: {e}")

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
                self.set_prism_status(f"подключено, версия {version}", True)
                self.log("sys", f"PRISM подключён, версия {version}")
            except Exception as e:
                self.set_prism_status("не удалось подключиться", False)
                self.log("err", f"PRISM недоступен: {e}")
                messagebox.showerror("PRISM", f"Не удалось подключиться:\n{e}")
                return

        self.workers = []
        avatar = Avatar(self.cfg, self.log) if self.cfg["enable_avatar"] else None
        if avatar:
            self.workers.append(avatar)
        if self.cfg["enable_tts"]:
            tts = ChatTTS(self.cfg, self.log)
            if avatar:
                tts.on_command = avatar.handle_chat
            self.workers.append(tts)
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
        self.start_btn.configure(text="Стоп", fg_color=STOP, hover_color=STOP_HOVER)
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
        self.start_btn.configure(text="Старт", fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.log("sys", "остановлено")

    def on_close(self):
        if self.running:
            self.stop_all()
        self.collect()
        self.destroy()
