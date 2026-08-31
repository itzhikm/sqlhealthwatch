"""Secret reference resolution.

Passwords are never stored in YAML. Config carries a *reference* -- ``env:NAME``,
``credman:TARGET`` or ``dpapi:PATH`` -- which is resolved here at load time. Windows auth is
preferred precisely so that most servers need no secret at all.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path


class SecretError(RuntimeError):
    pass


def resolve(ref: str | None) -> str | None:
    """Resolve a ``scheme:value`` secret reference. ``None`` passes through for Windows auth."""
    if ref is None:
        return None
    scheme, _, value = ref.partition(":")
    if not value:
        raise SecretError(
            f"secret reference {ref!r} has no value; expected env:NAME, credman:TARGET or dpapi:PATH"
        )
    resolver = _RESOLVERS.get(scheme)
    if resolver is None:
        raise SecretError(f"unknown secret scheme {scheme!r} in {ref!r}")
    return resolver(value)


def _from_env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError:
        raise SecretError(
            f"environment variable {name!r} is not set (add it to .env or the service account's environment)"
        ) from None


def _from_credman(target: str) -> str:
    """Windows Credential Manager. Import is local so the module stays importable on Linux."""
    try:
        import win32cred  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise SecretError(
            f"credman:{target} needs pywin32 on the collector host (pip install pywin32)"
        ) from exc
    try:
        cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
    except Exception as exc:  # pragma: no cover - platform dependent
        raise SecretError(f"credential {target!r} not found in Windows Credential Manager") from exc
    return cred["CredentialBlob"].decode("utf-16-le")


def _from_dpapi(path: str) -> str:
    """A DPAPI-protected blob on disk, decryptable only by the collector's service account."""
    try:
        import win32crypt  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise SecretError(f"dpapi:{path} needs pywin32 on the collector host") from exc
    blob = Path(path).read_text(encoding="ascii").strip()
    _, secret = win32crypt.CryptUnprotectData(base64.b64decode(blob), None, None, None, 0)
    return secret.decode("utf-8")


_RESOLVERS = {"env": _from_env, "credman": _from_credman, "dpapi": _from_dpapi}
