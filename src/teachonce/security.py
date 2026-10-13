"""Safety checks — secrets scanning.

TeachOnce must never store or propagate credentials (see AGENTS.md
"安全红线"). Pattern-based scan runs BEFORE anything is persisted.
"""

from __future__ import annotations

import re

from .errors import SensitiveContentError

# (pattern, human-readable description of what it looks like)
_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]"),
        "key/secret/password assignment",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "OpenAI-style API key (sk-...)"),
    (
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
        "bearer token",
    ),
    (re.compile(r"(?i)\bcookie\s*[:=]\s*\S+"), "cookie value"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "private key block",
    ),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "GitHub token (gh*_...)",
    ),
]


def scan_for_secrets(text: str) -> list[str]:
    """Return descriptions of sensitive fragments found in ``text`` (if any)."""
    hits: list[str] = []
    for pattern, label in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return hits


def ensure_no_secrets(text: str, *, where: str = "input") -> None:
    """Raise :class:`SensitiveContentError` if ``text`` contains secrets.

    ``where`` names the context (e.g. "demonstration", "correction") so the
    error message can tell the user exactly what to fix.
    """
    hits = scan_for_secrets(text)
    if hits:
        raise SensitiveContentError(
            f"Refusing to store {where}: it appears to contain sensitive "
            f"content ({', '.join(hits)}). TeachOnce never records passwords, "
            "tokens, or cookies. Remove the sensitive fragment and retry."
        )
