"""ID + OTP generation (matches legacy formats: U-xxxxxxxx / S-xxxxxxxx)."""

import secrets
import uuid


def new_user_id() -> str:
    return f"U-{uuid.uuid4().hex[:8]}"


def new_subscription_id() -> str:
    return f"S-{uuid.uuid4().hex[:8]}"


def new_otp() -> str:
    """6-digit numeric OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


def new_reset_token() -> str:
    return secrets.token_hex(20)
