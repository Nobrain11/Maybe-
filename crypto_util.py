"""
Encrypts/decrypts user-supplied Robinhood credentials before they touch
disk. Uses Fernet (AES-128-CBC + HMAC) from the `cryptography` package.

The encryption key (BOT_MASTER_KEY) must be set in .env and kept secret -
anyone with it can decrypt every stored user credential. Losing it means
every user has to /connect again (there's no recovery without it).

Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_MASTER_KEY = os.getenv("BOT_MASTER_KEY")
_fernet: Fernet | None = None

if _MASTER_KEY:
    _fernet = Fernet(_MASTER_KEY.encode())


class EncryptionNotConfigured(Exception):
    pass


def _require_fernet() -> Fernet:
    if _fernet is None:
        raise EncryptionNotConfigured(
            "BOT_MASTER_KEY is not set in .env. Generate one with:\n"
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return _fernet


def encrypt(plaintext: str) -> str:
    f = _require_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    f = _require_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Could not decrypt stored credential - BOT_MASTER_KEY may have "
            "changed since it was saved."
        ) from e
