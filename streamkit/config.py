"""Настройки StreamKit: значения по умолчанию, чтение и запись config.json."""

import json
import os

CONFIG_PATH = "config.json"

MODEL_ID = "v5_5_ru"
MODEL_URL = f"https://models.silero.ai/models/tts/ru/{MODEL_ID}.pt"
SAMPLE_RATE = 48000
SPEAKERS = ["aidar", "baya", "kseniya", "xenia", "eugene"]
CLIP_LOG = "clips.log"

DEFAULTS = {
    "twitch_channel": "",
    "ws_host": "localhost",
    "ws_port": 4455,
    "ws_password": "",
    "scene_game": "",
    "scene_lobby": "",
    "scene_idle": "",
    "voice_mode": "random",
    "say_nickname": True,
    "volume": 90,
    "user_cooldown": 3,
    "save_deaths": False,
    "clip_cooldown": 12,
    "val_region": [],
    "val_mode": "color",
    "val_sensitivity": 12,
    "val_bright": 200,
    "val_share": 1.5,
    "val_color": [248, 104, 80],
    "val_tolerance": 30,
    "val_color_share": 0.6,
    "val_run": 100,
    "val_cooldown": 8,
    "val_test": False,
    "enable_valorant": False,
    "enable_tts": True,
    "enable_director": True,
    "enable_clipper": True,
}

# процессы игр: (кусок имени процесса, роль)
GAME_PROCS = [
    ("league of legends", "game"),
    ("valorant-win64", "game"),
    ("cs2", "game"),
    ("leagueclient", "lobby"),
]


def load(path=CONFIG_PATH):
    """Читает настройки, дополняя недостающие ключи значениями по умолчанию."""
    cfg = dict(DEFAULTS)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    return cfg


def save(cfg, path=CONFIG_PATH):
    """Пишет настройки на диск. Возвращает текст ошибки или None."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return None
    except OSError as e:
        return str(e)
