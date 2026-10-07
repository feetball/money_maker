"""Kalshi API keys entered in the dashboard (Settings -> API keys).

Safety rules:

* **Write-only.** Nothing here returns the private key to a caller outside this process:
  :meth:`CredentialStore.summary` gives only a masked key id, the public-key fingerprint and
  timestamps. The API never echoes a key back to the browser.
* **On disk, owner-only.** One JSON file (``live.secrets_path``, default
  ``data/secrets/kalshi-keys.json``) written atomically with mode 0600 in a 0700 directory. A
  file found with group/other permissions is tightened on load. ``data/`` is git-ignored and
  excluded from the Docker image (it is a bind mount), so keys never reach a commit or an image.
  The file is not encrypted: anyone who can read it as the bot's OS user can use the key, the
  same as a key file named in ``config.yaml``.
* **Validated before it is stored.** The PEM must parse to an RSA (>= 2048 bit) or Ed25519
  private key; the API also checks it against Kalshi before saving.
* Keys set in ``config.yaml`` or ``KALSHIBOT_LIVE__*`` env vars win over dashboard keys.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshibot.kalshi.trading import load_private_key

__all__ = ["ENVIRONMENTS", "CredentialStore", "StoredKey", "fingerprint", "mask_key_id", "parse_private_key"]

log = logging.getLogger(__name__)

ENVIRONMENTS = ("demo", "prod")
MAX_PEM_CHARS = 16_000
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")


@dataclass(frozen=True)
class StoredKey:
    environment: str
    api_key_id: str
    private_key_pem: str
    saved_at: str


def mask_key_id(key_id: str) -> str:
    return f"{key_id[:4]}…{key_id[-4:]}" if len(key_id) > 10 else "…"


def fingerprint(private_key: Any) -> str:
    """SHA-256 of the public key (DER), first 16 hex digits: safe to show."""
    from cryptography.hazmat.primitives import serialization

    der = private_key.public_key().public_bytes(serialization.Encoding.DER,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashlib.sha256(der).hexdigest()[:16]
    return ":".join(h[i:i + 4] for i in range(0, 16, 4))


def parse_private_key(pem: str) -> Any:
    """Validate a pasted/uploaded PEM; raises ``ValueError`` with a message that never contains
    the key material."""
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    text = (pem or "").strip()
    if not text:
        raise ValueError("the private key is empty")
    if len(text) > MAX_PEM_CHARS:
        raise ValueError("the private key is too long to be a PEM key")
    if "PRIVATE KEY-----" not in text:
        raise ValueError("not a PEM private key (expected a '-----BEGIN ... PRIVATE KEY-----' block)")
    if "ENCRYPTED" in text:
        raise ValueError("the private key is password-protected; export it without a password")
    try:
        key = load_private_key(pem=text)
    except Exception:
        raise ValueError("the private key could not be read (is it the whole PEM file?)") from None
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise ValueError(f"RSA key too small ({key.key_size} bits)")
    elif not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"unsupported key type {type(key).__name__} (Kalshi uses RSA or Ed25519)")
    return key


def validate_key_id(key_id: str) -> str:
    k = (key_id or "").strip()
    if not KEY_ID_RE.match(k):
        raise ValueError("the API key id should be the id Kalshi shows next to the key (letters, digits, '-')")
    return k


class CredentialStore:
    """The dashboard's key file. Thread-unsafe by design: one server process owns it."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    # -- file -------------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            log.warning("%s was readable by other users (mode %o); tightening to 0600", self.path, mode)
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            log.error("cannot read the stored API keys (%s): %s", self.path, type(e).__name__)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        d = self.path.parent
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)
        fd, tmp = tempfile.mkstemp(prefix=".keys-", dir=d)  # created 0600
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- api --------------------------------------------------------------------------

    def get(self, environment: str) -> StoredKey | None:
        row = self._read().get(environment)
        if not isinstance(row, dict) or not row.get("api_key_id") or not row.get("private_key_pem"):
            return None
        return StoredKey(environment, str(row["api_key_id"]), str(row["private_key_pem"]), str(row.get("saved_at", "")))

    def put(self, environment: str, api_key_id: str, private_key_pem: str) -> StoredKey:
        if environment not in ENVIRONMENTS:
            raise ValueError(f"unknown environment {environment!r}")
        key_id = validate_key_id(api_key_id)
        parse_private_key(private_key_pem)
        data = self._read()
        saved = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        data[environment] = {"api_key_id": key_id, "private_key_pem": private_key_pem.strip() + "\n",
                             "saved_at": saved}
        self._write(data)
        log.info("stored a Kalshi %s API key (%s)", environment, mask_key_id(key_id))
        return StoredKey(environment, key_id, data[environment]["private_key_pem"], saved)

    def delete(self, environment: str) -> bool:
        data = self._read()
        if environment not in data:
            return False
        del data[environment]
        if data:
            self._write(data)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
        log.info("removed the stored Kalshi %s API key", environment)
        return True

    def summary(self, environment: str) -> dict[str, Any] | None:
        """What the dashboard may see about a stored key (never the key)."""
        k = self.get(environment)
        if k is None:
            return None
        try:
            fp = fingerprint(load_private_key(pem=k.private_key_pem))
        except Exception:
            fp = None
        return {"api_key_id": mask_key_id(k.api_key_id), "fingerprint": fp, "saved_at": k.saved_at or None}
