"""The vault: every key, token and password a connection needs, encrypted (AES-256-GCM).

The ciphertext lives in Postgres (connection_secrets). The key lives in one file, /data/vault.key, in a
folder only Lagrange mounts (./data/lagrange on the server), made the first time Lagrange starts. So the
orchestrator, which talks to the model, can read the rows but can't open them, and nothing that reaches
the model, a log, a display or the record ever holds a secret.

Each secret is bound to its connection and slot (the GCM associated data), so a row copied to another
connection or slot won't open. Lose the key file and the secrets are gone: you enter them again.
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
                             "(the saved secrets will need entering again).")
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
    def _ad(connection_id: int, slot: str) -> bytes:
        return f"{int(connection_id)}:{slot}".encode()

    def seal(self, connection_id: int, slot: str, value: str) -> tuple[bytes, bytes]:
        nonce = _secrets.token_bytes(12)
        return nonce, self._aead.encrypt(nonce, value.encode(), self._ad(connection_id, slot))

    def open(self, connection_id: int, slot: str, nonce: bytes, box: bytes) -> str:
        try:
            return self._aead.decrypt(bytes(nonce), bytes(box), self._ad(connection_id, slot)).decode()
        except Exception:
            raise VaultError(f"a saved secret ({slot}) won't open: the vault key changed. Enter it again.") from None
