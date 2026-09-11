"""业务编排：合并环境变量配置与数据库设置。"""

import ipaddress
import logging
import re
from urllib.parse import quote, urlsplit, urlunsplit

from backend.config import AppConfig, load_config
from backend.db import db
from backend.security import decrypt_text


logger = logging.getLogger("agent.services")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _canonical_host(raw_host: str) -> tuple[str, bool]:
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Base URL host 不是有效的 IDNA 名称") from exc
        if host.endswith("."):
            host = host[:-1]
        labels = host.split(".")
        if (
            not host
            or len(host) > 253
            or any(not _HOST_LABEL.fullmatch(label) for label in labels)
        ):
            raise ValueError("Base URL host 无效")
        return host, host == "localhost"
    return address.compressed, address.is_loopback


def _parse_base_url(base_url: str) -> tuple[str, tuple[str, str, int]]:
    if (
        not isinstance(base_url, str)
        or not base_url
        or base_url != base_url.strip()
        or any(char.isspace() for char in base_url)
        or "\\" in base_url
        or "?" in base_url
        or "#" in base_url
    ):
        raise ValueError("Base URL 格式无效")

    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise ValueError("Base URL 格式无效") from exc
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parsed.netloc or parsed.hostname is None:
        raise ValueError("Base URL 必须使用 http 或 https 并包含 host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Base URL 不得包含 userinfo")
    if parsed.netloc.endswith(":"):
        raise ValueError("Base URL port 无效")

    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise ValueError("Base URL port 无效") from exc
    port = explicit_port if explicit_port is not None else _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        raise ValueError("Base URL port 无效")

    host, is_loopback = _canonical_host(parsed.hostname)
    if scheme == "http" and not is_loopback:
        raise ValueError("非 loopback Base URL 必须使用 https")
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
        raise ValueError("Base URL path 包含无效转义")

    authority = f"[{host}]" if ":" in host else host
    if port != _DEFAULT_PORTS[scheme]:
        authority = f"{authority}:{port}"
    path = quote(parsed.path, safe="/:@!$&'()*+,;=-._~%").rstrip("/")
    normalized = urlunsplit((scheme, authority, path, "", ""))
    return normalized, (scheme, host, port)


def normalize_base_url(base_url: str) -> str:
    """严格验证并返回 Provider Base URL 的规范形式。"""
    return _parse_base_url(base_url)[0]


def credential_origin(base_url: str) -> tuple[str, str, int]:
    """返回凭据绑定使用的 scheme、IDNA host 与有效端口。"""
    return _parse_base_url(base_url)[1]


def get_runtime_config() -> AppConfig:
    """返回实际运行配置，且只组合与目标 origin 匹配的凭据。"""
    cfg = load_config()
    environment_url, environment_origin = _parse_base_url(cfg.base_url)
    settings = db.get_settings()
    if not settings:
        result = AppConfig(
            api_key=cfg.api_key,
            base_url=environment_url,
            model=cfg.model,
            max_rounds=cfg.max_rounds,
            tool_timeout=cfg.tool_timeout,
        )
        logger.info(
            "运行配置来源=environment，base_url=%s，model=%s，max_rounds=%d，"
            "tool_timeout=%.1f，api_key_configured=%s",
            result.base_url,
            result.model,
            result.max_rounds,
            result.tool_timeout,
            bool(result.api_key),
        )
        return result

    base_url, selected_origin = _parse_base_url(
        settings.get("base_url") or environment_url
    )
    encrypted_key = settings.get("api_key_encrypted")
    api_key = ""
    if encrypted_key:
        credential_base_url = settings.get("credential_base_url")
        if not credential_base_url:
            logger.warning("数据库 API Key 缺少可信 origin 绑定，拒绝使用")
        else:
            try:
                bound_origin = credential_origin(credential_base_url)
            except ValueError:
                logger.warning("数据库 API Key 的 origin 绑定无效，拒绝使用")
            else:
                if bound_origin != selected_origin:
                    logger.warning("数据库 API Key 与目标 origin 不匹配，拒绝使用")
                else:
                    try:
                        api_key = decrypt_text(encrypted_key)
                    except ValueError as exc:
                        logger.warning("数据库 API Key 解密失败，拒绝使用：%s", exc)
    elif selected_origin == environment_origin:
        api_key = cfg.api_key
    elif cfg.api_key:
        logger.warning("环境 API Key 与数据库 Base URL origin 不匹配，拒绝使用")

    result = AppConfig(
        api_key=api_key,
        base_url=base_url,
        model=settings.get("model") or cfg.model,
        max_rounds=settings.get("max_rounds") or cfg.max_rounds,
        tool_timeout=settings.get("tool_timeout") or cfg.tool_timeout,
    )
    logger.info(
        "运行配置来源=database，base_url=%s，model=%s，max_rounds=%d，"
        "tool_timeout=%.1f，api_key_configured=%s",
        result.base_url,
        result.model,
        result.max_rounds,
        result.tool_timeout,
        bool(result.api_key),
    )
    return result


def get_public_settings() -> dict:
    """返回前端设置面板需要的信息；只报告可安全使用的密钥。"""
    cfg = get_runtime_config()
    result = {
        "base_url": cfg.base_url,
        "model": cfg.model,
        "max_rounds": cfg.max_rounds,
        "tool_timeout": cfg.tool_timeout,
        "api_key_configured": bool(cfg.api_key),
    }
    logger.info("公开设置输出：%s", result)
    return result
