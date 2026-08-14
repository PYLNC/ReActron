"""Pydantic 请求与响应模型。"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """单条对话消息。"""

    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatRequest(BaseModel):
    """POST /api/chat 请求体。"""

    session_id: Optional[str] = None
    project_id: Optional[str] = None
    message: str = Field(min_length=1, description="用户发送的消息")


class SettingsRequest(BaseModel):
    """PUT /api/settings 请求体。"""

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    max_rounds: Optional[int] = Field(default=None, ge=1)
    tool_timeout: Optional[float] = Field(default=None, gt=0)


class SettingsResponse(BaseModel):
    """设置信息响应，不包含 API Key。"""

    base_url: str
    model: str
    max_rounds: int
    tool_timeout: float
    api_key_configured: bool


class SessionSummary(BaseModel):
    """会话摘要。"""

    id: str
    title: str
    created_at: str
    updated_at: str
    project_id: Optional[str] = None


class SessionListResponse(BaseModel):
    """会话列表响应。"""

    sessions: List[SessionSummary]


class SessionCreateRequest(BaseModel):
    """创建会话请求体。"""

    title: Optional[str] = "新会话"
    project_id: Optional[str] = None


class SessionRenameRequest(BaseModel):
    """重命名会话请求体。"""

    title: str = Field(min_length=1)


class ProjectCreateRequest(BaseModel):
    """创建项目请求体。"""

    name: str = Field(min_length=1)


class ProjectRenameRequest(BaseModel):
    """重命名项目请求体。"""

    name: str = Field(min_length=1)


class SessionProjectRequest(BaseModel):
    """把会话归入项目的请求体。"""

    project_id: Optional[str] = None


class MCPServerCreateRequest(BaseModel):
    """创建 MCP Server 的请求体。"""

    name: str = Field(min_length=1)
    transport: str = Field(default="stdio", pattern="^(stdio|sse)$")
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    enabled: bool = True


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str
    service: str
    time: str


class ConfigResponse(BaseModel):
    """非敏感运行配置，不会包含 API Key。"""

    base_url: str
    model: str
    max_rounds: int
    tool_timeout: float
    api_key_configured: bool


class ToolInfo(BaseModel):
    """单个工具定义。"""

    type: str
    function: Dict[str, Any]


class ToolsResponse(BaseModel):
    """工具列表响应。"""

    count: int
    tools: List[ToolInfo]
