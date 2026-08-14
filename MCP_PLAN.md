# ReActron MCP 接入设计

## 当前状态

ReActron 已接入 MCP 基础能力：

- 支持 stdio 和 SSE 两种传输方式。
- 支持从 `MCP_SERVERS` 环境变量或数据库读取服务器配置。
- 支持自动发现 MCP 工具并注册到 skills。
- 支持通过管理接口添加、删除、测试和刷新。

## 目标

让 ReActron 可以连接一个或多个 MCP Server，自动发现 MCP 工具，并像本地工具一样交给 Agent 调用。

## 接入架构

```text
ReActron Agent
  -> skills 注册中心
  -> MCP Manager
  -> MCP Client (stdio / SSE / HTTP)
  -> 外部 MCP Server
```

## 已实现

### 1. MCP Manager

新增 `backend/mcp/manager.py`，负责：

- 从环境变量 `MCP_SERVERS` 读取服务器配置。
- 支持 stdio、SSE 两种传输方式。
- 连接后调用 `tools/list` 发现工具。
- 把 MCP 工具转换为 Agent 的 `TOOL_DEFINITIONS`。
- 断开连接、心跳检测、异常恢复。

### 2. MCP Bridge（已并入 Manager）

`backend/mcp/manager.py` 内负责：

- 把 Agent 的工具调用转换为 MCP `tools/call`。
- 设置超时。
- 工具调用会统一经过 skills `execute_tool`，因此自动经过 Harness 审计和路径检查。

### 3. Skills 注册中心接入

把 MCP 动态工具注册为一个特殊 skill：

```python
MCP_SKILL = {
    "name": "mcp",
    "description": "通过 Model Context Protocol 调用外部服务器工具",
    "tool_definitions": mcp_tools,
    "tool_functions": mcp_functions,
}
```

### 4. 配置示例

```env
MCP_SERVERS=[
  {
    "name": "filesystem",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/tmp"]
  },
  {
    "name": "database",
    "transport": "sse",
    "url": "http://127.0.0.1:3001/mcp"
  }
]
```

### 5. 管理接口

- `GET /api/mcp/servers`：查看已连接 MCP Server。
- `POST /api/mcp/servers`：新增 MCP Server。
- `DELETE /api/mcp/servers/{id}`：移除 MCP Server。
- `POST /api/mcp/servers/{id}/test`：测试连接并列出工具。

### 6. 前端

在设置面板增加 MCP 分区：

- 显示已连接服务器。
- 添加服务器名称、传输方式、命令或 URL。
- 显示发现到的工具数量。
- 一键测试连接。

## 安全要求

- MCP Server 配置需要校验，不允许任意命令直接执行。
- 外部工具必须经过 Harness 审计和路径白名单。
- 连接需要超时和最大工具数量限制。
- 远程 MCP 需要鉴权 Token。

## 实施顺序

1. 安装 `mcp` Python SDK。
2. 实现 MCP Manager，支持 stdio。
3. 实现 Bridge，接入 skills 注册中心。
4. 增加管理接口。
5. 前端设置面板接入。
6. 增加 SSE 远程服务器支持。
