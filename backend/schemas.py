"""Pydantic 请求与响应模型。"""

from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)
from typing import Annotated


class ChatMessage(BaseModel):
    """单条对话消息。"""

    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatRequest(BaseModel):
    """POST /api/chat 请求体。"""

    model_config = ConfigDict(extra="forbid")

    session_id: Optional[str] = None
    project_id: Optional[str] = None
    message: str = Field(min_length=1, description="用户发送的消息")


class BootstrapRequest(BaseModel):
    """一次性本地认证凭据；不会保存于浏览器存储。"""

    secret: str = Field(min_length=1)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RefId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
WorkspaceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
WorkspacePath = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
WorkspaceKind = Literal["directory", "git"]
WorkspaceMode = Literal["read_only", "read_write"]


class DelegationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_refs: List[RefId] = Field(default_factory=list, max_length=16)
    artifact_refs: List[RefId] = Field(default_factory=list, max_length=16)


class TaskCreateRequest(BaseModel):
    """新控制面只接受 agent_id，不接受旧 session_id。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: NonEmptyText
    goal: NonEmptyText
    acceptance: List[str] = Field(default_factory=list)
    context: DelegationContext | None = None
    workspace_id: RefId | None = None
    workspace_mode: WorkspaceMode | None = None


class RunCreateRequest(BaseModel):
    """Run 启动不开放客户端选项。"""

    model_config = ConfigDict(extra="forbid")


class RunControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "cancel"]


class TaskCleanupRequest(BaseModel):
    """Cleanup 没有 force；繁忙或脏 checkout 必须失败关闭。"""

    model_config = ConfigDict(extra="forbid")


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny", "occurred", "not_occurred", "explicit_retry", "cancel"]
    note: Optional[str] = Field(default=None, max_length=2000)


class WorkspaceWriteDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class TaskResponse(BaseModel):
    task_id: str
    agent_id: str
    goal: str
    acceptance: List[str]
    context: Dict[str, Any]
    status: str
    latest_run_id: Optional[str]
    workspace_id: Optional[str] = None
    workspace_mode: Optional[WorkspaceMode] = None
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


class AgentFilesystemCapabilityRequest(BaseModel):
    """窄更新：服务端只允许改变 Agent 的两个 filesystem grant。"""

    model_config = ConfigDict(extra="forbid")

    filesystem_read: StrictBool
    filesystem_write: StrictBool


class AgentMCPWriteCapabilityRequest(BaseModel):
    """Narrow update for the side-effecting MCP grant."""

    model_config = ConfigDict(extra="forbid")

    mcp_write: StrictBool


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


class DelegationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int | None = Field(default=None, ge=1, le=20)
    max_tool_calls: int | None = Field(default=None, ge=1, le=32)


class HostDelegateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_agent_id: NonEmptyText
    goal: NonEmptyText
    acceptance: List[NonEmptyText] = Field(default_factory=list, max_length=32)
    context: DelegationContext | None = None
    requested_grant: AgentCapabilitiesRequest | None = None
    limits: DelegationLimits | None = None


class HumanReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_to_message_id: int = Field(ge=1)
    content: NonEmptyText = Field(max_length=4000)


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkspaceName
    root_path: WorkspacePath
    kind: WorkspaceKind
    max_mode: WorkspaceMode


class WorkspaceUpdateRequest(BaseModel):
    """root_path/kind 不可变；归档不可撤销。"""

    model_config = ConfigDict(extra="forbid")

    name: WorkspaceName | None = None
    max_mode: WorkspaceMode | None = None
    archived: StrictBool | None = None


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

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = "新会话"
    project_id: Optional[str] = None


class SessionRenameRequest(BaseModel):
    """重命名会话请求体。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)


class ProjectCreateRequest(BaseModel):
    """创建项目请求体。"""

    name: str = Field(min_length=1)


class ProjectRenameRequest(BaseModel):
    """重命名项目请求体。"""

    name: str = Field(min_length=1)


class RAGEnabledRequest(BaseModel):
    """Project RAG switches are host-only and never accept truthy coercion."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class KnowledgeSearchRequest(BaseModel):
    """Bounded host preview query; the server still owns MATCH generation."""

    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    top_k: StrictInt = Field(default=6, ge=1, le=10)


class KnowledgeDocumentResponse(BaseModel):
    document_id: str
    title: str
    size: int = Field(ge=0)
    mime_type: Optional[str] = None
    status: Literal["pending", "indexing", "ready", "failed", "deleting", "deleted"]
    error_code: Optional[str] = None
    chunk_count: int = Field(default=0, ge=0)
    indexed_at: Optional[str] = None
    created_at: str
    updated_at: str


class KnowledgeSourceResponse(BaseModel):
    citation_id: str
    chunk_id: str
    title: str
    locator: Dict[str, Any] = Field(default_factory=dict)
    available: StrictBool
    unavailable_reason: Optional[str] = None


class KnowledgeSourcesResponse(BaseModel):
    run_id: str
    project_id: Optional[str] = None
    sources: List[KnowledgeSourceResponse] = Field(default_factory=list)


class SessionProjectRequest(BaseModel):
    """把会话归入项目的请求体。"""

    model_config = ConfigDict(extra="forbid")

    project_id: Optional[str] = None


class MCPServerCreateRequest(BaseModel):
    """创建 MCP Server 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    transport: Literal["stdio", "streamable_http", "sse"] = "stdio"
    command: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)] | None = None
    args: List[Annotated[str, StringConstraints(max_length=4096)]] = Field(
        default_factory=list, max_length=64,
    )
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def validate_transport_fields(self):
        if self.transport == "stdio":
            if not self.command or self.url is not None:
                raise ValueError("stdio 仅接受 command/args")
            return self
        if self.command is not None or self.args or not self.url:
            raise ValueError("HTTP/SSE 仅接受 url")
        if (any(char.isspace() for char in self.url) or "\\" in self.url or
                "?" in self.url or "#" in self.url):
            raise ValueError("MCP URL 无效")
        try:
            parsed = urlsplit(self.url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                    parsed.username is not None or parsed.password is not None or
                    parsed.query or parsed.fragment):
                raise ValueError("MCP URL 仅接受无凭据、query 和 fragment 的 http(s) endpoint")
            parsed.port
        except ValueError as exc:
            raise ValueError("MCP URL 无效") from exc
        return self


class MCPServerApprovalRequest(BaseModel):
    """客户端只选择工具；所有审批摘要均由服务端生成。"""

    model_config = ConfigDict(extra="forbid")

    tool_names: List[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=64,
    )

    @model_validator(mode="after")
    def reject_duplicate_tools(self):
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("tool_names 不能重复")
        return self


class MCPServerEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


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
