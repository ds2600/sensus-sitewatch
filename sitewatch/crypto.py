"""Encrypt/decrypt device credentials at rest using ENCRYPTION_KEY from .env.

ENCRYPTION_KEY is a hex string (from secrets.token_hex(32)). Fernet needs a
32-byte urlsafe-base64 key, so we derive one from it deterministically.
"""
import base64
import os
from cryptography.fernet import Fernet

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        raw_key = os.environ["ENCRYPTION_KEY"]
        derived = base64.urlsafe_b64encode(raw_key.encode("utf-8")[:32].ljust(32, b"0"))
        _fernet = Fernet(derived)
    return _fernet


def encrypt(plaintext):
    if plaintext is None or plaintext == "":
        return None
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext):
    if ciphertext is None or ciphertext == "":
        return None
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
