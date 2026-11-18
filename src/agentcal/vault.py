"""Local encrypted storage for intent payloads (AES-GCM, key from Keychain)."""
from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"agentcal-intent-v1"


class Vault:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("vault key must be 32 bytes")
        self._aead = AESGCM(key)

    def seal(self, obj: dict) -> bytes:
        nonce = os.urandom(12)
        data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return nonce + self._aead.encrypt(nonce, data, AAD)

    def open(self, blob: bytes) -> dict:
        nonce, ct = blob[:12], blob[12:]
        return json.loads(self._aead.decrypt(nonce, ct, AAD))
