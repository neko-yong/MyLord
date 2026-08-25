# 双向关系仲裁员 V1

一个可公网部署的双人关系调解 Streamlit 应用：

```text
独立陈述 → 争议地图 → 共享调解 → 暂停 / 恢复 → 双向复核仲裁
```

V1 使用 PostgreSQL 保存共享案件状态，A 与 B 可以从不同设备进入同一案件。LLM 配置只存在服务端，普通参与者不需要提供 Endpoint、Model 或 API Key。

> 本项目用于关系调解与结构化分析，不是法律裁判。如果涉及现实的人身威胁、暴力或胁迫，应优先处理现实安全。

## Architecture

```text
A browser ─┐
           ├─ Public Streamlit app ─┬─ PostgreSQL / Supabase
B browser ─┘                        └─ OpenAI-compatible LLM API
```

- PostgreSQL 是案件、陈述、消息、暂停状态与仲裁结果的唯一事实源。
- `st.session_state` 只保存当前浏览器的 `case_id`、`role` 和登录状态。
- A/B Token 使用 `secrets.token_urlsafe(24)` 生成；PostgreSQL 只保存 SHA-256 Hash。
- 聊天室和案件状态使用 `st.fragment(run_every="2s")` 近实时读取，不使用 WebSocket。
- LLM Key、数据库 URL 和管理员创建口令只从 Streamlit Secrets 或环境变量读取。

## Features

- 管理员创建口令限制新案件创建。
- 创建时只展示一次 Case ID、A Token 与 B Token。
- A/B 分别登录；失败提示不会区分案件不存在还是 Token 错误。
- 独立陈述由 `UNIQUE(case_id, role)` 保证每人只能提交一次。
- 参与者只看到自己的陈述正文和对方是否已提交。
- 双方提交后生成一份共享争议地图。
- 共享聊天、暂停与恢复状态写入 PostgreSQL，并每 2 秒同步。
- AI 法官介入消息以 `JUDGE` 身份写入共享消息表。
- 最终仲裁保留正常 A/B、交换标签与 Meta Judge 双向复核流程。
- V1 每个案件只允许生成一份争议地图和一份最终仲裁，避免并发重复调用。
- 生成前会用唯一占位预约避免重复模型调用；异常退出留下的空预约可在 15 分钟后自动重试。

## Database schema

应用首次连接时会用 `CREATE TABLE IF NOT EXISTS` 初始化：

- `cases`: `case_id`, `title`, `a_token_hash`, `b_token_hash`, `status`, `paused_by`, timestamps
- `statements`: 每个 `(case_id, role)` 唯一
- `artifacts`: `DISPUTE_MAP` / `FINAL_JUDGMENT`，每个案件与类型唯一
- `messages`: `A` / `B` / `JUDGE` / `SYSTEM` 共享消息

状态机：

```text
COLLECTING
→ READY_FOR_MAP
→ MAP_READY
→ MEDIATING
↔ PAUSED
→ CLOSED
```

旧的 `mediator.db` 不会自动迁移，也不会被 V1 生产代码读取。新部署从 PostgreSQL 空数据库开始。

## Local development

推荐本地也连接 Supabase PostgreSQL；项目不会在缺少 `DATABASE_URL` 时静默回落到 SQLite。

### 1. Create the environment

```powershell
cd relationship_mediator_v01
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. Configure local secrets

复制模板，但不要提交真实文件：

```powershell
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
```

编辑 `.streamlit/secrets.toml`：

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB"

LLM_ENDPOINT = "https://your-provider.example/v1/chat/completions"
LLM_MODEL = "your-model"
LLM_API_KEY = "YOUR_API_KEY"

ADMIN_CREATE_SECRET = "YOUR_ADMIN_CREATE_SECRET"
DEVELOPMENT_MODE = true

# Local developer tooling only. Never enable on the production deployment.
DEV_MODE = false
LLM_MODE = "real"
DEV_DATABASE_MODE = "postgres"
```

同名环境变量也可用于本地开发；Streamlit Secrets 优先于环境变量。

### 3. Run

```powershell
python -m streamlit run app.py
```

打开 `http://localhost:8501`。

如果未设置 `DATABASE_URL`，应用会显示“数据库尚未配置”，不会建立本地数据库。LLM Secrets 未配置时仍可进入案件并完成双方独立陈述，但不能生成 AI 内容。

# Public Deployment

## 1. Create Supabase Project

1. 登录 Supabase。
2. 选择 **New project**，设置数据库密码和区域。
3. 等待 PostgreSQL 项目创建完成。

不需要启用 Supabase Auth 或 Storage；本项目只使用 PostgreSQL。

## 2. Get PostgreSQL Connection String

1. 在项目 Dashboard 点击 **Connect**。
2. Streamlit 是持续运行的后端，优先复制 **Session pooler** connection string（通常是端口 `5432`，也适合仅 IPv4 的托管环境）。
3. 用实际数据库密码替换连接串中的占位符，并保留控制台建议的 SSL 参数。

如果使用 transaction-mode pooler（通常端口 `6543`），本项目已关闭 psycopg prepared statements 以保持兼容。

不要把 connection string 写入源码、README 或 Git。

## 3. Push Repository to GitHub

确认以下文件不会进入提交：

```text
.streamlit/secrets.toml
.env
*.db
```

然后将项目代码推送到自己的 GitHub Repository。本仓库中的 `.streamlit/secrets.toml.example` 只有占位符，可以提交。

## 4. Deploy on Streamlit Community Cloud

1. 打开 Streamlit Community Cloud。
2. 选择 **Create app**。
3. 选择 GitHub repository、branch 和入口文件 `app.py`。
4. 打开 **Advanced settings**。

## 5. Configure Secrets

在部署时的 **Advanced settings → Secrets**，或部署后的 **App settings → Secrets** 中填写：

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB"

LLM_ENDPOINT = "https://your-provider.example/v1/chat/completions"
LLM_MODEL = "your-model"
LLM_API_KEY = "YOUR_API_KEY"

ADMIN_CREATE_SECRET = "YOUR_ADMIN_CREATE_SECRET"
DEVELOPMENT_MODE = false
DEV_MODE = false
LLM_MODE = "real"
DEV_DATABASE_MODE = "postgres"
```

不要把 `.streamlit/secrets.toml` 提交到 GitHub。公开部署保持 `DEVELOPMENT_MODE = false`，避免向普通用户显示技术诊断信息。

同时必须保持 `DEV_MODE = false`（或完全不配置）。即使误配
`LLM_MODE = "mock"`，应用也会在 `DEV_MODE = false` 时强制使用正式
LLM 路径。不要在 Streamlit Cloud 正式环境启用 Developer Playground；
它不是管理员后台或生产运维后台。

## 6. Open Public URL

部署成功后打开：

```text
https://your-app.streamlit.app
```

侧栏应显示数据库已连接；LLM 三项 Secret 齐全时显示 AI 法官已就绪。启动不会发起真实 LLM 请求或消耗 Token。

## 7. Create Case

在首页展开“创建新案件”，输入 `ADMIN_CREATE_SECRET` 和案件标题。立即保存：

- Case ID
- A Token
- B Token

服务器只保存 Token Hash，无法找回原始 Token。

## 8. Send Partner

只向 B 发送：

```text
Public URL
Case ID
B Token
```

A 保留 A Token。不要把 A/B 两个 Token 一起发送给同一个人。

## Security notes

- SQL 全部使用 psycopg 参数绑定。
- API Key 不进入 URL、数据库、页面或日志。
- 数据库 URL 和管理员创建口令不进入页面或数据库。
- 技术错误详情默认关闭，且 LLM 响应会截断和移除认证内容。
- 本项目没有端到端加密；数据库管理员和应用服务端可以访问调解内容。
- V1 未实现登录速率限制。强随机 Token 降低猜测风险，但公网高流量部署仍应在反向代理或平台层增加 Rate Limit。

## Tests

```powershell
python -m compileall -q -x "[\\/](\.venv|__pycache__)[\\/]" .
python -m unittest discover -s tests -v
```

需要真实测试库时，先设置 `TEST_DATABASE_URL` 再运行测试。未配置时 PostgreSQL 集成测试会明确报告 `POSTGRES_REAL_TEST = NOT RUN`，不会伪造结果。

## Developer Workflow

Developer Playground 只用于本地或独立测试部署，提供标准虚构 Fixture、
合法状态机 Scenario、Mock LLM、失败注入、A/B 快速视图和安全调试面板。
它不通过 query parameter、cookie、普通表单或管理员创建口令开启；唯一入口
是服务端 `DEV_MODE`。所有测试案件使用 `[DEV_TEST]` 标题前缀，删除和身份
切换会在服务端再次校验此前缀。

`DEV_MODE` 是 Developer Playground 的服务端总开关。旧的
`DEVELOPMENT_MODE` 只控制少量错误诊断显示，不会启用 Playground、Fast
Local、Mock、身份切换或失败注入。

### DEV_FAST

```toml
DEV_MODE = true
DEV_DATABASE_MODE = "local"
LLM_MODE = "mock"
```

用于日常表单、UI 和状态机开发。Fixture、案件、消息和 Artifact 只保存在
当前 Streamlit Session 的内存中；不会连接 PostgreSQL，也不会调用
DeepSeek。Local wrapper 每次 rerun 重新创建，不使用 `st.cache_resource`；
实际数据保存在当前 Session 的 `_dev_local_store` 中。

Fast Local 不跨浏览器共享，也不能证明 PostgreSQL 事务、`FOR UPDATE`、连接池
或跨客户端同步正确。它只用于 UI / workflow development。

### DEV_LLM

```toml
DEV_MODE = true
DEV_DATABASE_MODE = "local"
LLM_MODE = "real"
```

使用 Fast Local 数据测试正式 Prompt / Real LLM，不写远程数据库。Playground
会显示费用警告，并要求当前 Session 明确确认真实模型调用。

### DEV_INTEGRATION

```toml
DEV_MODE = true
DEV_DATABASE_MODE = "postgres"
LLM_MODE = "mock"
```

连接真实测试 PostgreSQL，用于并发、Evidence Freeze、checkpoint 恢复和
数据库完整性验证。请使用专用 `TEST_DATABASE_URL`，并精确清理生成的
`[DEV_TEST]` 案件。

### RELEASE_SMOKE

```toml
DEV_MODE = false
DEV_DATABASE_MODE = "postgres"
LLM_MODE = "real"
```

使用真实 PostgreSQL 与真实 DeepSeek 完成发布前冒烟。正式 Streamlit Cloud
Secrets 绝不能设置 `DEV_MODE = true`，除非该部署明确是隔离测试环境。
当 `DEV_MODE = false` 时，即使误配 `DEV_DATABASE_MODE = "local"` 或
`LLM_MODE = "mock"`，运行时仍会强制使用 PostgreSQL 与 Real LLM。

| Mode | Database | LLM | 用途 |
|---|---|---|---|
| DEV_FAST | Fast Local | Mock | 日常 UI / workflow 开发 |
| DEV_LLM | Fast Local | Real | Prompt / 模型集成测试 |
| DEV_INTEGRATION | Real PostgreSQL | Mock | 数据库事务与并发测试 |
| RELEASE | Real PostgreSQL | Real | 正式发布路径 |

Playground 中选择 **Real** 会显示费用警告并要求本次会话确认；默认始终是
**Mock**。真实模式继续使用现有正式 Prompt、DeepSeek 配置、token budget 与
checkpoint，不使用测试 Prompt。Raw A/B Token 仅在创建该 Dev Case 的当前
`st.session_state` 中短暂保存，不写入数据库明文，也不在调试面板中展示。
