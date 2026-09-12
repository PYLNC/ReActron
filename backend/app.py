"""FastAPI 入口：应用组装、静态托管、访问日志与全局异常处理。"""

import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# 配置文件必须先于日志设置和会创建运行资源的模块导入。
load_dotenv(BASE_DIR / ".env")

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.config import load_config


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_STREAM = os.getenv("LOG_STREAM", "1") == "1"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("agent.app")
logger.info("日志配置：LOG_LEVEL=%s，LOG_STREAM=%s", LOG_LEVEL, LOG_STREAM)

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


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
_api_router_loaded = False


@asynccontextmanager
async def lifespan(application: FastAPI):
    """按配置、数据库和 MCP 的现有边界启动，并反序关闭。"""
    global _api_router_loaded

    cfg = load_config()
    from backend.security import validate_secret_key

    validate_secret_key()
    application.state.config = cfg

    # 密钥验证成功后才允许导入会打开 DB/MCP 的现有模块。
    from backend import api as api_module
    from backend.db import DB_PATH, db

    if not _api_router_loaded:
        application.include_router(api_module.router)
        _api_router_loaded = True

    logger.info(
        "应用启动：model=%s，max_rounds=%d，db=%s",
        cfg.model,
        cfg.max_rounds,
        DB_PATH,
    )

    from backend.mcp.manager import get_manager

    try:
        try:
            result = await get_manager().refresh_registry()
            logger.info("MCP 启动刷新完成：%s", result)
        except Exception as exc:
            logger.warning("MCP 启动刷新失败：%s", exc)
        yield
    finally:
        db.close()
        logger.info("应用关闭：数据库连接已关闭")


app = FastAPI(title="ReActron Agent 框架", version="0.9.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    """返回聊天页面入口。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """将现有路由和框架的 HTTP 错误统一为 P0 错误体。"""
    code = {400: "invalid_request", 403: "forbidden", 404: "not_found"}.get(
        exc.status_code, "http_error"
    )
    message = "资源不存在" if exc.status_code == 404 else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": code,
            "message": message,
            "details": {},
            "trace_id": uuid.uuid4().hex,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """把 Pydantic 校验错误转换成统一 JSON 格式。"""
    details = [
        {"type": error["type"], "loc": error["loc"], "message": error["msg"]}
        for error in exc.errors()
    ]
    logger.warning("请求校验失败：path=%s，errors=%d", request.url.path, len(details))
    return JSONResponse(
        status_code=422,
        content={
            "code": "request_validation_error",
            "message": "请求参数校验失败",
            "details": details,
            "trace_id": uuid.uuid4().hex,
        },
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
