"""AES-256-GCM encryption for stored API keys.

The key material comes from the ENCRYPTION_KEY env var (never from the DB).
Ciphertext format: base64url(nonce(12) || ciphertext+tag).
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyBox:
    def __init__(self, secret: str):
        # Derive a fixed 32-byte AES key from the operator secret.
        self._aes = AESGCM(hashlib.sha256(("folk-assistant-bot:" + secret).encode()).digest())

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), b"api-key")
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        try:
            return self._aes.decrypt(raw[:12], raw[12:], b"api-key").decode("utf-8")
        except InvalidTag as exc:  # wrong ENCRYPTION_KEY or tampered value
            raise ValueError("could not decrypt stored key (ENCRYPTION_KEY changed?)") from exc
