# 更新日志

## 更新约定

每次代码、接口、页面或文档更新，都必须在本文件顶部新增一条记录，说明：

- 更新日期与版本号。
- 新增内容。
- 变更内容，特别是破坏性接口变更。
- 更新了哪些文档。

## 2026-08-14 v1.0.1

### 安全

- 新增 `scripts/check_secrets.py`，上传 GitHub 前扫描 API Key、私钥、数据库、密钥文件和日志。
- `.env.example` 已重置为占位符，不再包含真实 API Key。

### 文档

- 新增 GitHub 上传安全说明。

## 2026-08-14 v1.0.0

### 新增

- 正式接入 MCP（Model Context Protocol）。
- 新增 MCP Manager，支持 stdio 和 SSE 传输。
- MCP 工具会自动发现并注册到 skills 注册中心。
- 新增 MCP Server 管理接口和前端设置面板入口。
- 支持通过 `MCP_SERVERS` 环境变量配置外部服务器。

### 文档

- 更新 `README.md`、`DEVELOPMENT.md`、`MCP_PLAN.md`。

## 2026-08-14 v0.9.0

### 变更

- 项目名称变更为 ReActron。

### 新增

- 新增文件上传接口 `POST /api/upload`。
- 支持解析 Excel、PPT、CSV、文本和 JSON 文件。
- 新增 `file` skill 和 `read_uploaded_file` 工具。
- 会话现在必须归属于项目，未选择项目时禁止创建会话。
- 旧会话会自动迁移到“默认项目”。
- 新增 `MCP_PLAN.md`，描述 MCP 接入方案。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`。

## 2026-08-14 v0.8.0

### 新增

- 新增项目文件夹，支持创建、重命名、删除项目。
- 支持把会话归入项目或移回未分组。
- 新会话首条消息后自动生成标题。
- 空会话全局最多保留一个，已有空会话时不再重复创建。
- 启动时自动清理多余的空会话，只保留最新一个。

### 接口

- 新增 `GET/POST /api/projects`。
- 新增 `PATCH/DELETE /api/projects/{project_id}`。
- 新增 `PUT /api/sessions/{session_id}/project`。
- `POST /api/sessions` 支持 `project_id` 参数。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`。

## 2026-08-14 v0.7.0

### 前端

- 全面重做视觉风格，参考苹果系设计语言。
- 缩小基础字号，正文改为更克制的排版。
- 使用浅灰背景、白色面板、细边框、系统字体和轻量阴影。
- 顶栏改为半透明毛玻璃效果。
- 聊天输入区改为浮动圆角容器。
- 会话、技能、工具和参数列表改为更紧凑的现代样式。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`。

## 2026-08-14 v0.6.0

### 新增

- 新增安全与自检 Harness。
- 新增路径白名单、文件类型和大小校验。
- 新增 `monitor` / `enforce` 两种模式。
- 新增工具调用审计表 `audit_logs`。
- 新增 `GET /api/harness`、`POST /api/harness/self-test`、`GET /api/harness/audit`。
- 新增 `tests/test_harness.py`，使用标准库 unittest。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`。

## 2026-08-14 v0.5.2

### 修复

- 修复旧会话发消息后没有回复且错误提示被清空的问题。
- 请求失败时不再自动重开会话，错误提示会保留在聊天区。
- 请求成功后只刷新会话列表，不再打断当前聊天。

### 文档

- 更新 `CHANGELOG.md`。

## 2026-08-14 v0.5.1

### 前端

- 左侧改为纯会话抽屉，可整体展开/收起。
- 新增右侧信息抽屉，展示技能、可用工具和运行参数。
- 右侧各分区支持点击标题展开/收起，类似设置面板。
- 抽屉状态使用 localStorage 保存，刷新后保持。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`。

## 2026-08-14 v0.5.0

### 新增

- 新增 Skill 技能机制，Agent 启动时自动加载所有已注册技能。
- 新增 `excel` 技能：读取工作表、列出工作表、写入单元格、创建 Excel 文件。
- 新增 `ppt` 技能：列出幻灯片、提取文本、创建 PPT 文件。
- 新增 `GET /api/skills` 接口，前端侧边栏展示已注册技能。
- Agent 自动根据技能生成系统提示，帮助模型选择工具。

### 依赖

- 新增 `openpyxl`、`python-pptx`。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`，补充技能扩展指南。

## 2026-08-14 v0.4.1

### 优化

- 同一轮多个工具调用改为并行执行，减少多工具任务的总体耗时。
- 复用 `AsyncOpenAI` 客户端，避免每次请求重复创建连接。
- SQLite 开启 WAL 模式、`busy_timeout`，并新增消息读取索引。
- `done` 事件新增 `total_ms`，前端会显示本次 Agent 执行总耗时。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md`。

## 2026-08-14 v0.4.0

### 新增

- 增加 `LOG_LEVEL` 环境变量，支持 `DEBUG`、`INFO`、`WARNING`。
- 增加 `LOG_STREAM` 环境变量，默认开启模型 stream chunk 日志。
- 补全输入输出日志：HTTP 请求、聊天输入、模型输入消息、stream chunk、SSE 事件、工具输入输出、消息持久化和 SQLite 读写。
- `run.bat` 默认设置 `PYTHONIOENCODING=utf-8`，避免中文日志乱码。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md` 的日志配置说明。

## 2026-08-14 v0.3.2

### 修复

- 修复 `run.bat` 双击无法启动的问题。
- `run.bat` 改为 CRLF 换行，兼容 Windows `cmd` 解析。
- 启动前检测 8001 端口是否已被占用，服务已运行时直接提示访问地址。
- 启动失败时保留窗口并显示错误信息，方便排查。

### 文档

- 更新 `CHANGELOG.md`。

## 2026-08-13 v0.3.1

### 修复

- 修复未在前端配置 API Key 时仍可直接对话的问题。
- 后端不再把 `.env.example` 当作运行配置读取。
- API Key 现在只能通过前端“设置”（加密写入 SQLite）或正式 `.env` / 系统环境变量配置。

### 文档

- 更新 `README.md` 与 `DEVELOPMENT.md` 的配置说明。

## 2026-08-13 v0.3.0

### 新增

- 前端配置面板，可以直接填写 DeepSeek API Key、Base URL、模型、最大轮数和工具超时。
- API Key 使用 Fernet 对称加密后写入 SQLite，数据库不保存明文。
- 多会话管理：可以新建、切换、重命名、删除会话。
- SQLite 持久化：会话和消息保存到 `data/countboter.db`，下次打开仍可恢复。
- 新增 `CHANGELOG.md`，后续每次更新必须记录。

### 接口变更

- `POST /api/chat` 请求体改为 `{"session_id": "...", "message": "..."}`。
- 新增 `GET/PUT /api/settings`。
- 新增 `GET/POST /api/sessions`、`GET/PATCH/DELETE /api/sessions/{session_id}`。

### 文档

- 更新 `DEVELOPMENT.md`，补充数据持久化、加密、多会话和接口说明。
