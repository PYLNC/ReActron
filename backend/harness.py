"""安全与自检 Harness：路径策略、工具调用检查、审计和自测。"""

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backend.config import BASE_DIR
from backend.db import db


logger = logging.getLogger("agent.harness")


class HarnessError(Exception):
    """Harness 拒绝工具调用时抛出的异常。"""


@dataclass(frozen=True)
class PathPolicy:
    """文件路径安全策略。"""

    allowed_roots: tuple = ()
    allowed_extensions: tuple = (
        ".xlsx",
        ".xls",
        ".csv",
        ".pptx",
        ".ppt",
        ".md",
        ".txt",
        ".json",
    )
    max_file_size: int = 20 * 1024 * 1024
    deny_names: tuple = (
        ".secret_key",
        "countboter.db",
        "countboter.db-wal",
        "countboter.db-shm",
        ".env",
        ".env.example",
    )

    @classmethod
    def from_env(cls):
        """从环境变量构建路径策略。"""
        roots = [BASE_DIR, BASE_DIR / "data"]
        extra = os.getenv("HARNESS_ALLOWED_ROOTS", "").strip()
        if extra:
            for item in re.split(r"[;,]", extra):
                item = item.strip()
                if item:
                    roots.append(Path(item).expanduser().resolve())

        try:
            max_size = int(os.getenv("HARNESS_MAX_FILE_SIZE", str(20 * 1024 * 1024)))
        except ValueError:
            max_size = 20 * 1024 * 1024

        return cls(
            allowed_roots=tuple(roots),
            max_file_size=max(1, max_size),
        )

    def resolve(self, raw_path: str, operation: str = "file"):
        """解析并校验路径，非法时抛出 HarnessError。"""
        path = Path(raw_path).expanduser().resolve()

        for part in path.parts:
            if part in self.deny_names:
                raise HarnessError(f"禁止访问敏感文件: {part}")

        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise HarnessError(f"路径不在允许目录内: {path}")

        if operation != "list_dir":
            if path.suffix.lower() not in self.allowed_extensions:
                raise HarnessError(f"文件类型不允许: {path.suffix or '无扩展名'}")
            if path.exists() and path.stat().st_size > self.max_file_size:
                raise HarnessError(f"文件超过大小限制: {path}")

        return path


def _find_path_argument(arguments: dict):
    """从工具参数中提取可能涉及文件的路径。"""
    for key in ("file_path", "path"):
        value = arguments.get(key)
        if value:
            return str(value)
    return None


class Harness:
    """工具调用安全闸门，支持 monitor 和 enforce 两种模式。"""

    def __init__(self, mode=None, policy=None, record=True):
        self.mode = (mode or os.getenv("HARNESS_MODE", "monitor")).lower()
        self.policy = policy or PathPolicy.from_env()
        self.record = record
        logger.info(
            "Harness 已初始化，mode=%s，allowed_roots=%s，max_file_size=%d",
            self.mode,
            [str(root) for root in self.policy.allowed_roots],
            self.policy.max_file_size,
        )

    def _record(self, tool_name, arguments, result, ok):
        if not self.record:
            return
        try:
            db.add_audit_log(
                action="tool_call",
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                ok=ok,
            )
        except Exception as exc:
            logger.warning("Harness 审计写入失败：%s", exc)

    def check_tool_call(self, name: str, arguments: dict):
        """执行工具前检查路径参数，monitor 模式放行，enforce 模式拦截。"""
        raw_path = _find_path_argument(arguments)
        if not raw_path:
            return
        try:
            self.policy.resolve(raw_path, operation=name)
        except HarnessError as exc:
            logger.warning("Harness 拒绝工具调用：name=%s，error=%s", name, exc)
            self._record(name, arguments, {"error": str(exc)}, False)
            if self.mode == "enforce":
                raise
            logger.warning("当前为 monitor 模式，已放行该调用")

    def record_result(self, name: str, arguments: dict, result, ok: bool):
        """记录一次工具执行的审计结果。"""
        self._record(name, arguments, result, ok)

    def status(self) -> dict:
        """返回 Harness 当前配置状态。"""
        return {
            "mode": self.mode,
            "allowed_roots": [str(root) for root in self.policy.allowed_roots],
            "allowed_extensions": list(self.policy.allowed_extensions),
            "max_file_size": self.policy.max_file_size,
            "audit_count": db.count_audit_logs(),
        }


_harness = None


def get_harness() -> Harness:
    """返回全局 Harness 单例。"""
    global _harness
    if _harness is None:
        _harness = Harness()
    return _harness


def run_self_tests() -> list:
    """运行内置自检，返回每条测试的结果。"""
    results = []

    def record(name, ok, message=""):
        results.append({"name": name, "ok": ok, "message": message})

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        policy = PathPolicy(allowed_roots=(root,), allowed_extensions=(".xlsx",))

        try:
            policy.resolve(str(root / "ok.xlsx"), operation="excel_create_file")
            record("允许目录内文件", True)
        except HarnessError as exc:
            record("允许目录内文件", False, str(exc))

        outside = Path(tempfile.gettempdir()) / "harness_outside_test.xlsx"
        try:
            policy.resolve(str(outside), operation="excel_create_file")
            record("拒绝目录外文件", False, "未被拦截")
        except HarnessError:
            record("拒绝目录外文件", True)

        try:
            policy.resolve(str(root / "bad.txt"), operation="excel_create_file")
            record("拒绝非法扩展名", False, "未被拦截")
        except HarnessError:
            record("拒绝非法扩展名", True)

        try:
            policy.resolve(str(root / ".secret_key"), operation="excel_create_file")
            record("拒绝敏感文件名", False, "未被拦截")
        except HarnessError:
            record("拒绝敏感文件名", True)

        monitor = Harness(mode="monitor", policy=policy, record=False)
        try:
            monitor.check_tool_call("excel_create_file", {"file_path": str(outside)})
            record("monitor 模式放行并告警", True)
        except HarnessError as exc:
            record("monitor 模式放行并告警", False, str(exc))

        enforce = Harness(mode="enforce", policy=policy, record=False)
        try:
            enforce.check_tool_call("excel_create_file", {"file_path": str(outside)})
            record("enforce 模式拦截", False, "未被拦截")
        except HarnessError:
            record("enforce 模式拦截", True)

    return results
