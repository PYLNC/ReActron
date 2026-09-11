"""应用配置：统一从环境变量读取，避免各处重复解析。"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("agent.config")


@dataclass(frozen=True)
class AppConfig:
    """不可变配置对象，启动后全局共享。"""

    api_key: str
    base_url: str
    model: str
    max_rounds: int
    tool_timeout: float
    workers: int = 1


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """读取整数环境变量，非法值时回退到默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s 不是有效整数，使用默认值 %s", name, default)
        return default
    return max(value, minimum)


def _float_env(name: str, default: float, minimum: float = 0.1) -> float:
    """读取浮点环境变量，非法值时回退到默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s 不是有效数字，使用默认值 %s", name, default)
        return default
    return max(value, minimum)


def _single_worker_env() -> int:
    """P0 的进程内执行 gate 只允许一个应用进程。"""
    raw = os.getenv("WEB_CONCURRENCY", "1").strip() or "1"
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("WEB_CONCURRENCY 必须为 1") from exc
    if workers != 1:
        raise ValueError("WEB_CONCURRENCY 必须为 1")
    return workers


def load_config() -> AppConfig:
    """从环境变量构造 AppConfig。"""
    return AppConfig(
        api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip(),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        max_rounds=_int_env("DEEPSEEK_MAX_ROUNDS", 5),
        tool_timeout=_float_env("DEEPSEEK_TOOL_TIMEOUT", 10.0),
        workers=_single_worker_env(),
    )
