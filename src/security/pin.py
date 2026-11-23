"""Approval PIN: PBKDF2-HMAC-SHA256 hashing and verification (stdlib only).

Stored format: ``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>`` in APPROVAL_PIN_HASH.
A raw PIN must never be logged, printed or returned; this module never does.

CLI: ``python -m src.security.pin`` prompts for a PIN (no echo) and prints the hash.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from src.config.settings import get_config_value

ITERATIONS = 200_000
PIN_MIN_LEN, PIN_MAX_LEN = 4, 12
_SCHEME = "pbkdf2_sha256"


def valid_pin_format(pin) -> bool:
    return isinstance(pin, str) and PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN and pin.isprintable()


def hash_pin(pin: str, salt: bytes | None = None, iterations: int | None = None) -> str:
    iterations = iterations or ITERATIONS
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"{_SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def verify_pin(pin, stored: str) -> bool:
    """Constant-time check of ``pin`` against a stored hash; malformed input is just False."""
    try:
        scheme, iters, salt_hex, digest_hex = str(stored).split("$")
        if scheme != _SCHEME or not isinstance(pin, str):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def configured_hash() -> str:
    """The live APPROVAL_PIN_HASH ('' when no PIN is set)."""
    return get_config_value("APPROVAL_PIN_HASH")


def main() -> int:
    import getpass

    pin = getpass.getpass(f"New approval PIN ({PIN_MIN_LEN}-{PIN_MAX_LEN} chars): ")
    if not valid_pin_format(pin) or pin != getpass.getpass("Confirm PIN: "):
        print("PIN must be 4-12 printable characters and match the confirmation.")
        return 1
    print(f"APPROVAL_PIN_HASH={hash_pin(pin)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
