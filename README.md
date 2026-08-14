# ReActron

一个基于 FastAPI + DeepSeek 的 Agent 框架，包含工具调用循环、SSE 流式聊天、多会话、项目文件夹和文件上传解析。

## 功能

- Python FastAPI + Uvicorn 后端
- OpenAI SDK 兼容接口对接 DeepSeek
- 模型可以调用工具，工具结果回填后继续下一轮
- 默认最多 5 轮 Agent 循环
- 示例工具：`get_current_time`、`list_dir`
- 原生 HTML/JS/CSS 聊天界面，SSE 流式显示回复与工具调用过程
- 后端使用 Python `logging` 输出关键运行日志
- 提供 `/api/health`、`/api/tools`、`/api/config` 辅助接口
- 支持通过环境变量调整最大轮数和工具执行超时
- 支持前端直接配置 API Key，保存时使用 Fernet 加密
- 支持多会话管理，会话与消息持久化到 SQLite
- 同一轮多个工具调用并行执行，缩短整体响应时间
- 复用 AsyncOpenAI 客户端，避免每次请求重复建立连接
- SQLite 开启 WAL 模式并建立消息索引，减少数据库读写延迟
- 内置 Skill 机制，当前提供 Excel 表格处理和 PPT 演示文稿两个技能
- 左侧会话与右侧信息面板都可展开/收起，信息分区支持点击折叠
- 前端采用苹果系极简视觉：小字号、浅灰背景、细边框、毛玻璃顶栏
- 空会话全局最多保留一个，避免脚本批量创建空会话
- 新会话会根据第一条提问自动生成标题
- 支持项目文件夹，会话可以归并到不同项目
- 支持上传并解析 Excel、PPT、CSV、文本、JSON 文件
- 会话必须归属于项目，创建项目后才能创建会话
- 已支持 MCP：连接外部 MCP Server，自动发现并注册工具
- 内置安全与自检 Harness：路径校验、工具调用审计、monitor/enforce 模式

## 目录结构

```text
countboter/
├── backend/
│   ├── __init__.py
│   ├── app.py        # FastAPI 入口、静态托管、访问日志、全局异常
│   ├── api.py        # REST 与 SSE 接口路由
│   ├── agent.py      # Agent 循环
│   ├── config.py     # 环境变量配置
│   ├── db.py         # SQLite 数据访问
│   ├── file_parser.py # 上传文件解析
│   ├── harness.py    # 安全策略、工具审计和自检
│   ├── mcp/          # MCP Manager 与工具注册
│   ├── schemas.py    # Pydantic 请求/响应模型
│   ├── security.py   # API Key 加密
│   ├── services.py   # 业务编排
│   ├── skills/       # 技能注册中心与 Excel/PPT 技能
│   └── tools.py      # 示例工具与注册表
├── tests/
│   └── test_harness.py
├── MCP_PLAN.md
├── static/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── requirements.txt
├── .env.example
├── data/            # 自动创建的 SQLite 数据库目录
├── run.bat
├── CHANGELOG.md
├── DEVELOPMENT.md
└── README.md
```

## 环境变量

把 `.env.example` 复制为 `.env` 并填写自己的密钥，或者直接在页面右上角“设置”里填写。
后端不会把 `.env.example` 当作运行配置读取，因此只把密钥放在示例文件里不会生效。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 无 |
| `DEEPSEEK_BASE_URL` | OpenAI 兼容接口地址 | `https://api.deepseek.com/v1` |
| `DEEPSEEK_MODEL` | 使用的模型 | `deepseek-chat` |
| `DEEPSEEK_MAX_ROUNDS` | Agent 最大轮数 | `5` |
| `DEEPSEEK_TOOL_TIMEOUT` | 单个工具执行超时，单位秒 | `10` |
| `APP_SECRET_KEY` | Fernet 加密密钥，可选 | 自动生成 |
| `LOG_LEVEL` | 日志级别：DEBUG/INFO/WARNING | `INFO` |
| `LOG_STREAM` | 是否输出模型 stream chunk，1 开启 / 0 关闭 | `1` |
| `HARNESS_MODE` | Harness 模式：`monitor` 或 `enforce` | `monitor` |
| `HARNESS_ALLOWED_ROOTS` | 额外允许访问的目录，分号分隔 | 项目目录和 data/ |
| `HARNESS_MAX_FILE_SIZE` | 允许读取的最大文件字节数 | `20971520` |

也可以不配置 `.env`，直接在页面右上角“设置”里填写 API Key；保存时后端会加密后写入 SQLite。

## 安装与运行

本机直接使用 Conda 环境 `countbot`，依赖已经安装好：

```bash
cd C:\Users\LNC\Desktop\countboter
E:\anaconda\envs\countbot\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

也可以直接运行 `run.bat`，它会自动调用 `E:\anaconda\envs\countbot\python.exe`。

如果需要在新机器上准备环境：

```bash
conda create -n countbot python=3.11
conda activate countbot
pip install -r requirements.txt
```

浏览器打开 <http://127.0.0.1:8000>，输入消息即可看到流式回复。
如果 8000 端口已被占用，可以换成其他端口，例如：

```bash
E:\anaconda\envs\countbot\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8001
```

可以试试：

- 现在几点？
- 列出当前目录的文件
- 列出 C:\Users 下的内容

## API 接口

- `GET /api/health`：健康检查。
- `GET /api/tools`：返回当前注册给模型的工具列表。
- `GET /api/skills`：返回当前已注册的技能列表。
- `POST /api/upload`：上传并解析文件。
- `GET/POST /api/mcp/servers`：MCP Server 列表与新增。
- `DELETE /api/mcp/servers/{id}`：删除 MCP Server。
- `POST /api/mcp/refresh`：重新发现并注册 MCP 工具。
- `POST /api/mcp/servers/{id}/test`：测试 MCP 连接。
- `GET /api/harness`：返回 Harness 状态。
- `POST /api/harness/self-test`：运行 Harness 自检。
- `GET /api/harness/audit`：返回最近的工具调用审计记录。
- `GET /api/config`：返回模型、最大轮数、工具超时等非敏感配置。
- `GET /api/settings`：读取前端设置。
- `PUT /api/settings`：保存并加密前端设置。
- `GET /api/sessions`：会话列表。
- `POST /api/sessions`：新建会话。
- 创建会话必须携带 `project_id`。
- `GET/PATCH/DELETE /api/sessions/{session_id}`：读取、重命名、删除会话。
- `GET/POST /api/projects`：项目文件夹列表与创建。
- `PATCH/DELETE /api/projects/{project_id}`：重命名、删除项目。
- `PUT /api/sessions/{session_id}/project`：把会话归入项目。
- `POST /api/chat`：接收 `{"session_id": "...", "message": "..."}`，SSE 流式返回 Agent 执行过程。

## 日志

- 后端日志输出到控制台：HTTP 请求、Agent 轮次、工具调用、工具结果、异常等。
- 前端在浏览器控制台以 `[agent-chat]` 前缀输出 SSE 事件，方便调试。
- 每次功能更新都会记录到 `CHANGELOG.md`。
- 默认 `LOG_STREAM=1`，会输出模型每个 stream chunk、SSE 事件、工具输入输出和数据库读写。
- 如果日志过大，可在 `.env` 或启动脚本中设置 `LOG_STREAM=0`，只保留摘要日志。

## 安全 Harness

- `monitor` 模式：发现越权路径时记录警告和审计，但放行工具，方便调试。
- `enforce` 模式：发现越权路径时直接拦截工具调用。
- 默认只允许访问项目目录和 `data/`，可通过 `HARNESS_ALLOWED_ROOTS` 增加目录。
- 每次工具调用都会写入 `audit_logs` 表，可通过 `/api/harness/audit` 查看。
- 运行自检：

```bash
cd C:\Users\LNC\Desktop\countboter
E:\anaconda\envs\countbot\python.exe -m unittest discover -s tests
```

## GitHub 上传安全

- `.env`、`data/`、`.secret_key`、日志文件已在 `.gitignore` 中忽略。
- `.env.example` 只保留占位符，不写真实 API Key。
- 上传前运行敏感信息扫描：

```bash
E:\anaconda\envs\countbot\python.exe scripts/check_secrets.py
```

- 如果 API Key 曾经泄露或出现在对话中，请立即到 DeepSeek 控制台重新生成。

## 如何扩展工具

1. 在 `backend/tools.py` 中新增一个普通函数。
2. 在 `TOOL_DEFINITIONS` 中添加对应的 JSON Schema。
3. 在 `TOOL_FUNCTIONS` 中注册函数名。

Agent 循环会自动把模型的工具调用转发给 `execute_tool`，并把结果以 `role=tool` 回填到下一轮。

## 如何扩展技能

1. 在 `backend/skills/` 下新建技能模块，例如 `markdown_skill.py`。
2. 实现工具函数，并在模块中定义 `tool_definitions` 和 `tool_functions`。
3. 在 `backend/skills/__init__.py` 的 `SKILLS` 列表中注册。

Agent 启动时会自动合并所有技能的工具，并把技能说明加入系统提示。

## 技术说明

- `/api/chat` 接收 `{"session_id": "...", "message": "..."}`，返回 `text/event-stream`。
- 事件类型：`round`、`text`、`tool_call`、`tool_result`、`done`、`error`。
- `done` 事件会附带 `total_ms`，方便查看一次 Agent 执行的总耗时。
- 每一轮最多调用一次模型；如果模型返回 `tool_calls`，会执行工具后继续下一轮，最多 5 轮。
- 会话和消息保存在 `data/countboter.db`，下次打开仍可恢复。
- 空会话会被自动去重，已有空会话时不会重复创建。
- 会话首条消息会自动生成标题，项目用于对会话分组。
