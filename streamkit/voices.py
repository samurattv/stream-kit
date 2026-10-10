"""Голоса озвучки: десятки вариантов из пяти голосов Silero, память зрителей, эффекты звука."""

import hashlib
import json
import os
import random
import threading
from xml.sax.saxutils import escape

import numpy as np

from .config import SAMPLE_RATE, SPEAKERS

# Скорость и высота задаются через SSML. Крайние значения (x-slow, x-high…) не берём:
# они хуже разборчивы. Первые в каждом списке — обычные.
RATES = ["medium", "fast", "slow"]
PITCHES = ["medium", "low", "high"]
RATE_ORDER = ["x-slow", "slow", "medium", "fast", "x-fast"]   # для ускорения при большой очереди

VOICES = [(sp, rate, pitch) for sp in SPEAKERS for rate in RATES for pitch in PITCHES]

EFFECTS = {"none": "без эффекта", "radio": "рация", "robot": "робот", "echo": "эхо"}


def voice_label(voice):
    sp, rate, pitch = voice
    return f"{sp}/{rate}/{pitch}"


def faster_rate(rate, steps):
    """Ускоряет скорость речи на steps ступеней, но не быстрее x-fast."""
    i = RATE_ORDER.index(rate) if rate in RATE_ORDER else 2
    return RATE_ORDER[min(len(RATE_ORDER) - 1, i + steps)]


def to_ssml(text, rate, pitch):
    return f'<speak><prosody rate="{rate}" pitch="{pitch}">{escape(text)}</prosody></speak>'


class VoiceBook:
    """Какой голос у какого зрителя и кто попросил его не озвучивать. Хранится в tts_users.json."""

    def __init__(self, path="tts_users.json"):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def stable_index(self, user):
        return int(hashlib.md5(user.encode("utf-8")).hexdigest(), 16) % len(VOICES)

    def voice_for(self, user, mode="user", variety=True):
        """Выбор голоса. mode: user — закреплённый за зрителем, random — каждый раз случайный."""
        if not variety:
            speakers = SPEAKERS
            if mode == "random":
                return (random.choice(speakers), "medium", "medium")
            h = int(hashlib.md5(user.encode("utf-8")).hexdigest(), 16)
            return (speakers[h % len(speakers)], "medium", "medium")
        with self.lock:
            chosen = self.data.get(user, {}).get("voice")
        if isinstance(chosen, int) and 0 <= chosen < len(VOICES):
            return VOICES[chosen]
        if mode == "random":
            return random.choice(VOICES)
        return VOICES[self.stable_index(user)]

    def reroll(self, user):
        """Зритель просит себе новый голос (!голос). Возвращает новый голос."""
        with self.lock:
            current = self.data.get(user, {}).get("voice", self.stable_index(user))
            choices = [i for i in range(len(VOICES)) if i != current]
            idx = random.choice(choices)
            self.data.setdefault(user, {})["voice"] = idx
            self._save()
        return VOICES[idx]

    def reset(self, user):
        with self.lock:
            if user in self.data:
                self.data[user].pop("voice", None)
                if not self.data[user]:
                    del self.data[user]
                self._save()

    def is_muted(self, user):
        with self.lock:
            return bool(self.data.get(user, {}).get("muted"))

    def set_muted(self, user, muted):
        with self.lock:
            entry = self.data.setdefault(user, {})
            if muted:
                entry["muted"] = True
            else:
                entry.pop("muted", None)
                if not entry:
                    del self.data[user]
            self._save()


# ------------------------------------------------------------------ звук
def normalize_loudness(wav, target_rms=0.11, max_gain=4.0):
    """Выравнивает громкость фраз: разные голоса и скорости звучат не одинаково тихо или громко."""
    wav = np.asarray(wav, dtype=np.float32)
    if wav.size == 0:
        return wav
    rms = float(np.sqrt(np.mean(wav ** 2)))
    if rms < 1e-5:
        return wav
    wav = wav * min(target_rms / rms, max_gain)
    peak = float(np.max(np.abs(wav)))
    if peak > 0.95:
        wav = wav * (0.95 / peak)
    return wav


def apply_effect(wav, name, sr=SAMPLE_RATE):
    """Простые эффекты на numpy: рация, робот, эхо."""
    wav = np.asarray(wav, dtype=np.float32)
    if name not in EFFECTS or name == "none" or wav.size < 64:
        return wav
    if name == "radio":
        spec = np.fft.rfft(wav)
        freqs = np.fft.rfftfreq(len(wav), 1.0 / sr)
        mask = np.clip((freqs - 250) / 150, 0, 1) * np.clip((3600 - freqs) / 400, 0, 1)
        out = np.fft.irfft(spec * mask, len(wav)).astype(np.float32)
        out = np.tanh(out * 2.4) * 0.8
    elif name == "robot":
        t = np.arange(len(wav), dtype=np.float32) / sr
        out = wav * (0.55 + 0.45 * np.sin(2 * np.pi * 55.0 * t)).astype(np.float32)
        out = out * 1.4
    else:  # echo
        out = np.concatenate([wav, np.zeros(int(sr * 0.5), np.float32)])
        for delay, gain in ((0.17, 0.38), (0.34, 0.18)):
            d = int(sr * delay)
            out[d:d + len(wav)] += wav * gain
    peak = float(np.max(np.abs(out)))
    if peak > 0.98:
        out = out * (0.98 / peak)
    return out.astype(np.float32)


def output_device_names():
    """Названия устройств вывода звука (только из основного API, без дублей)."""
    import sounddevice as sd
    names = []
    try:
        api = sd.query_hostapis(sd.default.hostapi)
        indexes = api.get("devices", [])
    except Exception:
        indexes = range(len(sd.query_devices()))
    for i in indexes:
        try:
            dev = sd.query_devices(i)
        except Exception:
            continue
        if dev["max_output_channels"] > 0 and dev["name"] not in names:
            names.append(dev["name"])
    return names


def device_index(name):
    """Номер устройства по названию; None — устройство по умолчанию (или названия больше нет)."""
    if not name:
        return None
    import sounddevice as sd
    try:
        for i in range(len(sd.query_devices())):
            dev = sd.query_devices(i)
            if dev["max_output_channels"] > 0 and dev["name"] == name:
                return i
    except Exception:
        pass
    return None
