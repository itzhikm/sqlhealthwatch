"""Statement-text handling.

Top-query and blocked-statement text is the only place the collector captures anything resembling
application content, and it can contain literal values. ``collection.statement_text_mode: hash``
replaces the text with a stable digest, so queries can still be correlated day over day without the
literals ever reaching the repository.
"""

from __future__ import annotations

import hashlib


def prepare_statement_text(text, mode: str = "none") -> str | None:
    """Apply the configured handling to captured statement text."""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if mode == "hash":
        return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return text
