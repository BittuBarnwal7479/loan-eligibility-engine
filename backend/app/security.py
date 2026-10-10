"""Shared-secret checks for internal endpoints and local upload URLs."""

from __future__ import annotations

import hashlib
import hmac
import time


def constant_time_equals(provided: str | None, expected: str | None) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def sign_upload(secret: str, batch_id: str, expires_at: int) -> str:
    message = f"{batch_id}:{expires_at}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_upload_signature(secret: str, batch_id: str, expires_at: int, signature: str) -> bool:
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(sign_upload(secret, batch_id, expires_at), signature)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"
