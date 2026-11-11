from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from ...config import AvitoConfig

_ID_TAIL = re.compile(r"_(\d{6,})/?$")


def search_url(cfg: AvitoConfig, page: int = 1, pmin: int | None = None, pmax: int | None = None) -> str:
    q = {}
    if pmax is not None:
        q["pmax"] = int(pmax)
    if pmin:
        q["pmin"] = int(pmin)
    if page > 1:
        q["p"] = page
    if cfg.sort:
        q["s"] = cfg.sort
    return cfg.base_url.rstrip("/") + cfg.search_path + ("?" + urlencode(q) if q else "")


def strip_query(url: str) -> str:
    """Убирает ?context=... — он меняется от показа к показу."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def absolute(base_url: str, href: str) -> str:
    return strip_query(urljoin(base_url.rstrip("/") + "/", href))


def id_from_url(url: str) -> str | None:
    m = _ID_TAIL.search(urlsplit(url).path)
    return m.group(1) if m else None
