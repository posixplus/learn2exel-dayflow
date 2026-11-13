"""Secrets in the operating system's keychain. Capstone.

Lesson 87 stored the Google token in a file readable only by you, and said that was
the minimum. A file at mode 600 is still readable by every program you run, by a
backup tool, and by anyone who copies your home folder. The OS keychain encrypts
the item at rest and asks before a new program reads it.

This is a thin wrapper over the `keyring` package, which talks to macOS Keychain,
Windows Credential Locker or the Secret Service on Linux. It is optional:

    pip install keyring

Two names are used:  google-token  (the JSON from lesson 87)
                     anthropic-api-key
"""
from __future__ import annotations

from typing import Any

SERVICE = "dayflow"
GOOGLE_TOKEN = "google-token"
ANTHROPIC_KEY = "anthropic-api-key"


class VaultError(RuntimeError):
    pass


def _backend(backend: Any = None) -> Any:
    if backend is not None:
        return backend
    try:
        import keyring
    except ImportError as e:
        raise VaultError("the keychain store needs the keyring package: pip install keyring") from e
    return keyring


def get(name: str, *, backend: Any = None) -> str | None:
    try:
        return _backend(backend).get_password(SERVICE, name)
    except VaultError:
        raise
    except Exception as e:  # noqa: BLE001 - a locked or missing keychain is a state, not a bug
        raise VaultError(f"could not read {name!r} from the keychain: {type(e).__name__}: {e}") from e


def put(name: str, value: str, *, backend: Any = None) -> None:
    try:
        _backend(backend).set_password(SERVICE, name, value)
    except VaultError:
        raise
    except Exception as e:  # noqa: BLE001
        raise VaultError(f"could not write {name!r} to the keychain: {type(e).__name__}: {e}") from e


def delete(name: str, *, backend: Any = None) -> bool:
    b = _backend(backend)
    try:
        if b.get_password(SERVICE, name) is None:
            return False
        b.delete_password(SERVICE, name)
        return True
    except Exception as e:  # noqa: BLE001
        raise VaultError(f"could not delete {name!r} from the keychain: {type(e).__name__}: {e}") from e


def available() -> bool:
    """True when the keyring package is importable. Says nothing about whether a keychain is unlocked."""
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False
