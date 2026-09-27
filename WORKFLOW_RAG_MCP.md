# ReActron 的 RAG 与 MCP 工作流

本文面向使用者和维护者，说明当前代码中 Task、Run、RAG 与 MCP 如何串成一条可恢复、可审核的执行链。它是工作流导览，不替代 [RAG_DESIGN.md](RAG_DESIGN.md) 和 [MCP_PLAN.md](MCP_PLAN.md) 中的详细设计与验收清单。

## 一页式 RAG 前后变化

| 环节 | 加入 RAG 前（MCP 主链已存在） | 当前实现（RAG + MCP） |
| --- | --- | --- |
| 用户请求 | 用户消息创建 Task，再创建持久 Run | 主链与工具快照不变；Run 创建时额外冻结 RAG 来源身份 |
| 模型上下文 | Task 元数据、历史、当前输入、本 Run 消息 | 在当前输入前加入经核验的 Project 知识片段 |
| 外部知识 | 只能来自聊天历史、普通上传或内置工具 | Project 级知识库自动检索；Run 内用 `[Kx]` 引用 |
| 外部能力 | 内置、Workspace 和审批后的 MCP 工具 | 工具链不变；RAG 是被动上下文，不注册成工具 |
| 可重复性 | Run 冻结 Provider、工具和历史截止点 | 再冻结 RAG 来源身份；MCP 工具规格仍逐次与 live registry 对照 |
| 权限 | ToolSpec、grant、Workspace 和 Gateway 决定 | RAG 不增加权限；MCP 仍须满足快照、grant、Schema 与审批 |
| 写操作 | 受控的本地写工具需要审批 | `mcp.write` 还需要绑定到单次调用的 Host 审批 |
| UI | SSE 展示模型、工具与 Run 状态 | 增加知识库管理、检索预览、Run 来源与引用详情 |
| 降级 | Provider 或工具失败使对应 Run 失败 | RAG/单个 MCP Server 可单独不可用，主聊天与其他能力继续运行 |

## 基础执行链：Task 到 SSE

加入 RAG 前，浏览器主入口已经使用持久 Task/Run，而不是把一次 HTTP 连接当作执行生命周期。当前实现仍保留这条主链。

```text
用户输入
  |
  +--> Browser: sendMessage()
         |
         +--> POST /api/tasks
         |      |
         |      +--> Database.create_task()
         |             |
         |             +--> tasks(status=open)
         |             +--> messages(role=user, task_id, run_id=NULL)
         |
         +--> POST /api/tasks/{task_id}/runs + Idempotency-Key
                |
                +--> Database.create_run() / _insert_run_tx()
                       |
                       +--> freeze Provider config
                       +--> freeze Tool specs + effective grants
                       +--> freeze context_max_sequence
                       +--> runs(status=queued) + run.queued event
                              |
                              +--> TaskRunner.wake()
                                     |
                                     +--> claim_next_run()
                                     +--> build_context()
                                     |      |
                                     |      +--> Task/system metadata
                                     |      +--> history through frozen cutoff
                                     |      +--> current Task input exactly once
                                     |      +--> this Run's assistant/tool messages
                                     |
                                     +--> AgentStep -> chat.completions.create(stream=True)
                                            |
                                            +--> text delta -> persisted Run events -> SSE
                                            |
                                            +--> no tool_calls
                                            |      +--> save assistant message
                                            |      +--> Run completed
                                            |
                                            +--> tool_calls
                                                   +--> durable checkpoint
                                                   +--> Gateway executes tool
                                                   +--> role=tool result is saved
                                                   +--> next model round
```

`POST /api/chat` 是兼容入口：它在一个事务内创建 Task 和 Run，再订阅相同的 Runner/SSE 流程。浏览器的新流程使用分开的 Task 与 Run API。

## RAG 文档进入知识库

知识文件与普通 `/api/upload` 分开保存。前者属于一个 Project，不能通过全局上传文件读取路径绕过 Project 边界。

```text
Host 选择 Project 和文件
  |
  +--> POST /api/projects/{project_id}/knowledge
         |
         +--> require local Host session
         +--> verify Project + RAG capability + extension
         +--> stream to data/knowledge/{random_document_id}.{ext}
         +--> enforce 20 MiB/file and calculate SHA-256
         +--> enforce Project limits and content-hash dedupe
         |
         +--> knowledge_documents(status=pending)
                |
                +--> wake process-local RAGIndexer
                       |
                       +--> claim pending document
                       +--> status=indexing + renewable lease
                       +--> verify direct regular path, size and SHA-256
                       |
                       +--> spawned extraction process (hard timeout)
                       |      |
                       |      +--> txt / md / log / json / csv
                       |      +--> xlsx / pptx / docx / text-layer PDF
                       |      +--> bounded DocumentSegment[] + locators
                       |
                       +--> deterministic chunk_segments()
                       |      |
                       |      +--> normalized text
                       |      +--> bounded chunks with overlap
                       |      +--> deterministic chunk id + content hash
                       |
                       +--> complete_rag_document() transaction
                              |
                              +--> replace knowledge_chunks
                              +--> status=ready
                              +--> SQLite FTS5 trigram triggers update index
```

解析或索引失败会重试；尝试耗尽后文档进入 `failed`，Host 可显式重试。删除时先删 Chunk 并进入 `deleting`，所以它会立即退出检索；索引器随后删除物理文件并留下不含路径和正文的 `deleted` tombstone。

## RAG 进入 Run、模型与引用 UI

RAG 不是模型可调用的搜索工具。检索只发生在 Run 创建时，结果身份随 Run 冻结；正文在每轮构建上下文时重新核验。

```text
Task(goal, acceptance)
  |
  +--> create_run API builds bounded trusted query
         |
         +--> Database._insert_run_tx()                    [one transaction]
                |
                +--> derive project_id from trusted Session row
                +--> project.rag_enabled == false --------> empty RAG snapshot
                |
                +--> project.rag_enabled == true
                       |
                       +--> Project-scoped lexical search
                       +--> ready + not-deleted documents only
                       +--> rank, dedupe and per-document/context limits
                       |
                       +--> rag_snapshot_json
                              |
                              +--> project_id
                              +--> citation_id / chunk_id / content_sha256
                              +--> title / locator
                              +--> no body, path, query, score or secret
                                     |
                                     +--> Run queued
                                            |
                                            +--> build_context() before each model round
                                                   |
                                                   +--> strictly parse frozen snapshot
                                                   +--> re-read by project + chunk_id
                                                   +--> require ready/not-deleted + same hash
                                                   +--> drop unavailable/drifted bodies
                                                   +--> trim lower-ranked chunks to budget
                                                   |
                                                   +--> model messages
                                                          |
                                                          +--> untrusted-data system warning
                                                          +--> sources JSON with [Kx]
                                                          +--> current Task input
                                                                 |
                                                                 +--> model answer contains plain [Kx]
                                                                        |
                                                                        +--> message stores run_id
                                                                        +--> GET Run knowledge-sources
                                                                        +--> UI maps [Kx] per Run
                                                                        +--> click -> bounded preview
```

快照最多保留 6 个 Chunk、每文档最多 2 个，RAG 正文预算为 12,000 字符；完整模型消息加 Provider/工具快照仍受 64,000 序列化字符与 128 条消息上限约束。同一幂等 Run 返回原快照；Run 的租约恢复也不会重新检索。只有新 Run 才看到更新后的索引。

Run 来源列表只投影冻结的标题、locator 和当前可用性。引用详情会再次核验 Project、Chunk 与 hash，可用时只返回有界预览。文档删除或内容 hash 漂移后，旧回答仍能显示来源身份，但不能重新读取正文。

## MCP 从配置到一次工具结果

MCP 有两层审批：先批准 Server 的工具目录，再对每次 `mcp.write` 调用单独批准。目录批准不是写操作的通行证。

```text
POST /api/mcp/servers
  |
  +--> persist inert config
       enabled=false, no approved catalog
         |
         +--> Host: discover/test
         |      |
         |      +--> open temporary MCP session
         |      +--> initialize + list_tools
         |      +--> normalize names, schemas, annotations and effects
         |      +--> return safe catalog view
         |
         +--> Host: approve selected catalog entries
                |
                +--> fresh rediscovery
                +--> persist exact config hash + approved tool fingerprints
                +--> keep enabled=false
                       |
                       +--> Host: enable / refresh
                              |
                              +--> fresh rediscovery
                              +--> require exact approved config and selected entries
                              +--> ignore newly advertised, unapproved tools
                              +--> atomically publish immutable in-memory registry
                                     |
                                     +--> new Run creation
                                            |
                                            +--> freeze ToolSpec fields + effective grants
                                            +--> tool_snapshot_json (no handler or secret)
                                                   |
                                                   +--> Runner restore
                                                          |
                                                          +--> keep only frozen spec == live spec
                                                          +--> expose grant-allowed tools to model
                                                                 |
                                                                 +--> model selects MCP tool
                                                                        |
                                                                        +--> durable tool checkpoint
                                                                        +--> Gateway validates Schema,
                                                                        |    grant, origin and budgets
                                                                        |
                                                                        +--> read_only / never ------+
                                                                        |                              |
                                                                        +--> mcp.write / always       |
                                                                               |                       |
                                                                               +--> bind Run/call/     |
                                                                               |    server/config/     |
                                                                               |    schema/arguments/  |
                                                                               |    current boot       |
                                                                               +--> Host approve/deny  |
                                                                               +--> exact recheck and  |
                                                                               |    one-time consume   |
                                                                                                       |
                                                                        +------------------------------+
                                                                        |
                                                                        +--> durable tool.started
                                                                        +--> MCPManager.call()
                                                                               |
                                                                               +--> fresh session
                                                                               +--> live list_tools
                                                                               +--> verify approved
                                                                               |    tool fingerprint
                                                                               +--> call_tool(arguments)
                                                                               +--> close session
                                                                                      |
                                                                                      +--> bounded result
                                                                                      +--> redaction
                                                                                      +--> role=tool
                                                                                      +--> next model round
```

Registry refresh uses an immutable `MappingProxyType` and swaps the published map as a unit。Run 创建时会持久化冻结的工具描述；Runner claim/recovery 时只恢复它与当前 registry 的逐字段精确交集。调用时还会重新发现并核验已批准工具；Server 新增的未批准工具不会进入 registry，也不会让已批准工具自动获得新权限。

## RAG 与 MCP 联合 Run

两者进入模型的方向不同，互补但不互相替代。

```text
                              +-------------------------------+
Project knowledge ---------->| RAG: passive frozen context   |
                              | facts + [Kx], no capability   |
                              +---------------+---------------+
                                              |
                                              v
Task -> Run snapshots -> build_context -> MODEL
                                              |
                                              | model chooses tool_calls
                                              v
                              +---------------+---------------+
Approved live registry ----->| MCP: active external action   |
Run grants + Gateway -------->| schema/grant/approval checked |
                              +---------------+---------------+
                                              |
                                              v
                                      role=tool result
                                              |
                                              +----> next MODEL round
```

- RAG 提供“这次 Run 可参考什么”，不能调用工具，也不能增加 grant。
- MCP 提供“模型在本次 Run 被允许主动调用什么”，不能替代知识快照或引用来源。
- RAG 文本与 MCP 工具结果都是外部、不可信的模型输入。它们不能修改 Run 快照、Gateway 策略、审批绑定或 Workspace 边界。

## 加入 MCP 前后与运行影响

| 维度 | 加入 MCP 前 | 当前实现与影响 |
| --- | --- | --- |
| 能力 | 模型只能使用内置、控制面和 Workspace 工具 | 可接入 `stdio`、Streamable HTTP 和 SSE Server；只发布 Host 选择并批准的工具 |
| 安全 | 工具受 ToolSpec、grant、Workspace scope 和 Gateway 约束 | 保留原边界，再增加 inert 配置、目录审批、config/schema/policy fingerprint，以及每次 `mcp.write` 审批 |
| 确定性 | Run 冻结本地 ToolSpec 与 grants | 也冻结 MCP ToolSpec；Runner 恢复要求与当前 registry 精确匹配，调用前再核验 live fingerprint |
| 延迟/资源 | 无外部 MCP 建连、发现和远程执行成本 | discover、refresh 和每次实际调用都建立 session；每 Server 最多 4 个并发，每 Run 最多 32 个 MCP 调用 |
| 失败模式 | 没有 MCP Server 断线或目录漂移 | 超时、断线、Schema/config 漂移会撤下对应工具；不影响非 MCP 能力 |
| 副作用不确定性 | 本地受控写操作已有 durable start/人工核实边界 | 同一边界扩展到 `mcp.write`；超时或崩溃后进入 `outcome_unknown`，绝不自动重放 |
| 运维 | 无 Server 生命周期与目录状态 | 启动、启停和显式 refresh 会重建 registry；状态区分 pending、approved-disabled、enabled、drift 与 error |
| 现有 Run | 工具集合只含创建时的本地能力 | 不自动获得新 MCP 工具；禁用或 drift 后不能调用。仅短暂断线时，同一内存中仍持有 frozen handler 的 Run 可在后续调用新建 session 再试 |
| 新 Run | 从本地工具集合取快照 | 从当前 immutable registry 取快照，并按 Agent、父 Run、Task 请求和 Host 上限求 grants 交集 |

## 故障降级、边界与当前限制

### RAG

- Project 默认关闭 RAG；开启后命中的正文会发送给当前模型 Provider。
- 当前只有 SQLite FTS5/trigram 词法检索，没有向量数据库、Embedding 或重排模型。
- PDF 只读取文本层，没有 OCR；扫描件会以无文本失败。
- FTS capability 或索引器不可用不会阻止聊天、Runner 或 MCP 启动；知识上传/启用会被关闭或队列等待恢复。
- Project 边界来自可信的 `runs.session_id -> sessions.project_id`，而不是客户端传入的 Run 参数。
- RAG 快照不保存正文。删除或内容 hash 漂移后，旧 Run 不再 hydrate 正文；Session 后续切换 Project 不会改写旧 Run 已冻结的 Project 身份。
- 手工“检索预览”是 Host 操作，不要求打开自动注入开关，也不会改写任何 Run 快照。
- 模型输出的 `[Kx]` 是普通文本；UI 会把不存在于该 Run 快照的引用标为 unknown，不把它伪装成真实来源。

### MCP

- MCP Manager 没有持久连接池或后台自动重连循环。discover、refresh 与实际调用都创建短生命周期 session。
- 应用启动会 refresh 已启用 Server；单个 Server 失败只撤下其工具。整体 refresh 异常时 registry 清空，Runner 仍可使用其他非 MCP 能力。
- 环境变量 `MCP_SERVERS` 载入的配置也是 disabled/inert，不能绕过目录审批。
- `stdio` 要求绝对 executable/脚本路径，config hash 覆盖可执行内容，并使用临时 cwd 与最小子进程环境。远程 transport 只接受无凭据、query 和 fragment 的 HTTP(S) URL；远端实现本身不在 config hash 内，所以每次调用仍须核验 live 工具 fingerprint。
- 只接受两类远程策略：`read_only + approval=never` 或 `mcp.write + approval=always`。
- `mcp.write` grant 是 Agent/Task/父级/Host 上限的交集，能力变更只影响以后创建的 Run。
- 写审批绑定到持久化 tool call、参数、Server、config hash、Schema hash、Run attempt 和当前启动身份；过期、重启或内容变化后失效。
- Gateway 先校验参数、grant、来源、调用预算和审批，再接触远端 handler。远端错误正文不会作为可信策略输入。
- 对可能已经产生外部副作用的超时或崩溃，系统保留 unknown outcome 并要求人工核实，不做猜测性重试。
- refresh 最多处理 16 个 Server；单次发现最多 64 个工具。MCP 工具快照、参数与原始结果分别受 64 KiB、64 KiB、256 KiB 上限，回填模型的工具结果最多 8,000 字符。

### 共同安全边界

- RAG 不改变 ToolSpec、effective grants、Workspace scope、MCP allowlist 或审批状态。
- MCP 结果和 RAG 资料都可能包含提示注入。RAG 有显式不可信资料提示；MCP 结果仅作为 `role=tool` 数据回填。真正的权限决定始终在可信服务端。
- Context 和工具快照有独立大小上限；超限时失败或移除低排名 RAG Chunk，不静默扩大 Provider 请求。
- Host API 负责配置、上传和审批；模型不能自行启用 RAG、批准 MCP 目录或批准 `mcp.write`。

## 关键文件索引

| 文件 | 关键职责 |
| --- | --- |
| `static/app.js` | Task/Run 请求、知识库管理、SSE、Run 级 `[Kx]` 映射 |
| `static/index.html` | Project 知识库、RAG 开关、Run 引用与 MCP 管理 UI |
| `backend/api.py` | Task/Run、知识上传/检索/来源、MCP 生命周期与 Host 审批 API |
| `backend/app.py` | 启动顺序：DB、MCP refresh、RAGIndexer、TaskRunner；关闭时逆序 |
| `backend/db.py` | Task/Run 持久化、RAG 表/FTS/快照、MCP 配置/工具快照/审批绑定 |
| `backend/context.py` | 有界历史选择、RAG snapshot hydrate、不可信资料消息 |
| `backend/runtime.py` | Run claim/checkpoint、模型轮次、MCP 写审批、工具结果回填 |
| `backend/agent.py` | OpenAI-compatible 流式模型调用与 tool call 解析 |
| `backend/rag.py` | 确定性切块、来源复核、索引 lease、删除清理 |
| `backend/rag_extract.py` | 子进程内有界全文提取及格式限制 |
| `backend/mcp/manager.py` | discover/approve/enable/refresh、immutable registry、fresh-session 调用 |
| `backend/tool_gateway.py` | 唯一工具执行边界：Schema、grant、effects、审批、预算、脱敏 |
| `RAG_DESIGN.md` | RAG 数据模型、威胁模型、限制与验收细节 |
| `MCP_PLAN.md` | MCP 设计阶段、信任模型与实现约束 |
