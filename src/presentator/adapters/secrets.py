"""The one place a secret is turned into bytes a row may hold (ADR 0013).

Every parameter of the encryption stands here and nowhere else, so no caller
picks a mode, a nonce or a key length; a caller hands in a secret and gets the
bytes back, or hands in bytes and gets the secret back.
"""

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# The salt is a constant of this product rather than a secret: the instance key
# is the only secret input, and a salt that varied per row would have to be
# stored beside every value it salted.
_SALT: Final = b"presentator/secrets/v1"
# What this key is for, so the instance key's other users — the session cookie's
# HMAC among them — never derive the same bytes.
_PURPOSE: Final = b"presentator/source-access-secret/v1"
_KEY_LENGTH: Final = 32


@dataclass(frozen=True, slots=True, kw_only=True)
class SecretBox:
    """Encrypts one secret of this instance, and opens what this instance wrote."""

    cipher: Fernet

    def encrypt(self, secret: str) -> bytes:
        """The bytes to write down: authenticated, and useless without the key."""
        return self.cipher.encrypt(secret.encode())

    def decrypt(self, stored: bytes) -> str | None:
        """The secret those bytes hold, or nothing when this box did not write them.

        Bytes another key wrote, or bytes anybody changed since, are refused
        rather than answered with whatever they decode to; the caller reads
        that as a secret it cannot resolve.
        """
        try:
            return self.cipher.decrypt(stored).decode()
        except InvalidToken:
            return None


def secret_box(instance_key: str) -> SecretBox:
    """This instance's box, keyed by what only this instance carries.

    The key is derived rather than used raw, so the one secret an operator sets
    stays one secret while every use of it holds different key material.
    """
    derived = HKDF(
        algorithm=SHA256(),
        length=_KEY_LENGTH,
        salt=_SALT,
        info=_PURPOSE,
    ).derive(instance_key.encode())
    return SecretBox(cipher=Fernet(urlsafe_b64encode(derived)))
