from cryptography.fernet import Fernet, InvalidToken

from .config import FERNET_KEY

_fernet = Fernet(FERNET_KEY.encode("utf-8"))


def encrypt_refresh_token(token: str) -> str:
    return _fernet.encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_refresh_token(token_enc: str) -> str:
    try:
        return _fernet.decrypt(token_enc.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored refresh token could not be decrypted") from exc
