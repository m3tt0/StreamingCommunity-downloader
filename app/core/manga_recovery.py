"""Finding MangaWorld again after its domain rotates.

The same contract as ``domain_recovery``, which this reuses for everything that
matters to safety: a candidate must pass ``is_plausible`` (a second-level
domain, public DNS, a name matching ``NAME_PATTERN``), must prove it serves
MangaWorld, and is *proposed* to an administrator unless ``domain_auto_apply``
says otherwise. Only where candidates come from differs.

MangaWorld has no page announcing its domain. What it does have, measured, is
old domains that answer with a 301 to the current one — ``mangaworld.ac`` and
``mangaworld.so`` both point at ``mangaworld.mx`` — while the ones that lapsed
were bought and send visitors to ad networks (``.bz``, ``.io``) or to a parking
page on a third-level name (``ww38.mangaworld.fun``). So candidates are:

1. where the configured host itself redirects to;
2. where every host the panel has used before, plus a seed list of known ones,
   redirects to — one hop each, read from ``Location`` and never followed;
3. those same hosts directly, in case one of them is live again;
4. the links on ``MANGAWORLD_DOMAIN_SOURCE_URL``, when a deployment sets one.

The ad-network redirects fail the name pattern and the parking page fails the
second-level rule, which is the guard doing exactly its job. Verification asks
for the site's own ``og:site_name`` with no redirect allowed, so a parked
domain cannot pass by forwarding to the real one.
"""

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

from app import config
from app.core import domain_recovery
from app.core.headers import get_headers
from app.core.mangaworld import looks_like_mangaworld

logger = logging.getLogger(__name__)

# A module constant with an env override, never a settings field: see the same
# note on domain_recovery.NAME_PATTERN.
NAME_PATTERN = re.compile(os.getenv("MANGAWORLD_NAME_PATTERN", r"^mangaworld[a-z0-9-]{0,8}$"))

SOURCE_URL = os.getenv("MANGAWORLD_DOMAIN_SOURCE_URL", "").strip()

# Domains the site has used. Order is irrelevant; each is asked once per check.
KNOWN_HOSTS = (
    "www.mangaworld.mx", "www.mangaworld.ac", "www.mangaworld.so",
    "www.mangaworld.bz", "www.mangaworld.cx", "www.mangaworld.nz",
    "www.mangaworld.io", "www.mangaworld.in", "www.mangaworld.tv",
)

# data.json key holding every host the panel has moved away from.
HISTORY_KEY = "mangaworld_previous_hosts"
_MAX_HISTORY = 20

_MIN_CHECK_INTERVAL = domain_recovery._MIN_CHECK_INTERVAL
_PROBE_TIMEOUT = 8
_PROBE_WORKERS = 6

_lock = threading.Lock()
_pending: dict | None = None
_last_check_at: float | None = None


def _now() -> float:
    return time.monotonic()


def _headers() -> dict:
    return {"user-agent": get_headers()}


# ── Candidates ────────────────────────────────────────────────────────────────

def _redirect_target(host: str) -> str | None:
    """Where ``https://host/`` sends a visitor, one hop, without going there."""
    try:
        r = requests.get(f"https://{host}/", headers=_headers(),
                         timeout=_PROBE_TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return None
    if r.status_code not in (301, 302, 303, 307, 308):
        return None
    location = urljoin(f"https://{host}/", r.headers.get("location") or "")
    parsed = urlparse(location)
    if parsed.scheme != "https":
        return None
    return (parsed.hostname or "").lower().strip(".") or None


def _known_hosts(current: str) -> list[str]:
    history = config.read_data().get(HISTORY_KEY) or []
    hosts: list[str] = []
    for host in (current, *history, *KNOWN_HOSTS):
        host = (host or "").lower().strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def fetch_candidates(current: str) -> list[str]:
    """Every host worth verifying, the most telling first."""
    known = _known_hosts(current)
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
        redirects = list(pool.map(_redirect_target, known))

    candidates: list[str] = []

    def add(host):
        if host and host != current and host not in candidates:
            candidates.append(host)

    for target in redirects:  # where the old addresses point, current first
        add(target)
    if SOURCE_URL:
        try:
            for host in domain_recovery.fetch_candidates(SOURCE_URL):
                add(host)
        except Exception as exc:
            logger.warning("Cannot read the MangaWorld domain page: %s", exc)
    for host in known:  # last: a known host that is live again
        add(host)
    return candidates


# ── Verification ──────────────────────────────────────────────────────────────

def verify(host: str) -> bool:
    """Whether ``host`` serves MangaWorld itself, with no redirect in between."""
    try:
        r = requests.get(f"https://{host}/", headers=_headers(),
                         timeout=_PROBE_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        logger.info("MangaWorld candidate %s unreachable: %s", host, exc)
        return False
    return r.status_code == 200 and looks_like_mangaworld(r.text)


def is_plausible(host: str) -> tuple[bool, str]:
    return domain_recovery.is_plausible(host, NAME_PATTERN)


# ── Pending candidate ─────────────────────────────────────────────────────────

def pending() -> dict | None:
    with _lock:
        return dict(_pending) if _pending else None


def clear_pending() -> None:
    global _pending
    with _lock:
        _pending = None


def _set_pending(candidate: dict | None) -> None:
    global _pending
    with _lock:
        _pending = candidate


def apply_candidate(host: str) -> None:
    """Make ``host`` MangaWorld's domain, re-checking it first.

    The host being left is remembered: it is where the next rotation's 301 will
    most likely come from.
    """
    ok, reason = is_plausible(host)
    if not ok:
        raise RuntimeError(f"Dominio non adottabile: {reason}")
    if not verify(host):
        raise RuntimeError(f"«{host}» non risponde come MangaWorld")

    previous = config.configured_mangaworld_host()
    history = [h for h in (config.read_data().get(HISTORY_KEY) or []) if h != host]
    if previous and previous != host and previous not in history:
        history.insert(0, previous)
    config.update_data({"mangaworld_domain": host, HISTORY_KEY: history[:_MAX_HISTORY]})
    # Keyed on the host already; dropped as well, the rule the other source follows.
    from app.core import home

    home.clear_cache("mangaworld")
    clear_pending()
    logger.info("MangaWorld domain set to %s", host)


# ── The cycle ─────────────────────────────────────────────────────────────────

def run_check(force: bool = False) -> dict:
    """One pass: is MangaWorld alive where configured, and if not, where is it."""
    global _last_check_at

    current = config.configured_mangaworld_host()
    result: dict = {"current": current, "current_ok": False, "candidate": None,
                    "applied": False, "rejected": [], "checked": False}

    with _lock:
        if not force and _last_check_at is not None \
                and _now() - _last_check_at < _MIN_CHECK_INTERVAL:
            return result
        _last_check_at = _now()
    result["checked"] = True

    result["current_ok"] = verify(current)
    if result["current_ok"] and not force:
        _set_pending(None)
        return result

    for host in fetch_candidates(current):
        ok, reason = is_plausible(host)
        if not ok:
            result["rejected"].append({"host": host, "reason": reason})
            continue
        if not verify(host):
            result["rejected"].append({"host": host, "reason": "non verificato"})
            continue
        result["candidate"] = host
        break

    candidate = result["candidate"]
    if candidate is None:
        return result

    if config.get_settings().get("domain_auto_apply"):
        try:
            apply_candidate(candidate)
            result["applied"] = True
            domain_recovery._announce(
                "applied",
                f"MangaWorld ha cambiato dominio: ora è «{candidate}». "
                "Il pannello lo ha applicato da solo.",
                title="Dominio MangaWorld",
            )
        except RuntimeError as exc:
            logger.warning("Auto-apply of MangaWorld %s failed: %s", candidate, exc)
        return result

    _set_pending({"host": candidate, "found_at": time.time()})
    domain_recovery._announce(
        "found",
        f"MangaWorld non risponde più su «{current}». "
        f"Trovato «{candidate}»: apri il pannello per applicarlo.",
        title="Dominio MangaWorld",
    )
    try:
        from app.jobs import job_manager

        job_manager.broadcast({"type": "manga_domain_candidate"})
    except Exception:
        logger.debug("Cannot broadcast the MangaWorld candidate", exc_info=True)
    return result
