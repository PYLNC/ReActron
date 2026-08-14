"""业务编排：合并环境变量配置与数据库设置。"""

import logging

from backend.config import AppConfig, load_config
from backend.db import db
from backend.security import decrypt_text


logger = logging.getLogger("agent.services")


def get_runtime_config() -> AppConfig:
    """返回实际运行配置，数据库中的前端设置优先于 .env / 系统环境变量。"""
    cfg = load_config()
    settings = db.get_settings()
    if not settings:
        logger.info(
            "运行配置来源=environment，base_url=%s，model=%s，max_rounds=%d，"
            "tool_timeout=%.1f，api_key_configured=%s",
            cfg.base_url,
            cfg.model,
            cfg.max_rounds,
            cfg.tool_timeout,
            bool(cfg.api_key),
        )
        return cfg

    api_key = cfg.api_key
    if settings.get("api_key_encrypted"):
        try:
            api_key = decrypt_text(settings["api_key_encrypted"])
        except ValueError as exc:
            logger.warning("数据库 API Key 解密失败，使用环境变量：%s", exc)

    result = AppConfig(
        api_key=api_key,
        base_url=settings.get("base_url") or cfg.base_url,
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
    """返回前端设置面板需要的信息；密钥只来自数据库或正式环境配置。"""
    cfg = load_config()
    settings = db.get_settings() or {}
    result = {
        "base_url": settings.get("base_url") or cfg.base_url,
        "model": settings.get("model") or cfg.model,
        "max_rounds": settings.get("max_rounds") or cfg.max_rounds,
        "tool_timeout": settings.get("tool_timeout") or cfg.tool_timeout,
        "api_key_configured": bool(settings.get("api_key_encrypted") or cfg.api_key),
    }
    logger.info("公开设置输出：%s", result)
    return result
