"""API Key 加密与解密：使用 Fernet 对称加密。"""

import hashlib
import hmac
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from backend.config import BASE_DIR, AppConfig, load_config


SECRET_FILE = BASE_DIR / ".secret_key"
logger = logging.getLogger("agent.security")

_lock = threading.Lock()
_fernet = None


def _validate_key(key: bytes, source: str) -> bytes:
    """验证并原样返回标准 Fernet 编码。"""
    try:
        Fernet(key)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{source} 不是有效的 Fernet 密钥") from exc
    return key


def _create_posix_key_file() -> bytes:
    """完整写入临时文件后，以不覆盖方式原子发布 0600 密钥文件。"""
    key = Fernet.generate_key()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{SECRET_FILE.name}.",
        dir=SECRET_FILE.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())

        try:
            os.link(temporary_path, SECRET_FILE)
            logger.info("已生成加密密钥文件：%s", SECRET_FILE)
        except FileExistsError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)

    return _validate_key(SECRET_FILE.read_bytes(), str(SECRET_FILE))


def _load_key() -> bytes:
    """优先验证 APP_SECRET_KEY，否则读取或安全创建本地密钥文件。"""
    if "APP_SECRET_KEY" in os.environ:
        try:
            env_key = os.environ["APP_SECRET_KEY"].encode("ascii")
        except UnicodeEncodeError as exc:
            raise RuntimeError("APP_SECRET_KEY 必须是 ASCII Fernet 密钥") from exc
        return _validate_key(env_key, "APP_SECRET_KEY")

    try:
        return _validate_key(SECRET_FILE.read_bytes(), str(SECRET_FILE))
    except FileNotFoundError:
        pass

    if sys.platform == "win32":
        raise RuntimeError(
            "Windows 首次启动必须设置 APP_SECRET_KEY；本阶段不自动创建密钥文件"
        )
    return _create_posix_key_file()


def _get_fernet() -> Fernet:
    """线程安全地创建 Fernet 实例。"""
    global _fernet
    if _fernet is None:
        with _lock:
            if _fernet is None:
                _fernet = Fernet(_load_key())
    return _fernet


def validate_secret_key() -> None:
    """供应用启动阶段主动验证或初始化 Fernet 密钥。"""
    _get_fernet()


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


def verify_frozen_provider(snapshot: dict, *, settings: dict | None,
                           runtime_config: AppConfig) -> bool:
    """每次模型 I/O 前核对 Run 冻结配置与当前可信凭据，不返回密钥。"""
    from backend.services import credential_origin, normalize_base_url

    try:
        if not isinstance(snapshot, dict) or type(snapshot.get("v")) is not int or snapshot["v"] != 1:
            return False
        settings = settings or {}
        environment = load_config()
        frozen_url = normalize_base_url(snapshot["base_url"])
        current_url = normalize_base_url(runtime_config.base_url)
        selected_url = normalize_base_url(settings.get("base_url") or environment.base_url)
        if frozen_url != current_url or current_url != selected_url:
            return False
        if snapshot.get("model") != runtime_config.model or runtime_config.model != (
            settings.get("model") or environment.model
        ):
            return False
        current_origin = credential_origin(current_url)
        if snapshot.get("credential_origin") not in (None, list(current_origin), current_origin):
            return False
        key = runtime_config.api_key
        if not isinstance(key, str) or not key:
            return False
        encrypted = settings.get("api_key_encrypted")
        if encrypted:
            bound_url = settings.get("credential_base_url")
            if not bound_url or credential_origin(bound_url) != current_origin:
                return False
            if not hmac.compare_digest(decrypt_text(encrypted), key):
                return False
            fingerprint_source = encrypted
        else:
            if credential_origin(environment.base_url) != current_origin:
                return False
            if not hmac.compare_digest(environment.api_key, key):
                return False
            fingerprint_source = key
        expected = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        fingerprint = snapshot.get("credential_fingerprint")
        return isinstance(fingerprint, str) and hmac.compare_digest(fingerprint, expected)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, UnicodeError):
        return False
