"""Credential storage for GInbox Cleaner.

App passwords are stored in the OS credential manager (Windows Credential
Manager, GNOME Keyring / KWallet via Secret Service) through the optional
'keyring' package. When no usable backend exists - e.g. a minimal Linux
install without a Secret Service daemon - the password falls back to a
secrets.json file that is always created owner-readable only (0600).

config.json itself only ever holds non-secret settings (email, language).
"""

import json
import os
from pathlib import Path

SERVICE = "ginbox-cleaner"

try:
    import keyring
except Exception:  # not installed or broken import (no dbus, etc.)
    keyring = None


def secure_write(path, data):
    """Write bytes to `path` with owner-only permissions (0600).

    Every sensitive file the app writes goes through here so secrets
    never sit in a world-readable file, even briefly.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    # normalize perms even if the file pre-existed with wider rights
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class CredentialStore:
    """Get/set/delete app passwords, preferring the OS keyring."""

    def __init__(self, secrets_file):
        self._file = Path(secrets_file)

    # ---------- keyring ----------

    @staticmethod
    def _backend():
        if keyring is None:
            return None
        try:
            backend = keyring.get_keyring()
        except Exception:
            return None
        # keyring's "fail" backends report priority <= 0
        if getattr(backend, "priority", 1) <= 0:
            return None
        return backend

    @property
    def backend_name(self):
        return "keyring" if self._backend() is not None else "file"

    @property
    def backend_label(self):
        """Human-readable name of the keyring in use, or None for file."""
        backend = self._backend()
        if backend is None:
            return None
        name = type(backend).__name__.lower()
        if "win" in name:
            return "Windows Credential Manager"
        if "kwallet" in name:
            return "KWallet"
        if "secret" in name or "dbus" in name:
            return "GNOME Keyring"
        return type(backend).__name__

    # ---------- public API ----------

    def get(self, account):
        backend = self._backend()
        if backend is not None:
            try:
                pw = backend.get_password(SERVICE, account)
                if pw:
                    return pw
            except Exception:
                pass
        return self._file_all().get(account)

    def set(self, account, password):
        backend = self._backend()
        if backend is not None:
            try:
                backend.set_password(SERVICE, account, password)
                self._file_remove(account)
                return
            except Exception:
                pass
        data = self._file_all()
        data[account] = password
        self._file_write(data)

    def delete(self, account):
        backend = self._backend()
        if backend is not None:
            try:
                backend.delete_password(SERVICE, account)
            except Exception:
                pass
        self._file_remove(account)

    # ---------- secrets.json fallback ----------

    def _file_all(self):
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _file_remove(self, account):
        data = self._file_all()
        if account in data:
            data.pop(account, None)
            self._file_write(data)

    def _file_write(self, data):
        secure_write(self._file, json.dumps(data, indent=2).encode())
