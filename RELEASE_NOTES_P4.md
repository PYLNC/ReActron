# ReActron 更新说明（截至 P4）

本次更新从 `6b623226` 演进至 `ef97894e`，将原有单会话聊天扩展为可持久运行、可委派任务的本地 Agent 系统。P5 暂不实施。

## 已交付

- **P0 · 本地安全边界**：启动时在终端生成一次性认证码；Host API 使用本地认证，并对工具调用实行明确的权限和审计边界。凭据不会作为普通 API 响应返回。
- **P1 · 持久任务运行**：Task、Run、事件和检查点保存在 SQLite；支持排队、暂停、恢复、取消、租约及重启恢复。对于可能已发生副作用但结果不明的调用，保持待人工处理，不自动重放。
- **P2 · Supervisor/Worker 协作**：显式创建根 Supervisor，并由其管理 Worker、委派任务和收取消息；Worker 可并行执行，各 Agent 同时只运行一个 Run。问题答复和子任务结果按原始 Run 精确唤醒，Worker 不可自行扩大授权。
- **P3 · Workspace 与 Git 隔离**：Host 注册 Workspace，任务绑定工作区；Git 写任务使用独立 worktree。提供受范围限制的文本读取、写入和 diff 查看，阻止越界路径及不安全的 worktree 清理。任务结束后 worktree 转为 `retained` 并保留，不自动删除或删分支；仅用户显式请求清理、且没有活跃 Run、worktree 干净时，才执行普通 `git worktree remove`，不使用 `--force`。
- **P4 · 单次写入审批**：`workspace_write_text` 必须由本地 Host 用户在当前根 Supervisor 对应的审批入口逐次批准；批准与具体 Run、工具调用及写入内容绑定。拒绝、过期、重启后的旧批准均不能用于执行。旧的通用审批入口不能绕过这条写入审批流程。

界面已增加 Agent 树、任务、消息、Workspace、diff 和写入审批入口；原有聊天入口继续保留。

## 运行

建议使用 Python 3.11，在项目根目录安装依赖后启动单进程服务：

```bash
python -m pip install -r requirements.txt
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8001 --workers 1
```

打开 <http://127.0.0.1:8001>，输入启动终端显示的一次性认证码。使用模型聊天前，在页面“设置”中填写 DeepSeek API Key，或在本地 `.env` 配置；不要把密钥提交到仓库。

## 验证与当前边界

- P4 集成时全量测试共 217 项：216 通过、1 项因平台差异跳过；最后的启动 nonce 修复后另有 25 项定向测试通过，未重复运行全量测试。
- P5 的写入后 Artifact 持久内容快照尚未交付；现有 Artifact 数据结构不等于完整的快照交付能力。
- 不自动 commit、push 或 merge，也不自动清理已完成任务的 worktree。
- 内部子任务重试的 Event reserve 边界仍待完善；当前公开 API 不提供该重试路径。
