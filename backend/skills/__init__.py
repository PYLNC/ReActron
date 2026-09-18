"""Skill 注册中心：集中管理所有技能、工具定义和系统提示。"""

import logging

from backend.skills.file_skill import FILE_SKILL
from backend.skills.excel_skill import EXCEL_SKILL
from backend.skills.ppt_skill import PPT_SKILL
from backend.skills.control_skill import CONTROL_TOOLS
from backend.tools import TOOL_DEFINITIONS as CORE_TOOL_DEFINITIONS
from backend.tools import TOOL_FUNCTIONS as CORE_TOOL_FUNCTIONS


logger = logging.getLogger("agent.skills")

# 所有已注册技能，按顺序加载
SKILLS = [
    FILE_SKILL,
    EXCEL_SKILL,
    PPT_SKILL,
]

# 合并基础工具与所有技能提供的工具
TOOL_DEFINITIONS = list(CORE_TOOL_DEFINITIONS)
TOOL_FUNCTIONS = dict(CORE_TOOL_FUNCTIONS)
for skill in SKILLS:
    TOOL_DEFINITIONS.extend(skill["tool_definitions"])
    TOOL_FUNCTIONS.update(skill["tool_functions"])
TOOL_DEFINITIONS.extend(
    {"type": "function", "function": {"name": name, "description": description,
                                    "parameters": schema}}
    for name, (description, schema) in CONTROL_TOOLS.items()
)

MCP_TOOL_DEFINITIONS = []
MCP_TOOL_FUNCTIONS = {}


def register_mcp_tools(definitions: list, functions: dict):
    """动态注册 MCP 发现到的工具，重复刷新时会先移除旧工具。"""
    global MCP_TOOL_DEFINITIONS, MCP_TOOL_FUNCTIONS

    for definition in MCP_TOOL_DEFINITIONS:
        if definition in TOOL_DEFINITIONS:
            TOOL_DEFINITIONS.remove(definition)
    for name in MCP_TOOL_FUNCTIONS:
        TOOL_FUNCTIONS.pop(name, None)

    MCP_TOOL_DEFINITIONS = definitions
    MCP_TOOL_FUNCTIONS = functions
    TOOL_DEFINITIONS.extend(definitions)
    TOOL_FUNCTIONS.update(functions)
    logger.info("MCP 工具注册完成：%d 个", len(definitions))


def _reject_legacy_execution(*_args, **_kwargs):
    raise RuntimeError("tool_gateway_required")


# 旧 Agent 导入兼容；此入口不能执行任何 handler，A 将改为网关调用。
execute_tool = _reject_legacy_execution


def get_skills_info() -> list:
    """返回技能摘要，供前端和接口展示。"""
    info = [
        {
            "name": skill["name"],
            "description": skill["description"],
            "tools": [
                definition["function"]["name"]
                for definition in skill["tool_definitions"]
            ],
        }
        for skill in SKILLS
    ]
    if MCP_TOOL_DEFINITIONS:
        info.append({
            "name": "mcp",
            "description": "Model Context Protocol 外部服务器工具",
            "tools": [
                definition["function"]["name"]
                for definition in MCP_TOOL_DEFINITIONS
            ],
        })
    return info


def build_system_prompt() -> str:
    """根据已注册技能生成 Agent 系统提示。"""
    lines = [
        "你是一个具备工具调用能力的 Agent，可以选择合适的技能和工具完成任务。",
        "",
        "当前可用技能：",
    ]
    for skill in SKILLS:
        lines.append(f"- {skill['name']}: {skill['description']}")
    lines.extend([
        "",
        "使用规则：",
        "1. 优先理解用户任务，再决定是否需要调用工具。",
        "2. 文件路径以用户提供或工具返回的路径为准。",
        "3. 工具执行失败时，把错误信息回填给用户并尝试其他方式。",
    ])
    return "\n".join(lines)
