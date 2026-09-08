"""Authentication, replay-key, bounds, and redaction helpers."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Optional, Union


MAX_DELIVERY_ID_LENGTH = 100
MAX_LOG_BYTES = 65_536
MAX_LOG_LINES = 400


class WebhookAuthenticationError(ValueError):
    pass


def verify_github_signature(body: bytes, signature: Optional[str], secret: Optional[str]) -> None:
    if not secret:
        raise WebhookAuthenticationError("GitHub webhook authentication is not configured")
    if not signature or not signature.startswith("sha256="):
        raise WebhookAuthenticationError("missing GitHub webhook signature")
    supplied = signature.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        raise WebhookAuthenticationError("invalid GitHub webhook signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied.casefold(), expected):
        raise WebhookAuthenticationError("invalid GitHub webhook signature")


def validate_delivery_id(value: Optional[str]) -> str:
    if not value or len(value) > MAX_DELIVERY_ID_LENGTH:
        raise WebhookAuthenticationError("missing or invalid GitHub delivery ID")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise WebhookAuthenticationError("missing or invalid GitHub delivery ID")
    return value


SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s]+"),
    re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def redact_and_bound_logs(value: Union[bytes, str]) -> str:
    if isinstance(value, bytes):
        value = value[: MAX_LOG_BYTES + 1].decode("utf-8", errors="replace")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_LOG_BYTES:
        value = encoded[:MAX_LOG_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"
    lines = value.splitlines()[:MAX_LOG_LINES]
    text = "\n".join(lines)
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text
