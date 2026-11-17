"""Oddiy kameralar uchun edge'dagi video tahlil (faqat numpy).

Kamerada «aqlli» funksiyalar bo'lishi shart emas: RTSP oqimining kichik (sub)stream
kadrlari mini-PC'da tahlil qilinadi. Har bir eshik uchun panelda chizilgan ko'pburchak
(polygon) ichidagi harakat sekin yangilanadigan fon bilan solishtiriladi:

* ``enter``  — zonada harakat boshlandi (kuchsiz dalil: yonidan o'tgan bo'lishi mumkin);
* ``dwell``  — zonada harakat ``dwell_seconds`` dan uzoq davom etdi: odam eshik oldida
  to'xtab, eshikni ochdi (kuchli dalil);
* ``line``   — asosiy kirish zonasidan o'tish (kirishlar sanog'i uchun);
* ``tamper`` — kamera yopildi (kadr deyarli bir xil rangda) yoki burildi
  (sahna ma'lumotnoma kadrdan uzoq vaqt keskin farq qiladi).

Sekin fon (alpha kichik) tufayli eshik oldida qimirlamay turgan odam ham bir necha
soniya «harakat» deb hisoblanadi — aynan shu dwell aniqlashga kerak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def polygon_mask(polygon: list[list[float]], height: int, width: int) -> np.ndarray:
    """Nisbiy (0..1) koordinatali ko'pburchakdan bool niqob (juft-toq qoidasi)."""
    pts = np.array([[x * width, y * height] for x, y in polygon], dtype=np.float64)
    ys, xs = np.mgrid[0:height, 0:width]
    xs = xs + 0.5
    ys = ys + 0.5
    inside = np.zeros((height, width), dtype=bool)
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        cond = (yi > ys) != (yj > ys)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_cross = (xj - xi) * (ys - yi) / (yj - yi) + xi
        inside ^= cond & (xs < x_cross)
        j = i
    return inside


@dataclass
class ZoneState:
    region_id: int
    kind: str  # door / line
    mask: np.ndarray
    active_since: float | None = None
    last_motion: float | None = None
    sent_enter: bool = False
    sent_dwell: bool = False


@dataclass
class MotionConfig:
    dwell_seconds: float = 2.5  # shundan uzoq — odam eshik oldida turdi
    min_fraction: float = 0.05  # zona maydonining qancha qismi o'zgarsa — harakat
    pixel_threshold: int = 25  # piksel yorqinligi farqi
    release_seconds: float = 1.5  # harakat to'xtagandan keyin zona holati tiklanadi
    bg_alpha: float = 0.02  # fon yangilanish tezligi (sekin)
    covered_std: float = 5.0  # kadr yorqinligi shu standart og'ishdan past — kamera yopilgan
    covered_seconds: float = 10.0
    moved_diff: float = 45.0  # ma'lumotnoma kadrdan o'rtacha farq — kamera burilgan
    moved_seconds: float = 60.0


@dataclass
class MotionDetector:
    zones_cfg: list[dict]
    height: int
    width: int
    cfg: MotionConfig = field(default_factory=MotionConfig)

    def __post_init__(self):
        self.zones = [
            ZoneState(int(z["region_id"]), z.get("kind", "door"), polygon_mask(z["polygon"], self.height, self.width))
            for z in self.zones_cfg
            if z.get("polygon") and len(z["polygon"]) >= 3
        ]
        self.bg: np.ndarray | None = None
        self.reference: np.ndarray | None = None
        self.stable_since: float | None = None
        self.covered_since: float | None = None
        self.moved_since: float | None = None
        self.tampered = False
        self.relearn = False

    # --------------------------------------------------------------- tamper
    def _tamper(self, gray: np.ndarray, t: float) -> list[dict]:
        out = []
        c = self.cfg
        std, mean = float(gray.std()), float(gray.mean())
        # Yopilgan: tasvir deyarli bir xil (qo'l, lenta, bo'yoq) yoki juda qorong'i va tekis
        covered = std < c.covered_std or (mean < 30 and std < 2 * c.covered_std)
        moved = False
        if self.reference is not None and not covered:
            moved = float(np.abs(gray.astype(np.int16) - self.reference).mean()) > c.moved_diff
        if covered:
            self.covered_since = self.covered_since or t
        else:
            self.covered_since = None
        if moved:
            self.moved_since = self.moved_since or t
        else:
            self.moved_since = None
        bad = (self.covered_since is not None and t - self.covered_since >= c.covered_seconds) or (
            self.moved_since is not None and t - self.moved_since >= c.moved_seconds
        )
        if bad and not self.tampered:
            self.tampered = True
            out.append({"type": "tamper", "state": "active", "reason": "covered" if covered else "moved", "t": t})
        elif not covered and not moved and self.tampered:
            self.tampered = False
            out.append({"type": "tamper", "state": "inactive", "t": t})
        # Ma'lumotnoma kadr: sahna 60 soniya tinch bo'lganda yangilanadi
        if not self.tampered and not covered:
            if self.reference is None:
                self.reference = gray.astype(np.int16)
                self.stable_since = t
            elif not moved and t - (self.stable_since or t) > 600:
                self.reference = gray.astype(np.int16)
                self.stable_since = t
        return out

    # --------------------------------------------------------------- main
    def process(self, gray: np.ndarray, t: float) -> list[dict]:
        """Kulrang kadr (height × width, uint8) va vaqt (soniya) → hodisalar ro'yxati."""
        events = self._tamper(gray, t)
        frame = gray.astype(np.float32)
        if self.covered_since is not None or self.tampered:
            # Kamera yopilgan/burilgan: fonni buzmaslik uchun tahlil to'xtatiladi
            self.relearn = True
            return events
        if self.bg is None or self.relearn:
            self.bg = frame
            self.relearn = False
            for z in self.zones:
                z.active_since = z.last_motion = None
                z.sent_enter = z.sent_dwell = False
            return events
        diff = np.abs(frame - self.bg) > self.cfg.pixel_threshold
        self.bg = (1 - self.cfg.bg_alpha) * self.bg + self.cfg.bg_alpha * frame
        for z in self.zones:
            frac = float(diff[z.mask].mean()) if z.mask.any() else 0.0
            if frac >= self.cfg.min_fraction:
                if z.active_since is None:
                    z.active_since = t
                z.last_motion = t
                if not z.sent_enter:
                    z.sent_enter = True
                    events.append({"type": "line" if z.kind == "line" else "enter", "region_id": z.region_id, "t": t})
                if z.kind == "door" and not z.sent_dwell and t - z.active_since >= self.cfg.dwell_seconds:
                    z.sent_dwell = True
                    events.append({"type": "dwell", "region_id": z.region_id, "t": t})
            elif z.active_since is not None and t - (z.last_motion or t) > self.cfg.release_seconds:
                z.active_since = z.last_motion = None
                z.sent_enter = z.sent_dwell = False
        return events
