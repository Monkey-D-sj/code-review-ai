import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config.setting import settings
from app.core.exceptions import CustomException

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """惰性构建 Fernet 实例，密钥由 settings.SECRET_KEY 派生（SHA-256 → url-safe base64）。"""
    global _fernet
    if _fernet is None:
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


def encrypt_password(plain: str | None) -> str:
    """明文密码 → Fernet 密文。空值原样返回空串。"""
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_password(cipher: str | None) -> str:
    """Fernet 密文 → 明文密码。空值原样返回空串。"""
    if not cipher:
        return ""
    try:
        return _get_fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise CustomException(msg="存储源密码解密失败，可能原因：SECRET_KEY 变更或数据损坏")
