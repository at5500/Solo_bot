import base64
import hashlib
import hmac
import re

from config import API_TOKEN, WEBHOOK_SECRET_TOKEN


def _secret_bytes() -> bytes:
    seed = (WEBHOOK_SECRET_TOKEN or API_TOKEN or "solobot-referral").strip()
    return seed.encode("utf-8")


def _urlsafe_b64decode_nopad(value: str) -> bytes:
    normalized = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(normalized.encode("ascii"))


def encode_referral_code(user_id: int) -> str:
    if int(user_id) <= 0:
        raise ValueError("user_id must be positive")
    raw = int(user_id).to_bytes(8, byteorder="big", signed=False)
    secret = _secret_bytes()
    mask = hmac.new(secret, b"ref-mask-v1", hashlib.sha256).digest()[:8]
    obfuscated = bytes(a ^ b for a, b in zip(raw, mask, strict=False))
    signature = hmac.new(secret, b"ref-sign-v1:" + obfuscated, hashlib.sha256).digest()[:6]
    payload = base64.urlsafe_b64encode(obfuscated + signature).decode("ascii").rstrip("=")
    return f"r1_{payload}"


def encode_partner_code(user_id: int) -> str:
    if int(user_id) <= 0:
        raise ValueError("user_id must be positive")
    raw = int(user_id).to_bytes(8, byteorder="big", signed=False)
    secret = _secret_bytes()
    mask = hmac.new(secret, b"partner-mask-v1", hashlib.sha256).digest()[:8]
    obfuscated = bytes(a ^ b for a, b in zip(raw, mask, strict=False))
    signature = hmac.new(secret, b"partner-sign-v1:" + obfuscated, hashlib.sha256).digest()[:6]
    payload = base64.urlsafe_b64encode(obfuscated + signature).decode("ascii").rstrip("=")
    return f"p1_{payload}"


# Blocklist for custom partner codes (audit F-017). The tokens below
# look like an official channel — letting users squat ``admin`` /
# ``support`` / ``api`` makes phishing trivial.
_RESERVED_PARTNER_CODES: frozenset[str] = frozenset({
    # Roles / system.
    "admin", "administrator", "api", "bot", "help", "moderator", "null",
    "official", "owner", "root", "staff", "support", "system", "team",
    "undefined",
    # Brand — anything that could pass for our own channel.
    "dino", "dinovpn", "dinopay", "vpndino",
    # Product / app.
    "vpn", "vpns", "myvpn", "service", "services", "app", "application",
    "mini", "miniapp", "cabinet", "dashboard", "site", "web",
    # Payments / finance — highest phishing value for a paid service (F-017).
    "pay", "payment", "payments", "billing", "invoice", "invoices",
    "checkout", "refund", "refunds", "wallet", "balance", "topup", "deposit",
    "withdraw", "withdrawal", "payout", "payouts", "order", "orders",
    "transaction", "transactions", "bank", "card", "cards", "sbp", "kassa",
    "receipt", "subscription", "subscribe", "price", "tariff", "tariffs",
    "money", "cash", "cashback",
    # Auth / verification / security.
    "verify", "verified", "verification", "secure", "security", "login",
    "signin", "signup", "register", "registration", "auth", "authenticate",
    "account", "accounts", "password", "reset", "confirm", "confirmation",
    "activate", "activation", "otp", "code", "codes", "token", "tokens",
    "2fa", "unlock",
    # Official / contact — impersonating staff channels.
    "contact", "contacts", "info", "news", "notify", "notification",
    "notifications", "alert", "alerts", "mail", "email", "feedback",
    "abuse", "report", "ticket", "tickets", "manager", "agent", "operator",
    # Promo / scam bait.
    "free", "bonus", "bonuses", "gift", "gifts", "promo", "promocode",
    "discount", "discounts", "sale", "trial", "premium", "vip", "win",
    "winner", "prize", "prizes", "claim", "reward", "rewards", "giveaway",
    "lottery", "jackpot",
    # Structural / routes.
    "home", "index", "main", "settings", "profile", "guest", "test", "demo",
    "example", "www",
})

# Reserved even as a *substring* — impersonating the brand or a payment/auth
# channel inside a longer slug ("dinovpn_support", "verify_account",
# "official_billing") is the whole risk. Only long, high-signal tokens go here
# so short generic words don't false-positive (e.g. "team" in "steam").
_RESERVED_SUBSTRINGS: tuple[str, ...] = (
    "dino",
    "payment",
    "verify",
    "verification",
    "official",
    "support",
    "billing",
    "refund",
    "password",
    "checkout",
    "invoice",
)


def is_reserved_partner_code(code: str | None) -> bool:
    """Returns ``True`` when ``code`` is on the reserved blocklist —
    either an exact match against :data:`_RESERVED_PARTNER_CODES` or
    containing one of :data:`_RESERVED_SUBSTRINGS` (case-insensitive).
    Guards the ``PATCH /partners/.../code`` endpoints before any DB write —
    the user gets a clean 422 instead of an unhelpful 409 «занято» (or a
    successful squat when the slot is still free)."""
    normalized = (code or "").strip().lower()
    if not normalized:
        return False
    if normalized in _RESERVED_PARTNER_CODES:
        return True
    return any(token in normalized for token in _RESERVED_SUBSTRINGS)


def decode_referral_code(value: str | None) -> int | None:
    token = str(value or "").strip()
    if not token:
        return None
    if token.startswith("r1_"):
        encoded = token[3:]
        try:
            data = _urlsafe_b64decode_nopad(encoded)
        except Exception:
            return None
        if len(data) != 14:
            return None
        obfuscated, signature = data[:8], data[8:]
        secret = _secret_bytes()
        expected = hmac.new(secret, b"ref-sign-v1:" + obfuscated, hashlib.sha256).digest()[:6]
        if not hmac.compare_digest(signature, expected):
            return None
        mask = hmac.new(secret, b"ref-mask-v1", hashlib.sha256).digest()[:8]
        raw = bytes(a ^ b for a, b in zip(obfuscated, mask, strict=False))
        parsed = int.from_bytes(raw, byteorder="big", signed=False)
        return parsed if parsed > 0 else None
    if token.startswith("p1_"):
        return None
    match = re.fullmatch(r"\d+", token)
    if not match:
        return None
    parsed = int(match.group(0))
    return parsed if parsed > 0 else None


def decode_partner_code(value: str | None) -> int | None:
    token = str(value or "").strip()
    if not token:
        return None
    if token.startswith("p1_"):
        encoded = token[3:]
        try:
            data = _urlsafe_b64decode_nopad(encoded)
        except Exception:
            return None
        if len(data) != 14:
            return None
        obfuscated, signature = data[:8], data[8:]
        secret = _secret_bytes()
        expected = hmac.new(secret, b"partner-sign-v1:" + obfuscated, hashlib.sha256).digest()[:6]
        if not hmac.compare_digest(signature, expected):
            return None
        mask = hmac.new(secret, b"partner-mask-v1", hashlib.sha256).digest()[:8]
        raw = bytes(a ^ b for a, b in zip(obfuscated, mask, strict=False))
        parsed = int.from_bytes(raw, byteorder="big", signed=False)
        return parsed if parsed > 0 else None
    if token.startswith("r1_"):
        return decode_referral_code(token)
    match = re.fullmatch(r"\d+", token)
    if not match:
        return None
    parsed = int(match.group(0))
    return parsed if parsed > 0 else None
