"""Скачивание фото: уменьшение до 384 px по длинной стороне, SHA-1 исходника и перцептивный хеш (pHash).

pHash считается так же, как в библиотеке imagehash (DCT 32x32, левый верхний блок 8x8, порог — медиана),
поэтому хеши совместимы с imagehash.hex_to_hash. Расстояние Хэмминга <= 6–8 обычно означает одно и то же фото.
"""

from __future__ import annotations

import hashlib
import io
import logging
import random
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageOps

from . import db
from .config import Config
from .utils import now_msk, short_hash

log = logging.getLogger("realty")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


@lru_cache(maxsize=4)
def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    return m  # масштаб не важен: сравниваем с медианой


def phash(img: Image.Image, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    n = hash_size * highfreq_factor
    g = np.asarray(img.convert("L").resize((n, n), Image.Resampling.LANCZOS), dtype=np.float64)
    m = _dct_matrix(n)
    dct = m @ g @ m.T
    low = dct[:hash_size, :hash_size]
    bits = (low > np.median(low)).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:0{hash_size * hash_size // 4}x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def process_image(data: bytes, max_side: int, quality: int) -> tuple[bytes, dict]:
    img = Image.open(io.BytesIO(data))
    img.load()
    orig_w, orig_h = img.size
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), {
        "width": img.width, "height": img.height, "orig_width": orig_w, "orig_height": orig_h,
        "phash": phash(img), "sha1": hashlib.sha1(data).hexdigest(),
    }


def photo_path(cfg: Config, platform: str, listing_id: str, position: int, kind: str, url: str) -> Path:
    prefix = "L" if kind == "layout" else ""
    return cfg.path("photos", platform, str(listing_id), f"{prefix}{position:02d}_{short_hash(url)}.jpg")


def download_pending(con, cfg: Config, platform: str, run_id: int | None, limit: int | None = None,
                     deadline: float | None = None, referer: str = "https://www.avito.ru/") -> dict:
    pc = cfg.photos
    rows = db.photos_pending(con, platform, limit or pc.max_per_run, pc.max_attempts)
    stats = {"pending": len(rows), "ok": 0, "failed": 0, "bytes": 0}
    if not rows:
        return stats
    log.info("Фото к скачиванию: %d", len(rows))
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT, "Referer": referer, "Accept": "image/avif,image/webp,image/*,*/*"})
    for n, r in enumerate(rows, 1):
        if deadline is not None and time.monotonic() > deadline:
            stats["stopped_by_deadline"] = True
            break
        url = r["url"]
        try:
            resp = sess.get(url, timeout=pc.timeout_s)
            if resp.status_code == 404 and r["url_large"] and r["url_large"] != url:
                resp = sess.get(r["url_large"], timeout=pc.timeout_s)
            resp.raise_for_status()
            jpg, info = process_image(resp.content, pc.max_side, pc.jpeg_quality)
            path = photo_path(cfg, platform, r["listing_id"], r["position"], r["kind"], url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpg)
            info["local_path"] = str(path.relative_to(cfg.data_path))
            info["bytes"] = len(jpg)
            db.photo_ok(con, r["rid"], info, now_msk())
            stats["ok"] += 1
            stats["bytes"] += len(jpg)
        except Exception as e:  # noqa: BLE001 — любая ошибка фото не должна останавливать сбор
            db.photo_fail(con, r["rid"], f"{type(e).__name__}: {e}")
            stats["failed"] += 1
            if r["attempts"] + 1 >= pc.max_attempts:
                db.log_error(con, run_id, platform, "photo", url, "photo_failed", str(e), now_msk())
        if n % 50 == 0:
            con.commit()
            log.info("Фото: %d/%d (ошибок %d)", n, len(rows), stats["failed"])
        time.sleep(random.uniform(pc.delay_min_s, pc.delay_max_s))
    con.commit()
    return stats
