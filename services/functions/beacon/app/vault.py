"""Beacon's vault: your Gmail app password, the calendar's password, your Google calendar's secret address and
your ntfy topic, encrypted (AES-256-GCM).

The ciphertext lives in Postgres (beacon_secrets); the key in one file, /data/vault.key, in a folder only Beacon
mounts (./data/beacon on the server), made the first time Beacon starts. The orchestrator, which talks to the
model, can read the rows but can't open them. Each secret is bound to its slot (the GCM associated data), so a
row copied to another slot won't open. Lose the key file and you enter them again on the setup form.
"""
import os
import secrets as _secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32


class VaultError(Exception):
    pass


def load_key(path: str | Path) -> bytes:
    """The vault's key, made once. Two copies starting together (a deploy) agree: only one can create it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = p.read_bytes()
        if len(key) != KEY_BYTES:
            raise VaultError(f"{p} isn't a vault key ({len(key)} bytes). Move it aside to start a new vault "
                             "(you'll enter the passwords again).")
        return key
    key = _secrets.token_bytes(KEY_BYTES)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


class Vault:
    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise VaultError("the vault key must be 32 bytes")
        self._aead = AESGCM(key)

    @staticmethod
    def _ad(slot: str) -> bytes:
        return f"beacon:{slot}".encode()

    def seal(self, slot: str, value: str) -> tuple[bytes, bytes]:
        nonce = _secrets.token_bytes(12)
        return nonce, self._aead.encrypt(nonce, value.encode(), self._ad(slot))

    def open(self, slot: str, nonce: bytes, box: bytes) -> str:
        try:
            return self._aead.decrypt(bytes(nonce), bytes(box), self._ad(slot)).decode()
        except Exception:
            raise VaultError(f"a saved secret ({slot}) won't open: the vault key changed. Enter it again on the "
                             "setup form (\"set up my email\").") from None
