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
    """先冻结 MCP 注册表再启动 Runner，关闭时按相反顺序。"""
    global _api_router_loaded

    cfg = load_config()
    from backend.security import validate_secret_key

    validate_secret_key()
    application.state.config = cfg

    # 密钥验证成功后才允许导入会打开 DB/MCP 的现有模块。
    from backend import api as api_module
    from backend.db import DB_PATH, db
    from backend.workspace import initialize_workspace_state

    db.initialize()
    initialize_workspace_state()

    if not _api_router_loaded:
        application.include_router(api_module.public_router)
        application.include_router(api_module.router)
        _api_router_loaded = True

    logger.info("应用启动：max_rounds=%d，db=%s", cfg.max_rounds, DB_PATH)

    from backend.mcp.manager import get_manager
    manager = get_manager()
    try:
        await manager.refresh_registry()
        logger.info("MCP 启动刷新完成")
    except Exception:
        # A partial/failed refresh must never leave executable MCP specs visible.
        manager.clear_registry()
        logger.warning("MCP 启动刷新失败；本次启动禁用全部 MCP 工具")

    from backend.runtime import get_runner
    runner = get_runner()
    try:
        await runner.start()
        from backend.auth import issue_bootstrap_secret

        secret = issue_bootstrap_secret()
        print(f"[ReActron] One-time local bootstrap secret: {secret}", file=sys.stderr, flush=True)
        yield
    finally:
        try:
            await runner.close()
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
    code = {
        400: "invalid_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        413: "upload_limit_exceeded",
        415: "upload_type_mismatch",
    }.get(exc.status_code, "http_error")
    message = {
        401: "请先完成本地认证",
        403: "无权执行此操作",
        404: "资源不存在",
        413: "上传文件超过限制",
        415: "上传文件类型不符",
    }.get(exc.status_code, str(exc.detail))
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
    errors = [
        {"type": error["type"], "loc": error["loc"], "message": error["msg"]}
        for error in exc.errors()
    ]
    logger.warning("请求校验失败：path=%s，errors=%d", request.url.path, len(errors))
    return JSONResponse(
        status_code=422,
        content={
            "code": "request_validation_error",
            "message": "请求参数校验失败",
            "details": {"errors": errors},
            "trace_id": uuid.uuid4().hex,
        },
    )


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception):
    """未处理的异常只向客户端暴露稳定错误码与追踪号。"""
    trace_id = uuid.uuid4().hex
    logger.error("请求失败：trace_id=%s path=%s", trace_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "服务器内部错误",
            "details": {},
            "trace_id": trace_id,
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
