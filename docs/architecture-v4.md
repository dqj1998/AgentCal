# AgentCal 平台架构设计方案 v4.0（实现依据）

> 状态：**decision-complete**，可直接据此拆任务开工。
> 替代：`AgentCal 平台架构设计方案.pdf`（v3.0），v3.0 仅作背景资料，冲突处以本文件为准。
> 评审依据：v3.0 的 5 条 P0（真相源反转、防篡改名不副实、无授权模型、隐私定位矛盾、无协作设计）已在 §2/§3/§5/§9 逐条闭环；21 条 P1/P2 的处置见 §12 差异表。
> 落库位置：开工时移入 `AgentCal/docs/architecture-v4.md`（现放项目根目录便于与 v3.0 对照）。

---

## 0. 一句话定位

单机、单用户、**日历驱动**、**Telegram 干预**的自动化执行平台：人类在 Google Calendar 用自然语言下达任务，本地调度器解析、鉴权、排队，由浏览器执行器完成对外部站点的操作，异常时通过 Telegram 让人介入。

明确的**能力收缩**（相对 v3.0 的说法）：

| v3.0 说法 | v4.0 决定 | 理由 |
|---|---|---|
| "多智能体协作平台" | 单个 Task 由一个 Agent 定义执行；agent 之间不协作 | 协作协议、handoff、冲突裁决尚未设计，写了也不能实现；MVP 阶段协作是负债 |
| "处理复数级任务" | 并发上限 1 个浏览器 run、2 个 LLM 调用 | 单机 + 单 Chromium profile 的物理约束 |
| "通过 OpenCode 编写新 Agent 逻辑" | 新 agent = `agents/<name>.toml` 声明 + 可选 Python step | 开发工具不进运行时架构 |
| "Event Bus (Redis/SQLite)" | **不引入 Redis**；SQLite + asyncio 队列 | 单进程单机，Redis 是额外故障源；跨机再引入 |

若确实需要多 agent 协作，见 §10 M4 的契约草案，不进入 MVP 范围。

---

## 1. 五条第一原则（所有实现必须服从）

1. **真相源唯一**：本地 SQLite 是任务状态唯一事实。Calendar 与 Telegram 都是**投影（projection）+ 输入通道**，永不参与状态判定。
2. **幂等先行**：任何外部副作用执行前，必须先落一条可查的 `claim`（本地事务），且副作用前记录 `publish_intent`（内容哈希）。
3. **投影可失败**：日历写回、Telegram 广播失败只进重试队列，绝不阻塞或回滚任务推进。状态推进只看本地事务是否提交。
4. **控制面必须鉴权**：日历事件、Telegram 指令、语音消息都视为不可信输入，必须通过 `allowed actor + HMAC/组织者校验 + policy gate` 三道检查。
5. **参数不出本机**：云端只允许出现 `task_id + 人类可读摘要 + 签名`；URL、账号、payload 一律本地加密存储。

---

## 2. 架构

```
┌─ 输入通道 (Channels) ─────────────────────────────────────────┐
│ Calendar Poller(60s, syncToken)   Telegram Bot(长轮询)   STT  │
└───────────────┬───────────────────────┬──────────────────────┘
                │ Command               │ Command
                ▼                       ▼
┌─ 控制面 (Control, 单进程 asyncio) ────────────────────────────┐
│ Normalizer → Auth/Policy Gate → Idempotency Store → Scheduler │
│                                          │                    │
│        SQLite(WAL, 真相源) ◄─────────────┤ FSM/Lease/Ledger   │
│                                          │                    │
│        Projectors(address→Calendar/TG) ◄─┘                    │
└───────────────────────────┬───────────────────────────────────┘
                            │ JSON-RPC over UDS
                            ▼
┌─ 执行面 (Execution, 独立子进程) ──────────────────────────────┐
│ Executor worker (每 profile 一个)  ── Playwright / CDP attach │
│                                     ── Humanizer / Grounding  │
│ MCP Tool Host (spawn/health/timeout/quarantine)               │
└───────────────────────────────────────────────────────────────┘

┌─ 观测面 (Observability) ──────────────────────────────────────┐
│ JSONL 日志 + metrics 快照 + artifact 目录（截图/DOM/traceback）│
└───────────────────────────────────────────────────────────────┘
```

组件职责与边界（这张表是评审时的抓手，不允许含糊）：

| 组件 | 职责 | 明确不做 |
|---|---|---|
| Calendar Poller | `events.list(syncToken)` 增量拉取、事件 → Command | 不解析意图、不写回 |
| Telegram Bot | 上行指令/语音 → Command；下行投影 | 不做业务判定 |
| Normalizer | 三源 Command 归一化 | 不做鉴权 |
| Auth/Policy Gate | actor 白名单、签名验签、目标域白名单、action 风险分级 | 不做重试 |
| Idempotency Store | `idem_key` 唯一约束 + 状态 CAS | 不做业务 |
| Scheduler/FSM | claim、lease、重试退避、状态转移 | 不直接做 IO |
| Projectors | 把 DB 状态写成 Calendar colorId/description、Telegram 卡片 | **不读回自己写的数据做判定** |
| Executor | 单 run 的 step 执行、journal、resume | 不调度其它 run |
| MCP Host | 工具进程生命周期 | 不做 agent 编排 |
| Artifact Store | 大对象落盘（DOM/Traceback/截图） | 不进 DB 大字段 |

**为什么投影不能反向驱动**：Poller 会看到自己刚写的 `colorId` 变更，若判定依赖日历就会自激。实现约束：`command.source_event_id` 与本地 `projected_event_ids` 比对，自己的写入直接丢弃；判定分支代码里禁止读取 `colorId`。

---

## 3. 领域模型

存储：SQLite 3（WAL, `synchronous=NORMAL`, 单写线程）。迁移用编号 SQL 文件 + `schema_migrations`。所有时间用 UTC ISO-8601 字符串 + 索引。

```sql
-- 任务：从"人类意图"到"终态"
CREATE TABLE tasks (
  id             TEXT PRIMARY KEY,             -- tsk_<ulid>
  idem_key       TEXT NOT NULL UNIQUE,         -- 见 §4.3
  source         TEXT NOT NULL,                -- calendar | telegram | voice
  actor_id       TEXT NOT NULL,                -- 日历 organizer / TG user_id
  title          TEXT NOT NULL,                -- 人类摘要（可上云）
  intent_enc     BLOB NOT NULL,                -- NaCl secretbox(payload)，永不上云
  agent          TEXT,                          -- agents/<name>，解析后填充
  confidence     REAL,
  state          TEXT NOT NULL,                -- pending|claimed|running|blocked_human|success|failed|canceled|missed
  attempt        INTEGER NOT NULL DEFAULT 0,
  not_before     TEXT NOT NULL,                -- 抖动后的最早开始时刻
  due_at         TEXT NOT NULL,
  policy_snap    TEXT NOT NULL,                -- 判定时的 policy 快照(JSON)
  calendar_id    TEXT, event_id TEXT, event_etag TEXT,
  created_at     TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE runs (
  id         TEXT PRIMARY KEY,                 -- run_<ulid>
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  attempt_no INTEGER NOT NULL,
  state      TEXT NOT NULL,                    -- leased | running | blocked | succeeded | failed | orphaned
  lease_owner TEXT, lease_expires_at TEXT,     -- 心跳 15s，租期 90s
  started_at TEXT, ended_at TEXT, exit_reason TEXT,
  UNIQUE(task_id, attempt_no)
);
-- 不变式：一任务至多一个活跃 run（索引必须建在 runs 之后）
CREATE UNIQUE INDEX ux_tasks_active_run ON runs(task_id)
  WHERE state IN ('leased','running','blocked');
CREATE INDEX ix_tasks_due ON tasks(state, not_before);        -- 调度器扫描
CREATE INDEX ix_runs_lease ON runs(state, lease_expires_at);  -- 对账扫描

CREATE TABLE steps (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  idx INTEGER NOT NULL, kind TEXT NOT NULL,    -- navigate|type|click|extract|wait|publish|ask_human
  target TEXT, input_ref TEXT, output_ref TEXT,-- 大对象指向 artifacts
  state TEXT NOT NULL,                         -- pending|done|failed|skipped
  error_class TEXT, started_at TEXT, ended_at TEXT,
  UNIQUE(run_id, idx)
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY, run_id TEXT, kind TEXT NOT NULL, -- dom|screenshot|traceback|response
  path TEXT NOT NULL, bytes INTEGER, sha256 TEXT, created_at TEXT NOT NULL
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT,
  reason TEXT NOT NULL,                       -- captcha|2fa|risk_action|unknown_side_effect
  requested_at TEXT NOT NULL, expires_at TEXT NOT NULL,   -- 默认 +24h
  channel_chat_id TEXT, channel_msg_id TEXT, channel_thread_id TEXT,
  decision TEXT, decided_by TEXT, decided_at TEXT          -- approve|deny|timeout
);

CREATE TABLE commands (            -- 输入事件流水，幂等与审计的唯一入口
  id TEXT PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL, actor_id TEXT NOT NULL, raw_ref TEXT,
  parsed_json TEXT, signature_ok INTEGER NOT NULL,
  policy_verdict TEXT NOT NULL,               -- allow|reject:<reason>|ask
  received_at TEXT NOT NULL, applied_at TEXT
);

CREATE TABLE projector_state (
  kind TEXT PRIMARY KEY,                      -- calendar | telegram
  cursor TEXT,                                -- calendar syncToken / tg update_id
  last_full_sync_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tg_messages (
  chat_id TEXT, message_id INTEGER, thread_id INTEGER,
  task_id TEXT, kind TEXT, updated_at TEXT,
  PRIMARY KEY(chat_id, message_id)
);

CREATE TABLE llm_calls (
  id TEXT PRIMARY KEY, run_id TEXT, route TEXT NOT NULL,
  model TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
  cost_usd REAL, latency_ms INTEGER, schema_ok INTEGER, created_at TEXT NOT NULL
);
```

**密钥与加密**：`intent_enc` 用 NaCl secretbox，主密钥存 macOS Keychain（service `agentcal`，account `master`）；Google refresh token、Telegram bot token 同样只进 Keychain。进程启动时取一次，内存持有，退出清零。Keychain 锁定 → 进程进入 `degraded` 并 Telegram 告警，不静默降级为明文。

---

## 4. 状态机

### 4.1 状态集合

Task：`pending → claimed → running → {success | failed | blocked_human | canceled | missed}`；`blocked_human → running | canceled | failed`。
Run：`leased → running → {succeeded | failed | orphaned}`，`running → blocked → running`（人工介入挂起时释放租约）。

> 实现注记（M0）：`blocked` 是 run 的独立状态而非“假 running”，否则对账会把等待人工的任务误判为孤儿。

### 4.2 转移表（实现直接照抄，一行一个 handler）

| 当前状态 | 事件 | 条件 | 动作 |
|---|---|---|---|
| pending | `due(now>=not_before)` | policy=allow | CAS `pending→claimed`；建 run(lease 90s)；入队 |
| pending | `due` | policy=reject | `failed(rejected)`；投影 Tomato + 原因 |
| pending | `cancel` | — | `canceled`；投影 Graphite |
| pending | `missed` | `now > due + 10min` 且从未 claim | 按 `missed_policy`：`run_late`（默认）→ claimed；`ask` → Telegram 询问 |
| claimed | `worker_start` | — | `running`；投影 Banana（延迟 ≥2s 去抖） |
| claimed | `lease_expired` | 未开始执行 | run `orphaned`；task 回 `pending`，`attempt+1`，退避 |
| running | `step_failed(transient)` | `attempt < 3` | 关闭 run（`failed`）→ task 回 `pending`，`not_before = now + backoff[attempt]`（30s/2m/8m）；重试是新 attempt/新 run |
| running | `step_failed(transient)` | 已达上限 | `failed`；投影 Tomato |
| running | `step_failed(human_required)` | captcha/2FA/风险动作 | `blocked_human`；冻结 journal；投影 Tangerine；Telegram 卡片 |
| running | `step_failed(policy)` | 目标域不在白名单 | `failed(policy)`；不重试 |
| running | `lease_expired` | 进程死亡 | run `orphaned` → 见 §4.4 对账 |
| running | `all_steps_done` | — | `success`；投影 Basil；日志摘要写回日历 |
| blocked_human | `approve(t)` | resume 点存在 | `running`，从 step 断点续跑 |
| blocked_human | `deny(t)` | — | `canceled` |
| blocked_human | `ttl(24h)` | — | `failed(approval_timeout)` |
| any | `human_cancel` | 非终态 | `canceled`；杀 run |

### 4.3 幂等键

- 日历：`cal:{event_id}:{event.updated}` —— 人类每次编辑 `updated` 变化，视为新意图；同一 `updated` 重复投递只执行一次。
- Telegram 指令：`tg:{chat_id}:{message_id}`。
- 语音：`tg:{chat_id}:{message_id}:voice`（同一条消息只转录一次）。
- Run 层：`(task_id, attempt_no)` 唯一，重试是新 attempt，不是新 task。

### 4.4 启动对账（Reconcile，MVP 必做）

进程启动与每 5 分钟各跑一次：

1. `running/claimed` 且 `lease_expires_at < now` → run `orphaned`。
2. 该 run 的最后一个 `step.kind == publish` 且无 `output_ref` → **副作用状态未知**：task → `blocked_human(reason=unknown_side_effect)`，禁止自动重试。
3. 否则 task → `pending`，`attempt+1`。
4. `projector_state.cursor` 与日历对账：本地 pending 但日历无对应事件 → 事件被人类删除 → `canceled`。

**非幂等副作用**（发帖、提交表单）的硬规则：`publish` step 之前必须写 `publish_intent{content_sha256, target}`；失败后**永不自动重放**，一律走人工确认（Telegram 卡片附截图与目标页链接）。

---

## 5. 接口契约

### 5.1 日历通道

**专用日历**：新建 secondary calendar `AgentCal`，`calendarId` 写配置；禁止使用主日历（避免 `syncToken` 全量遍历与污染人类日程）。

| 字段 | 规则 |
|---|---|
| `summary` | 人类自由文本 ≤1024；同时作为 `tasks.title` 上云 |
| `description` | 三区格式，见 §5.2，硬上限 **8192 字符** |
| `colorId` | 由投影器显式写入，见 §5.3；不依赖默认色 |
| `reminders` | `useDefault=false, overrides=[]`；避免系统日常提醒干扰 |
| `sendUpdates` | 所有写操作传 `sendUpdates=none`；避免通知邮件轰炸 |
| 时区 | 事件 `start.timeZone` 取自 `settings.get(timeZone)`；相对时间换算必须用该 TZ + 当前绝对时间。**存储前必须把 `dateTime` 归一化为 UTC `Z` 秒级字符串**：Google 返回带 `+09:00` 等偏移，直接与 `Z` 字符串比较会推迟数小时才可执行（实测踩过）。 |

**轮询协议**：

```
首次:  events.list(calendarId, timeMin=now-7d, singleEvents=true, showDeleted=true)
增量:  events.list(calendarId, syncToken=cursor, singleEvents=true, showDeleted=true)
410:   丢弃 cursor → 重回首次 → 触发 §4.4 第 4 步对账
429/5xx: 指数退避 5s→60s，最多 5 次；连续失败 10 次 → Telegram 告警，进程转 degraded
周期:  60s tick；tick 由 Supervisor 内的定时器驱动（不依赖 launchd 补跑语义）
```

**授权与重新授权（D1）**：个人 Gmail 没有 Workspace Internal 应用的免验证路径，开发期挂在 Testing 态时 refresh token 可能 7 天后失效——因此把“失效”当**常态事件**处理，而不是异常：

- `agentcal auth login`：本地 loopback（`127.0.0.1:8765`）+ PKCE，scope 只申请 `calendar.events` + `calendar.readonly` + `calendar.settings.readonly`；token 进 Keychain。
- `agentcal auth status`：打印存在性 / 过期时间 / scope，**不打印 token 本身**。
- 启动时与每日 03:00 主动校验；失效 → 进程 `degraded`（停 poller，保留执行中的 run），Telegram 发一张含“请在本机执行 `agentcal auth login`”的卡片。
- 重授权完成后自动恢复，并丢弃 cursor 做一次全量重建，补跑停机期间的日历变更。
- 上线前再评估是否转 Production（需隐私政策与演示材料）；转与不转不影响本实现。

**休眠策略**：笔记本合盖期间 tick 停止；唤醒后立即执行一次 tick。`due_at + 10min` 未处理的进入 `missed`，按 `missed_policy` 处理（默认补跑一次，`ask` 模式询问）。

**重复事件**：MVP 支持 Google 生成的 recurring instance（用 `recurringEventId + originalStartTime` 作 idem key 的扩展位）；对 `status=cancelled` 的 instance exception 跳过并只记日志。整系列创建/编辑不支持（v1.1）。

### 5.2 `description` 三区格式

```
<A: 人类 PROMPT，自由编辑，建议 ≤1500 字符>

---------- RUN LOG (auto, do not edit) ----------
<B: 投影器写入，≤800 字符，固定 6 行模板>

---------- PARAMS v1 (do not edit) ----------
<C: 单行 base64url(JSON)，≤600 字符>
```

预算：1500 + 800 + 600 + 分隔符 ≈ 3000，留足余量给人类。写前守卫：拼接后 `len > 7800` → 截断 B 区并告警；C 区超限 → 拒绝写入（说明 payload 过大，必须留在本地）。

B 区模板（固定 6 行，超出只保留最后 6 条状态变更）：

```
state: running | attempt: 1 | started: 2026-11-17T02:10:11Z
step: 3/7 publish → x.com
last_error: -
artifacts: /artifacts/run_01J.../  (3 files)
updated: 2026-11-17T02:12:40Z
```

C 区信封（**不是**防篡改的 Base64，而是签名信封）：

```json
{
  "v": 1,
  "tid": "tsk_01JB8Z3K5Q",
  "nonce": "8f1c2a",
  "exp": "2026-12-01T00:00:00Z",
  "agent": "x-publisher",
  "payload_ref": "vault:tsk_01JB8Z3K5Q",
  "sig": "<base64url(HMAC-SHA256(master_key, canonical_json))>"
}
```

- `canonical_json`：键排序、无空格、UTF-8。
- 校验失败/`exp` 过期/`tid` 与事件不匹配 → **不执行**，记录 `reject:<reason>` 并告警。
- **C 区缺席 = 人类意图**：人类直接在日历里写自然语言是产品主路径，这类事件没有 PARAMS，不走签名，而是走“组织者必须是本人 + 分类路由”的通道；一旦存在 PARAMS 就必须验签。否则人类根本没法下单，而签名又失去了意义。
- `payload_ref` 指向本地加密库；日历里**永不出现** URL、账号、正文。

**写回协议**（解决并发覆盖）：

```
1. events.get(eventId)  → 拿到 etag
2. 本地读 DB → 只替换 B 区（A/C 原样保留）
3. events.patch(eventId, {description, colorId}, If-Match: etag, sendUpdates:"none")
4. 412 (Precondition Failed) → 重读重合并重试，最多 3 次；仍失败 → 记住脏投影，下个 tick 再试
```

三区分区合并规则：A 区以日历为准（人类最新），B 区以本地为准，C 区以本地为准但签名随写。任何一方检测到对方区域被非预期修改 → 记 `projection_conflict` 事件 + 告警（不自动覆盖）。

### 5.3 colorId 映射（必须写死，禁止用"黄色/红色"描述）

| colorId | Google 名称 | 语义 | 谁写 |
|---|---|---|---|
| 9 | Blueberry | pending / claimed | 投影器 |
| 5 | Banana | running | 投影器 |
| 6 | Tangerine | blocked_human（待介入） | 投影器 |
| 10 | Basil | success | 投影器 |
| 11 | Tomato | failed | 投影器 |
| 8 | Graphite | canceled / paused | 投影器 |

不用 `colorId` 1/2/3/4/7，不使用"默认色"（用户主题一变就会误判）。UI 上的"⚪默认"不再作为状态。

### 5.4 抖动与错峰（修正 v3.0 的 `-15~+30`）

- 语义：**错峰执行**，不是反检测。
- 参数：`not_before = due_at`（不提前，仅允许 0~90s 的随机延迟，均匀分布）。
- 日历事件的 `start` 永不修改——人类看到的时间必须可信。
- 真正的反检测压力放在 §6 的交互节奏上，不放在调度上。

### 5.5 Telegram 通道

**上行**（必须匹配命令或 @bot，否则忽略）：

```
/task <自然语言>        /status [id]        /retry <id>
/approve <id> [note]    /deny <id>          /pause  /resume
语音消息 → STT → 同 /task 流程
```

**鉴权**：`allowed_user_ids`（精确匹配 `from.id`）+ `allowed_chat_ids`；群内开启 bot privacy mode 后只收命令与 @提及；`from.id` 不在白名单 → 记录并静默丢弃（不回错误信息，避免探测）。

**inline keyboard 编码（硬约束 64 字节）**：

```
callback_data = "<card>:<short_id>:<action>"
card    ∈ {k, q}          // k = 任务通知卡，q = 审批卡
short_id = base32(crc32(task_id) + seq)[:8]   // 8 字节，映射表存 approvals / tg_messages
action  ∈ {a, d, r, t}    // approve / deny / retry / trace
```

示例：`q:7K3M9Q2P:a` = **12 字节**，余量充足。禁止把 JSON、task_id 全量或 run_id 塞进 `callback_data`。

**下行约束**：

| 约束 | 处理 |
|---|---|
| `answerCallbackQuery` 10s 内必须响应 | 收到即 ack `"已受理"`，后续异步 edit |
| 群 `editMessageText` 约 20 msg/min | 单聊天合并器：状态变更去抖 2s，同 task 1 秒内只 edit 一次 |
| 429 + `retry_after` | 严格按返回值退避，不叠加本地重试 |
| 消息长度 4096 | 摘要模板固定 ≤1200 字符，详情回 `/status` 拉取 |
| 图片 10MB | 截图降采样到 1280px 宽；原图只进 artifact |

**消息映射**：`tg_messages` 记录 `(chat_id, message_id, thread_id)`，恢复投影时用 `thread_id` 保证 forum topic 不串台。

### 5.6 LLM 路由与输出约束

统一经 LiteLLM proxy（本地 `127.0.0.1`），只暴露 route 名，代码里不出现裸模型名。

| route | 模型级 | 用途 | 输出约束 |
|---|---|---|---|
| `task_is_task` | SLM | 二分类：是否是任务 | enum `{"is_task":bool}` + logprob |
| `task_route` | SLM | agent + 参数抽取 | JSON Schema + grammar |
| `dom_summarize` | SLM | DOM 裁剪摘要 | 自由文本 ≤400 token |
| `copywrite` | LLM | 营销文案 | 自由文本 + 长度约束 |
| `code_assert` | LLM | 页面断言生成 | JSON Schema |
| `vision_ground` | VLM | 截图 → 定位 | Schema，见 §6.4 |

**JSON 可靠性**：`format=json` / grammar constrained decoding（Ollama、outlines、lm-format-enforcer）+ 本地 JSON Schema 校验 + 1 次修复重试；两次失败 → 降级到上一级模型；再失败 → `step_failed(transient)`。**禁止**只靠 "system prompt 要求输出 JSON"。

**置信度来源**（必须有出处，不能是模型自报）：

- 主用：约束字段的 token logprob → `p = exp(min_logprob)`。
- 备用：5 次采样一致性（`agree/5`）。
- 校准：≥100 条人工标注集，画 reliability curve，选阈值写进配置。

**三档决策**（修正 v3.0 把"低置信度"和"私人日程"混为一档）：

```
Stage 1: is_task        → false 且事件无 @agentcal 标记 → 静默忽略（私人日程）
Stage 2: route + params → p ≥ t_auto(0.90)          → 自动执行
                        → t_ask ≤ p < t_auto(0.60)  → Telegram 询问
                        → p < t_ask                 → 不入库，仅记日志
强制人工：风险动作（见 §7 policy）无条件进入审批，与置信度无关
```

### 5.7 MCP 工具宿主

**边界**：MCP 只承载"工具/资源暴露"，**不承载**调度、状态、路由。所谓"热加载"= 重扫 `mcp.json` 并 spawn 新 server，与主进程代码无关。

| 职责 | 参数 |
|---|---|
| spawn | `stdio`，启动超时 10s |
| 健康检查 | `ping` 每 5s，连续 3 次失败 → 标记 unhealthy |
| 调用超时 | 默认 60s，`browser.*` 120s |
| 重启 | 5 分钟内 ≤3 次；超限 quarantine 1h + 告警 |
| schema 版本 | 启动时读 `serverInfo.version`，与 `mcp.lock.json` 比对，不匹配则拒绝装载 |
| 授权 | 每次工具调用过 policy gate（工具白名单 + 目标域白名单） |

MVP 工具白名单：`browser.navigate|click|type|extract|screenshot`、`http.fetch`、`fs.read/write`（仅 `sandbox/` 目录）、`calendar.note`。**不提供** `shell`（默认拒绝，按需在 agent 定义里显式开启并记录审计）。

---

## 6. 执行引擎

### 6.1 进程模型

```
launchd (KeepAlive)
└─ supervisor.py            # 单实例锁(flock)，崩溃重启
   ├─ control loop          # poller/projector/scheduler/fsm（asyncio）
   └─ executor worker × N   # N=1（MVP）；JSON-RPC over UDS
        └─ MCP host          # 独立子进程树
```

- Supervisor 崩溃 → launchd 拉起 → 执行 §4.4 对账。
- Executor 崩溃 → supervisor 检测 socket 断开 → run `orphaned` → 对账流程。
- 单实例：`flock /tmp/agentcal.lock`，第二个实例直接退出（防止双写日报）。

### 6.2 浏览器 profile 策略（决定 v3.0 的取舍）

Chromium 的 `SingletonLock` 使同一 profile 同时只能被一个实例占用。两种模式择一，不混用：

| 模式 | 适用 | 做法 | 代价 |
|---|---|---|---|
| **A. CDP attach（首选）** | 目标站点需要真实登录态、风控严格 | 用户手动启动 Chrome（专用 profile 目录），`--remote-debugging-port=9222`；Executor `connect_over_cdp` | 需要 Chrome 常驻；需 `caffeinate` 防休眠 |
| B. Playwright persistent context | 无真实 Chrome / 一次性任务 | 每 profile 目录一个锁文件，独占启动 | 指纹与真实用户差异大，风控命中率高 |

轮换与隔离：profile 目录 `profiles/<name>/`，`profile.lock` 用 `flock`；acquire 失败 → 任务排队而非并发。会话失效（跳登录页）→ `blocked_human(reason=session_expired)`，由人手动登录一次。

**stealth 说明**：v3.0 把"持久化 User Data Dir"称作"指纹伪装"，这是概念错误——那是会话态。指纹是 canvas/WebGL/audio/fonts/TLS。实现上：

- 优先走模式 A（真实 Chrome 自带真实指纹），不依赖补丁。
- 补丁方案（playwright-stealth 及社区 fork / patchright / camoufox 一类）**版本演进快，选型前必须复核当前维护状态与 Playwright 版本兼容性**，并记录在 `docs/adr/0004-stealth-patch.md`。
- 若引入补丁：必须跑一组指纹自检（`webdriver`、`plugins`、`canvas hash`、`navigator.webdriver`、`JA3`）作为验收，不能凭"装了就不检测"。

### 6.3 拟人化参数（初始值 + 可标定，不是定论）

| 项 | 初始值 | 标定方式 |
|---|---|---|
| 按键间隔 | 均值 120ms，σ=35ms，截断 [55, 320] | 用真实人类录入样本拟合 |
| 标点后停顿 | 250–600ms 均匀 | 同上 |
| 手误率 | **默认 0**；仅短文本 agent 白名单开启 0.02 | 内容损坏率 <0.1% 才允许开 |
| 鼠标轨迹 | 三次贝塞尔（非二次）+ 1–2px 抖动 | 轨迹曲率/加速度分布对比真实录制 |
| 点击前 | 悬停 200–500ms + overshoot 8–20px 再回拉 | — |
| 滚动 | 分 3–6 段，段间 80–400ms | — |

**输入方式选择（修正 v3.0 的 "放弃 page.fill()"）**：

| 场景 | 方式 |
|---|---|
| 短文本/搜索/评论（≤120 字符） | `locator.pressSequentially(text, {delay})`，产生 keydown/keypress/input/keyup 完整序列 |
| 长文本（正文/文章） | 合成 paste：`clipboard` 写入 → 焦点 → `Mod+V`（或 CDP `Input.insertText`）；前后各补 1–2 次真实按键 |
| 禁止 | `page.fill()`（只派发 input 事件，无按键序列） |

**内容完整性守卫（发布类必做）**：提交前读回编辑区文本，与目标文本做 `normalize + diff`；不一致 → 不提交，`step_failed(human_required)`。开启手误率时，修正逻辑必须持续到 diff 为 0。

### 6.4 视觉介入与坐标系（v3.0 未定义的必错点）

`page.screenshot()` 的像素与 `mouse.click(x,y)` 的 CSS 像素存在 `devicePixelRatio` 差异，`fullPage` 截图还带滚动偏移——直接回填坐标必错。

```
step1  viewport 截图（不做 fullPage）
step2  VLM 输出优先为 {role, name} 或 selector；像素坐标仅作 fallback
step3  fallback 时: css_x = px_x / devicePixelRatio
                    css_y = (px_y / devicePixelRatio) - scrollY
step4  点击前校验: elementFromPoint(css_x, css_y) 命中目标可见性 → 否则拒绝
step5  截图发 Telegram: 1280px 宽 JPEG(quality 80) + 原始 artifact 路径
```

**人类介入回路**：`blocked_human` 时冻结 journal（已完成的 step 与 output_ref 都在），人回复"点稍后提醒"→ `vision_ground` 输出定位 → 命中则续跑并**先** `answerCallbackQuery`；未命中 → 再问一次，最多 2 轮，之后转人工浏览器（模式 A 下可接管）。

### 6.5 语音链路（修正"毫秒级"）

| 项 | 值 |
|---|---|
| 引擎 | whisper.cpp，`ggml-base` 或 `small`（按站点噪音选） |
| 目标性能 | RTF < 0.2（10s 音频 p95 < 2s），不是"毫秒级" |
| 前置 | Silero VAD 门控；静音/纯噪 → 直接拒绝，不进模型（防幻觉） |
| 后置 | 转录文本 + 当前绝对时间（ISO 8601）+ 日历 TZ → `task_route` |
| 失败 | 空文本/置信度极低 → Telegram 回"没听清，请重发或打字" |

### 6.6 资源预算（M2 8 核 / 16GB 假设）

| 组件 | 常驻 | 峰值 |
|---|---|---|
| SQLite + control loop | ~120MB | ~200MB |
| SLM 7B Q4（`task_*`, `dom_summarize`） | 4.2–4.8GB | — |
| Whisper small | ~0.5GB（按需） | ~0.7GB |
| Chromium（1 profile） | 0.6GB | 1.5GB |
| VLM（按需，用完释放） | 0 | 3–4GB |
| 合计 | ~6GB | ~9GB，需 16GB 机型 |

**8GB 机型降级方案**（必须写进 README）：SLM 换 3B（Q4 ≈ 2GB）、禁用本地 VLM（视觉介入改走云 API）、Whisper 用 `base`、浏览器并发严格 =1。

**DOM 交给 SLM 前必须裁剪**：先做 DOM 剪枝（去 script/style/隐藏节点、保留语义节点、按视口分块），目标 ≤2k token。整页 DOM（常见 8k–30k token）直接送 7B 会让单页耗时到几十秒，链路不可用。

---

## 7. 策略与授权（v3.0 完全缺失）

`policy` 是独立模块，输入 `(actor, action, target, payload)`，输出 `allow | reject(reason) | require_approval`。判定顺序：

1. **actor 白名单**：`actor_id ∈ allowed_users` 且来源 chat/calendar 均在白名单。
2. **事件归属**：日历事件 `organizer.email == 本人` 且日历是 `AgentCal`；否则 reject。
3. **签名**：含 C 区 PARAMS 的事件必须验签通过且未过期；无 PARAMS 的人类意图跳过验签，但仅在 `organizer == 本人` 时放行。
4. **目标域白名单**：`target.domain ∈ allowed_domains`。**每 agent 一份**（`[agents.<name>] domains`），未配置的 agent 才退回落 `[policy].allowed_domains`；空名单即拒绝（fail closed）。实现注记：`executor` 把该 agent 的名单放进 `settings.allowed_domains` 交给 worker，agent 在执行前自检；`x-publisher` 目前只校验入口 `compose_url`，点击后跳转的每步 URL 尚未逐步校验。

   **永久信任与撤销（实测新增）**：浏览器 agent 遇到未授权域名时不再硬拒，而是 `blocked_human(reason=new_domain:<host>)`；Telegram 批准后写入 `trusted_domains`（永久、source=approval）。有效名单 = 配置种子 ∪ 运行时信任；用 `/domains`、`agentcal domains` 审计，用 `/revoke <host>`、`agentcal revoke-domain` 撤销（撤销只删运行时条目；配置里的种子需改配置）。
5. **风险分级**：
   - `low`（读取、抓取、草稿）→ allow
   - `medium`（发布、提交、删除单条）→ 首次 require_approval，白名单后可 allow
   - `high`（支付、删号、改权限、批量 >N）→ 永远 require_approval
6. **速率闸**：每域名每分钟动作上限（默认 6，可 diy 到 agent 级），超限直接 reject 并记 `rate_limited`。

**使用边界（写进 README，非道德声明而是运行约束）**：只对自有账号、且目标站点 ToS 允许自动化的域名操作；`allowed_domains` 是显式清单，默认空。平台不做目标发现、不扫描非授权域。

---

## 8. 失败矩阵（每个外部调用点都要有行）

| 故障点 | 检测 | 处理 | 级别 |
|---|---|---|---|
| Calendar 429/5xx | HTTP 状态 | 退避 5s→60s，≤5 次；连续 10 轮告警 | warn |
| Calendar 410 (syncToken) | 状态码 | 全量重建 + §4.4 对账 | warn |
| Calendar 401 | 状态码 | 刷新 token；失败 → degraded（停 poller，保留 run）+ Telegram 重授权卡片；恢复后全量重建 cursor | error |
| Calendar 写入 412 | If-Match | 重读重合并，≤3 次；仍败 → 脏投影队列 | warn |
| Telegram 429 | `retry_after` | 严格按值退避；合并器降低频率 | warn |
| Telegram 409/网络断 | 长轮询异常 | 重连退避 1s→30s | warn |
| callback 超时 | 10s 无 ack | 记录；消息保留重试按钮 | info |
| LLM schema 失败 | 本地校验 | 修复重试 1 次 → 降级模型 → step transient | warn |
| LLM 成本超阈值 | 当日累计 ≥ $10 | 当日一条聚合告警（附 top-3 消耗任务），**不中止任何 run** | warn |
| 浏览器 navigate 超时 | 30s | 重试 1 次；仍败 → failed | warn |
| 验证码/2FA | 检测器 | `blocked_human`，不发重试按钮（无意义） | error |
| 会话失效（跳登录） | URL/selector 断言 | `blocked_human(session_expired)` | error |
| 发布后状态未知 | 进程死亡 + publish step | `blocked_human(unknown_side_effect)`，禁止自动重放 | critical |
| 磁盘满 | 写 artifact 前检查 | 清理最旧 artifact；仍不足 → 暂停排队 | error |
| Keychain 锁定 | 启动读取失败 | degraded，只读模式；不接受新任务 | error |

---

## 9. 观测与验收

### 9.1 指标（每 60s 落一行 JSON，`metrics/<date>.jsonl`）

```
task_success_rate_1h, task_p95_e2e_ms, runs_orphaned,
captcha_hit_rate, human_intervention_rate,
calendar_write_fail_rate, telegram_429_count,
llm_cost_usd_1h, llm_cost_usd_today, llm_schema_fail_rate,
artifact_disk_bytes
```

告警阈值（写进配置）：成功率 <80%（1h 且样本 ≥5）、orphan >0、日历写失败 >3 次/10min、**当日累计成本 ≥ $10（每天一条，不中止任务）**。

### 9.2 验收用例（可执行，缺一不算完成）

| 编号 | 场景 | 通过标准 |
|---|---|---|
| T1 | 同一日历事件连续投递 3 次 | 只产生 1 个 run，其余被 `idem_key` 拒绝 |
| T2 | 执行到 step 3 时 `SIGKILL` | 90s 后 lease 过期 → orphan → attempt+1；无重复副作用 |
| T3 | 人类改 A 区同时投影器写 B 区 | 两侧内容都存活；冲突记 `projection_conflict` |
| T4 | 篡改 C 区 payload 一个字节 | 拒绝执行，task → `blocked_human(unsigned_payload)` |
| T5 | 10 个任务同时状态变更 | 无 Telegram 429；日历写无 412 死循环 |
| T6 | 合盖 3 小时后唤醒 | missed 策略生效；无重复执行 |
| T7 | 目标域不在白名单 | `rejected(policy)`，无浏览器启动 |
| T8 | 7B 模型输出非法 JSON | 修复重试成功后继续；两次失败则按 route 降级 |
| T9 | 单 profile 两个任务并发 | 第二个排队，不出现 SingletonLock 崩溃 |
| T10 | 群内非白名单用户发 `/task` | 静默丢弃 + 审计日志，无任务产生 |

---

## 10. 分期与退出条件

| 里程碑 | 内容 | 退出条件 |
|---|---|---|
| **M0 骨架**（约 1 周） | DB + 迁移 + FSM + 日历读写 + 投影（仅 colorId/日志）+ supervisor/launchd | T1/T2/T3/T4/T6 通过；手动插入一条任务可跑通 pending→success |
| **M1 执行**（1–2 周） | Executor 子进程 + profile 锁 + 首个真实 agent：内部巡检 `security-scan`（目标域 `internal.example`）+ Telegram 干预 + policy gate | T5/T7/T9/T10 通过；连续 20 次任务成功率 ≥90% |
| **M2 语音与 SLM**（约 1 周） | whisper.cpp + VAD + 两级分类 + grammar + 置信度校准 | T8 通过；100 条标注集上 `t_auto` 误判率 <5% |
| **M3 拟人化与视觉兜底**（1–2 周） | Humanizer + a11y grounding + 像素兜底 + 截图回归 | 目标站点 7 天内无验证码触发（或触发率 <5%）；视觉定位命中率 ≥90% |
| **M4 外扩与协作**（未排期） | MCP 第三方 server + 多 agent handoff 草案 | 见下方契约草案；不在 MVP |

**M4 协作契约草案**（只作方向，避免再次出现"标题无对应设计"）：

```
Handoff{ from_agent, to_agent, task_id, parent_step_id,
         input_ref, expected_output_schema, deadline }
规则: 子任务拥有独立 run；父 run 在 handoff 处挂起；
      子 run 失败不回滚父 run，只把 failure 作为父的输入；
      深度上限 3，总预算上限按 task 记账。
```

---

## 11. 工程形态

```
agentcal/
├─ pyproject.toml              # Python 3.12, uv
├─ src/agentcal/
│  ├─ channels/               # calendar_poll.py calendar_proj.py telegram_bot.py stt.py
│  ├─ core/                   # models.py db.py migrate.py fsm.py policy.py idempotency.py reconcile.py
│  ├─ llm/                    # router.py schemas/*.json calibrate.py
│  ├─ exec_/                  # worker.py rpc.py steps/*.py
│  │  └─ browser/             # profiles.py humanizer.py grounding.py detect.py
│  ├─ mcp/                    # host.py lock.py
│  └─ obs/                    # metrics.py logging.py
├─ agents/                    # security-scan.toml (M1), x-publisher.toml (v1.1)
├─ config/agentcal.toml
├─ ops/launchd/com.agentcal.supervisor.plist
├─ artifacts/                 # 运行产物（gitignore）
├─ sandbox/                   # fs 工具唯一可写区
├─ tests/{unit,integration,fixtures}/
└─ docs/adr/
```

**技术选型**：Python 3.12 + `uv`；`playwright`（含 CDP attach）、`litellm`、`httpx`、`pynacl`、`pydantic`（schema 校验）、`vaderSentiment` 不需要。存储只用 SQLite（`sqlite3` + 手写迁移），不引 ORM——表结构固定、查询简单，ORM 反而是负担。

**配置样例**：

```toml
[core]
calendar_id = "agentcal@group.calendar.google.com"
poll_interval_s = 60
missed_policy = "run_late"      # run_late | ask
lease_ttl_s = 90
max_attempts = 3

[policy]
allowed_users = [123456789]
allowed_chats = [-1001234567890]
allowed_domains = ["internal.example"]   # M1 只开内部域；x.com 待 v1.1
max_actions_per_min_per_domain = 6

[browser]
mode = "cdp"                    # cdp | persistent
cdp_endpoint = "http://127.0.0.1:9222"
profile_dir = "profiles/main"

[llm]
proxy = "http://127.0.0.1:4000"
t_auto = 0.90
t_ask = 0.60
daily_cost_alert_usd = 10        # 超阈值只告警，不中止任务
```

**测试策略**：单测覆盖 FSM 表（每行一个用例）、幂等键、签名验签、预算守卫；集成测试用 fake calendar（本地 HTTP stub 模拟 410/412/429）、fake Telegram、录制的 DOM fixture；浏览器层用 `test_target.html` 本地页面，不碰真站点（真站点只做人工验收）。

---

## 12. 与 v3.0 的差异表（评审闭环）

| # | v3.0 条目 | 问题 | v4.0 决定 |
|---|---|---|---|
| 1 | 日历作为任务状态源 | 无事务保证、重复执行、永久 running | SQLite 为真相源，日历是投影；§2 原则 1 |
| 2 | description "Base64 防篡改" | Base64 不是签名 | HMAC 信封 + `If-Match` + 三区合并；§5.2 |
| 3 | 无授权模型 | 群里任何人可下指令 | actor 白名单 + 组织者校验 + 签名；§7 |
| 4 | 参数明文进云端日历 | 与 local-first 冲突 | 只上云 `task_id + 摘要 + 签名`，参数进本地加密库；§3/§5.2 |
| 5 | "多智能体协作" | 无任何协作设计 | 收缩为单 agent 执行，协作留 M4 契约草案；§0/§10 |
| 6 | 颜色状态机用 emoji/颜色词 | 无 colorId、默认色不可靠 | 显式 9/5/6/10/11/8 映射表；§5.3 |
| 7 | `-15~+30` 分钟抖动 | 与"等待时间到达"矛盾，动机错位 | 改 0~90s 错峰，不改事件时间；§5.4 |
| 8 | "每分钟轮询" | 缺 syncToken/410/recurring/睡眠 | 补全轮询协议与对账；§5.1/§4.4 |
| 9 | description 无长度约束 | 8192 上限会截断 | 三区预算 + 写前守卫；§5.2 |
| 10 | "System Prompt 约束 JSON" | 不可靠 | grammar + schema 校验 + 降级；§5.6 |
| 11 | 单阈值 0.9 混判私人日程 | 逻辑打结 | 两级分类 + 三档 + 风险强制审批；§5.6 |
| 12 | Whisper "毫秒级" | 事实错误 | RTF<0.2 指标 + VAD；§6.5 |
| 13 | 引入 Google Tasks | 第二状态源 | 删除（如需展示，v1.1 做单向镜像） |
| 14 | "放弃 page.fill()" | 未给正确 API，手误率危及内容 | `pressSequentially` / paste 分工 + diff 守卫；§6.3 |
| 15 | 视觉回传坐标 | fullPage/DPR 未定义，必点偏 | a11y 优先 + DPR 换算 + 命中校验；§6.4 |
| 16 | User Data Dir 当"指纹伪装" | 概念错，与 stealth 补丁冲突 | CDP attach 首选；补丁需版本复核 + 指纹自检；§6.2 |
| 17 | MCP 承载 agent 热加载 | 协议误用，缺进程治理 | MCP 只管工具；agent 用 TOML 定义；§5.7/§6.1 |
| 18 | Telegram 无约束说明 | callback 64B、限流会踩坑 | 短码编码 + 合并器 + ack 规则；§5.5 |
| 19 | 无并发/资源预算 | 单 profile + 内存溢出 | profile 锁 + 资源表 + 8GB 降级；§6.2/§6.6 |
| 20 | 无可观测/验收 | "极佳可观察性"是形容词 | 指标 + 10 条验收用例；§9 |
| 21 | 无使用边界 | 目标域无约束 | `allowed_domains` 默认空 + 速率闸；§7 |
| 22 | 术语错用 | "无头 UI""复数级任务"等 | 全文改为"任务 UI / 并发任务" |

---

## 13. 决策记录（2026-11-17 拍板）

来源：用户在评审会话中的明确答复；7 项已全部拍板（D2 于开发启动时确认接受模式 A）。

| # | 决策 | 落地位置 | 备注 |
|---|---|---|---|
| D1 | 长期使用的**个人 Gmail**账户（无 Workspace Internal 路径）：开发期接受 Testing 态 7 天失效风险，把重新授权做成一条命令；上线前再评估转 Production | §5.1 授权与重新授权、§8 401 行 | 7 天失效是社区/文档口径，**实现前按官方文档复核**；不复核也不阻塞，因为实现按“随时可能失效”设计 |
| D2 | **接受模式 A**：本机 Chrome 常驻 + 专用 profile 走 CDP attach；不引入 stealth 补丁 | §6.2、`docs/adr/0003-cdp-vs-persistent.md` | 若日后需要模式 B，再启动 M3 的补丁评估与指纹自检 |
| D3 | **B**：支持 Google 生成的 recurring instance；整系列创建/编辑 v1.1 | §5.1 重复事件、§4.3 幂等键 | 与 v4.0 原默认一致 |
| D4 | 首个真实 agent = **内部巡检** `security-scan`，目标域 `internal.example` | §10 M1、§11 agents/、§11 config | 不涉及第三方 ToS，验收可重复；x-publisher 顺延 v1.1 |
| D5 | **已关闭**：本机实测 M2 / 16GB / Chrome 已装，无需降级 | §6.6 | 换 8GB 机器时按 §6.6 降级表执行 |
| D6 | **A**：单 profile；多身份隔离 v1.1 再评估 | §0 能力收缩、§6.2 | 与 v4.0 原默认一致 |
| D7 | 单任务**不设成本上限**；当日累计 ≥ $10 发一条聚合告警，**不中止任何 run** | §8、§9.1、§11 `[llm]` | 告警去重：同日只发一次，`/status` 可查明细与 top-3 消耗任务 |

**仍需核实的事实项（不是决策，实现时按官方文档确认）**：① Testing 态 refresh token 的实际有效期；② stealth 补丁与 Playwright 版本的当前兼容性（仅 D2 被否决时才需要）；③ Calendar/Telegram 的配额数值随 API 版本变动，以官方为准；④ OAuth 转 Production 的验证材料要求。

---

## 14. 下一步（可直接拆分的工作包）

1. `docs/adr/0001-sqlite-truth-source.md`、`0002-description-envelope.md`、`0003-cdp-vs-persistent.md`、`0004-stealth-patch.md`（把 §2/§5.2/§6.2 的取舍写成 ADR，评审留痕）。
2. M0 工作包 6 个：迁移框架、FSM 表实现 + 单测、日历 poller（含 410/429）、投影器（含 412 合并）、supervisor/launchd、验收脚本 T1–T4/T6。
3. 每完成一个工作包，跑对应验收用例并把结果写进 `docs/m0-report.md`；用例不通过不算完成。

---

## 15. 实现状态（MVP，2026-11-17）

代码在 `AgentCal/`，`PYTHONPATH=src:. python3 -m unittest discover -s tests -t .` → **91 tests OK**（含 ResourceWarning 当错误跑的验证）。

| 里程碑 | 交付 | 对应模块 | 验收 |
|---|---|---|---|
| M0 | 真相源、FSM、租约、对账、日历 poller、投影、签名信封 | `db.py` `fsm.py` `poller.py` `projector.py` `envelope.py` | T1–T4、T6 |
| M1 | 子进程执行器、profile 锁、速率闸、`security-scan`、Telegram 干预与通知、OAuth 登录与自动刷新 | `runner/` `browser/profile.py` `agents/` `telegram.py` `notifier.py` `auth.py` | T5/T7/T9/T10 + 授权流用例 |
| M2 | 两级分类 + schema 校验 + 自一致性置信度 + 校准工具 + 语音链路 | `llm/router.py` `llm/calibrate.py` `voice.py` | T8 + 校准用例 |
| M3 | 拟人化数学、a11y 优先定位、DPR/滚动换算、VLM 定位、Playwright 适配与 `x-publisher` | `browser/humanizer.py` `grounding.py` `vision.py` `driver.py` `agents/x_publisher.py` | 单元用例 + 假页驱动 + 内容守卫 |

实现期内对设计的三处修正（已回写到上文）：
1. run 增加 `blocked` 状态（§4.1/§4.2）：否则等待人工的任务会因租约过期被误判为孤儿。
2. transient 失败改为“关闭 run → task 回 pending + 退避”（§4.2）：原表述与唯一活跃 run 约束冲突。
3. 无 PARAMS 的人类事件放行（§5.2/§7）：签名只管系统参数，人类自然语言入口走组织者校验 + 路由。

尚未交付（不影响上述验收，但别当成已完成）：
- 真机验收：目标站点验证码触发率、7 天连续运行成功率，以及第三方站点的真实发布——这些只能在部署后测；驱动与 `x-publisher` 目前只用假页验证了逻辑。
- 多账号 profile（D6 明确为 v1.1）。
- `x-publisher` 的站点选择器是初始值，目标站点 DOM 变动后需要重新标定。
