"""FastAPI 入口：应用组装、静态托管、访问日志与全局异常处理。"""

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_STREAM = os.getenv("LOG_STREAM", "1") == "1"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("agent.app")
logger.info("日志配置：LOG_LEVEL=%s，LOG_STREAM=%s", LOG_LEVEL, LOG_STREAM)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backend.api import router
from backend.config import load_config
from backend.db import DB_PATH


# 只加载正式 .env；.env.example 只是模板，不再作为运行配置读取。
load_dotenv(BASE_DIR / ".env")


def _warn_if_secret_in_example():
    """如果示例文件里误存了真实密钥，给出提醒而不是直接使用它。"""
    example_file = BASE_DIR / ".env.example"
    if not example_file.exists():
        return
    for line in example_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("DEEPSEEK_API_KEY="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value and value != "sk-你的密钥":
                logger.warning(
                    "检测到 .env.example 中保存了真实 API Key，后端已忽略该文件；"
                    "请在前端“设置”或 .env 中配置密钥"
                )
            return


_warn_if_secret_in_example()

app = FastAPI(title="ReActron Agent 框架", version="0.9.0")

cfg = load_config()
logger.info(
    "FastAPI 应用初始化完成，BASE_DIR=%s，model=%s，max_rounds=%d，db=%s",
    BASE_DIR,
    cfg.model,
    cfg.max_rounds,
    DB_PATH,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(router)


@app.on_event("startup")
async def startup_mcp():
    """启动时自动刷新 MCP 工具注册。"""
    from backend.mcp.manager import get_manager

    try:
        result = await get_manager().refresh_registry()
        logger.info("MCP 启动刷新完成：%s", result)
    except Exception as exc:
        logger.warning("MCP 启动刷新失败：%s", exc)


@app.get("/")
async def index():
    """返回聊天页面入口。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """把 Pydantic 校验错误转换成统一 JSON 格式。"""
    logger.warning("请求校验失败：%s", exc.errors())
    return JSONResponse(
        status_code=422,
        content={"error": "请求参数校验失败", "detail": exc.errors()},
    )


@app.middleware("http")
async def access_log(request: Request, call_next):
    """记录每次 HTTP 请求的方法、路径、状态码和耗时。"""
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "%s %s from %s -> %s %.1fms",
        request.method,
        request.url.path,
        request.client.host if request.client else "-",
        response.status_code,
        duration_ms,
    )
    return response
