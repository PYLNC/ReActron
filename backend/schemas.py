"""Pydantic 请求与响应模型。"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated


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


class BootstrapRequest(BaseModel):
    """一次性本地认证凭据；不会保存于浏览器存储。"""

    secret: str = Field(min_length=1)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TaskCreateRequest(BaseModel):
    """新控制面只接受 agent_id，不接受旧 session_id。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: NonEmptyText
    goal: NonEmptyText
    acceptance: List[str] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)


class RunCreateRequest(BaseModel):
    """Run 启动不开放客户端选项。"""

    model_config = ConfigDict(extra="forbid")


class RunControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "cancel"]


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny", "occurred", "not_occurred", "explicit_retry", "cancel"]
    note: Optional[str] = Field(default=None, max_length=2000)


class TaskResponse(BaseModel):
    task_id: str
    agent_id: str
    goal: str
    acceptance: List[str]
    context: Dict[str, Any]
    status: str
    latest_run_id: Optional[str]
    created_at: str
    updated_at: str
    finished_at: Optional[str]


class TaskListResponse(BaseModel):
    tasks: List[TaskResponse]


class RunResponse(BaseModel):
    run_id: str
    task_id: str
    agent_id: str
    attempt: int
    status: str
    wait_reason: Optional[str]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    last_event_id: int
    allowed_actions: List[str]


class AgentSummary(BaseModel):
    agent_id: str
    title: str
    created_at: str
    updated_at: str
    project_id: Optional[str]


class AgentListResponse(BaseModel):
    agents: List[AgentSummary]


AgentTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ControlGrant = Literal[
    "read_only",
    "control.delegate_task",
    "control.send_agent_message",
    "control.get_task_status",
    "control.wait_for_tasks",
    "control.cancel_task",
    "control.request_human_approval",
]


class AgentCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: List[ControlGrant] = Field(default_factory=list, max_length=32)


class SupervisorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: AgentTitle = "Supervisor"
    system_prompt: str = Field(default="", max_length=4000)
    capabilities: AgentCapabilitiesRequest | None = None


class SupervisorSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: NonEmptyText


class WorkerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_agent_id: NonEmptyText
    title: AgentTitle
    system_prompt: str = Field(default="", max_length=4000)
    capabilities: AgentCapabilitiesRequest | None = None
    can_delegate: bool = False


class AgentRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: AgentTitle


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
    enabled: bool = False


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
