"""Pure logic for the paid-form access gate (no FastAPI/Redis imports).

The gate decides whether the standalone payment form may be served: the
visitor must arrive from an allow-listed landing domain (Referer), or be
remembered by IP, or carry a valid signed cookie. This module holds the
stateless pieces — client-IP extraction from a proxy chain, the hand-edited
domain allowlist with mtime-based hot reload, and the HMAC-signed cookie
token — so they can be unit-tested without the app running. The FastAPI
routes live in ``api/v2/routes/paid_gate.py``.
"""

import hashlib
import hmac
import ipaddress
import os
import time

from urllib.parse import urlsplit

from logger import logger


# Local reverse proxies are always trusted for X-Forwarded-For purposes;
# extra proxy IPs/CIDRs (e.g. the pay-server's Caddy after a split-server
# move) come from config and are appended by the caller.
_ALWAYS_TRUSTED = ("127.0.0.0/8", "::1/128")


def _parse_networks(cidrs: list[str] | tuple[str, ...]) -> list[ipaddress._BaseNetwork]:
    """Parses IPs/CIDRs into networks, skipping malformed entries.

    @param cidrs: Plain IPs ("1.2.3.4") or CIDRs ("10.0.0.0/8").
    @return: Parsed network objects (invalid entries dropped with a warning).
    """
    nets: list[ipaddress._BaseNetwork] = []
    for raw in cidrs:
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            nets.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("[PaidGate] Пропущен некорректный адрес доверенного прокси: {}", value)
    return nets


def _is_trusted(ip_str: str, nets: list[ipaddress._BaseNetwork]) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def pick_client_ip(peer_ip: str, xff_header: str | None, trusted_proxies: list[str] | None = None) -> str:
    """Resolves the real client IP behind a chain of reverse proxies.

    Walks ``X-Forwarded-For`` from the right and returns the first entry that
    is not a trusted proxy — the standard defence against a client smuggling a
    fake leftmost XFF value. When the direct peer itself is not trusted, the
    header cannot be believed at all and the peer address wins.

    @param peer_ip: Direct TCP peer of the request.
    @param xff_header: Raw ``X-Forwarded-For`` value, if any.
    @param trusted_proxies: Extra trusted proxy IPs/CIDRs from config
        (loopback is always trusted).
    @return: Best-effort client IP, never empty when ``peer_ip`` is set.
    """
    nets = _parse_networks([*_ALWAYS_TRUSTED, *(trusted_proxies or [])])
    peer = (peer_ip or "").strip()
    if not xff_header or not _is_trusted(peer, nets):
        return peer
    chain = [part.strip() for part in xff_header.split(",") if part.strip()]
    for entry in reversed(chain):
        if _is_trusted(entry, nets):
            continue
        return entry
    return peer


# --- Domain allowlist (hand-edited file, hot-reloaded by mtime) ---

_domains_cache: dict[str, tuple[float, set[str] | None]] = {}


def load_domains(path: str) -> set[str] | None:
    """Loads the landing-domain allowlist from a hand-edited file.

    One domain per line, ``#`` starts a comment, blank lines ignored. The
    parsed set is cached and re-read only when the file's mtime changes, so
    admins edit the file in place with no restart.

    @param path: Absolute path to the allowlist file.
    @return: Lowercased domain set, or ``None`` when the file is absent /
        unreadable — the caller treats that as "gate disabled" so a missing
        file can never kill the payment funnel.
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        if _domains_cache.get(path, (None, 0))[1] is not None:
            logger.warning("[PaidGate] Файл доменов {} недоступен — гейт выключен", path)
        _domains_cache[path] = (0.0, None)
        return None

    cached = _domains_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        logger.warning("[PaidGate] Не удалось прочитать файл доменов {} — гейт выключен", path)
        _domains_cache[path] = (0.0, None)
        return None

    domains: set[str] = set()
    for line in lines:
        entry = line.split("#", 1)[0].strip().lower().rstrip(".")
        if entry:
            domains.add(entry)
    _domains_cache[path] = (mtime, domains)
    logger.info("[PaidGate] Загружен список доменов ({} шт.) из {}", len(domains), path)
    return domains


def referer_matches(referer: str | None, domains: set[str]) -> bool:
    """True when the Referer's host is an allow-listed domain or its subdomain.

    @param referer: Raw ``Referer`` header value.
    @param domains: Lowercased allowlist from :func:`load_domains`.
    """
    if not referer or not domains:
        return False
    try:
        host = (urlsplit(referer.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


# --- Signed cookie token (stateless: nothing stored server-side) ---

_TOKEN_VERSION = "v1"


def sign_gate_token(secret: bytes, now: int | None = None) -> str:
    """Issues a gate cookie value: ``v1.<ts>.<hmac>``.

    @param secret: Server-side HMAC key.
    @param now: Unix seconds (defaults to current time).
    @return: Token string safe to store in a cookie.
    """
    ts = int(now if now is not None else time.time())
    payload = f"{_TOKEN_VERSION}.{ts}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_gate_token(secret: bytes, token: str | None, max_age_sec: int, now: int | None = None) -> bool:
    """Checks a gate cookie: intact signature and age within ``max_age_sec``.

    Tokens dated in the future beyond a small clock-skew allowance are
    rejected — a forged timestamp must not extend the lifetime.

    @param secret: Server-side HMAC key.
    @param token: Cookie value as received from the client.
    @param max_age_sec: Maximum accepted token age.
    @param now: Unix seconds (defaults to current time).
    """
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return False
    try:
        ts = int(parts[1])
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if ts > current + 300 or current - ts > max_age_sec:
        return False
    expected = hmac.new(secret, f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts[2])
