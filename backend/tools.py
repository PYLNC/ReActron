"""示例工具集合：新工具在这里实现并注册即可。"""

import logging
from datetime import datetime
from pathlib import Path


logger = logging.getLogger("agent.tools")


def get_current_time():
    """获取当前日期和时间。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("调用 get_current_time，当前时间：%s", now)
    return {
        "datetime": now,
        "note": "使用服务器本地时区",
    }


def list_dir(path: str = "."):
    """列出指定目录下的文件和子目录。"""
    # 展开用户目录并转成绝对路径，方便模型传相对路径或 ~ 开头的路径
    target = Path(path).expanduser().resolve()

    if not target.exists():
        raise FileNotFoundError(f"目录不存在: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录: {target}")

    entries = []
    # 目录排在前面，名称按字母顺序排列，结果更稳定也更好看
    for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        entries.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
        })

    logger.info("调用 list_dir，entries=%d", len(entries))
    return {"path": str(target), "count": len(entries), "entries": entries}


# 提供给模型的工具 JSON Schema，OpenAI 兼容接口都使用这个格式
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出指定目录下的文件和子目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要查看的目录路径，默认为当前目录。"},
                },
                "required": [],
            },
        },
    },
]


# 工具名称到实际函数的映射，新增工具时在这里注册即可
TOOL_FUNCTIONS = {
    "get_current_time": get_current_time,
    "list_dir": list_dir,
}
