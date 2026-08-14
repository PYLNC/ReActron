"""API Key 加密与解密：使用 Fernet 对称加密。"""

import base64
import logging
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from backend.config import BASE_DIR


SECRET_FILE = BASE_DIR / ".secret_key"
logger = logging.getLogger("agent.security")

_lock = threading.Lock()
_fernet = None


def _load_key() -> bytes:
    """优先读取 APP_SECRET_KEY，否则自动生成并保存密钥文件。"""
    env_key = os.getenv("APP_SECRET_KEY", "").strip()
    if env_key:
        try:
            return base64.urlsafe_b64decode(env_key.encode("ascii"))
        except Exception:
            logger.warning("APP_SECRET_KEY 不是有效的 base64，回退到本地密钥文件")

    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()

    key = Fernet.generate_key()
    SECRET_FILE.write_bytes(key)
    logger.info("已生成加密密钥文件：%s", SECRET_FILE)
    return key


def _get_fernet() -> Fernet:
    """线程安全地创建 Fernet 实例。"""
    global _fernet
    if _fernet is None:
        with _lock:
            if _fernet is None:
                _fernet = Fernet(_load_key())
    return _fernet


def encrypt_text(plain_text: str) -> str:
    """加密明文，返回可写入数据库的字符串。"""
    token = _get_fernet().encrypt(plain_text.encode("utf-8"))
    logger.info("API Key 加密输入长度=%d，输出已加密", len(plain_text))
    return token.decode("ascii")


def decrypt_text(token: str) -> str:
    """解密数据库中的密文。"""
    try:
        plain_text = _get_fernet().decrypt(token.encode("ascii"))
        logger.info("API Key 解密输出长度=%d", len(plain_text.decode("utf-8")))
        return plain_text.decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("API Key 解密失败，可能加密密钥已变化") from exc
