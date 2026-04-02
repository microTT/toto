# 1. 目标与边界

## 1.1 目标

1. 自动加载：

   * 会话开始 / 继续时，加载 **全局长期记忆** + **本地短期记忆**。
2. 自动总结：

   * 在每轮结束后评估是否需要做记忆总结。
   * 真正的总结由独立 worker 用 `codex exec` 完成，不在 hook 内直接做。
3. 可修订：

   * 新总结可以新增、更新、废弃、提升/降级旧记忆。
4. 可搜索：

   * 本地旧记忆只通过 MCP 服务查询。
5. 可并发：

   * 多个 Codex session 同时跑时，不能互相污染或重复写入。

## 1.2 非目标

1. 不追求“把完整历史自动塞回上下文”。
2. 不让普通主 agent 在日常 coding 流程中随意直接改记忆。
3. 不把 hook 当长任务执行器。
4. 不把记忆文件当 transcript 副本。

---

# 2. 总体架构

```text
Codex
  ├─ SessionStart hook
  │    └─ memory-hook: 预热 memory snapshot cache（不做真实注入）
  ├─ UserPromptSubmit hook
  │    └─ memory-hook: 读取/构建 snapshot，并将长期+短期记忆注入本 turn
  └─ Stop hook
       └─ memory-hook: 仅写入“summary candidate job”到队列

memoryd（常驻）
  ├─ 轮询队列 / 维护状态
  ├─ 触发 codex exec summarizer（hooks 关闭、只读、ephemeral）
  ├─ 拿到结构化 patch plan
  ├─ deterministic patch applier 落盘到 Markdown
  ├─ 触发 embedding / BM25 索引更新
  └─ 维护 archive / GC / 重试 / 审计日志

MCP memory server
  ├─ memory.search_old      # 旧记忆检索，给主 agent 自动调用
  ├─ memory.get             # 读记录
  ├─ memory.upsert          # 显式 remember/update 时用
  ├─ memory.delete          # 显式 forget 时用
  └─ memory.rebuild_index   # 管理用
```

关键原则只有一句：

**Hook 是轻控制面，worker 才是重数据面。**

---

# 3. 路径设计

我建议统一命名规则，但把“源文件”和“控制/索引”分开。

```text
memory_home/
  control/
    state.sqlite              # 全局状态、队列、锁、cursor、append-only event log、审计元信息
    index.sqlite              # FTS + vector metadata 索引
    jobs/
  global/
    MEMORY.md                 # 全局长期记忆，文件真相源
    audit/
      ops.jsonl
  workspace/
    recent/
      2026-04-01.md           # 本地短期记忆，today
      2026-03-31.md           # 本地短期记忆，yesterday
    archive/
      2026/03/2026-03-29.md   # 本地旧记忆，文件真相源
    runtime/
      session_<session_id>.json # snapshot cache / bootstrap state
    audit/
      ops.jsonl
```

`memory_home` 的解析规则按当前实现固定为：

1. 如果显式传入 `--memory-home` 或环境变量 `CODEX_MEMORY_HOME`，直接使用它。
2. 否则默认使用 `~/.codex/memories/<workspace_instance_id>`。
3. 如果该路径不可写，则回退到 `<workspace_root>/.memory-system`。

它保存：

* 所有 record 元数据
* chunk 文本
* embedding 向量
* BM25/FTS 索引
* repo_id / workspace_instance_id 映射
* record revision
* dedupe/fingerprint

这意味着：

* **Markdown 是真相源**
* **SQLite 是可重建索引层**
* DB 坏了可以从 Markdown 全量重建

本地 `.codex/memory/` 建议默认加入 `.git/info/exclude`，不要进 Git。

---

# 4. Repo 身份、Workspace 实例与作用域

不要直接把 `cwd` 压成一个 id 之后到处复用。这里至少拆成两个层次：

* `repo_id`：跨 clone 稳定，表示“这是同一个代码仓库/项目来源”
* `workspace_instance_id`：本机当前这个具体工作副本实例

建议：

```text
repo_id =
  if git remote.origin.url exists:
      sha256(normalize(origin_url))
  else if git root exists:
      sha256("repo:" + realpath(git_root))
  else:
      sha256("repo:" + realpath(cwd))

workspace_instance_id =
  if git root exists:
      sha256(realpath(git_root))
  else:
      sha256(realpath(cwd))
```

这样：

* 同一 repo 多个子目录 session 仍归同一个 `workspace_instance_id`
* 同一 repo 的不同 clone 共享 `repo_id`，但不会错误共用本地 recent 文件
* cross-clone 检索只有在你显式按 `repo_id` 放宽检索范围时才发生

Scope 只保留三种：

* `global_long_term`
* `local_recent`
* `local_archive`

但本地 recent 要允许一个例外：

* **pinned carry-over**：某条 local recent 记录虽然超过两天，但仍是 open/pinned，就继续被 recent loader 加载，而不是立刻降为 old。

否则长周期任务会被“日期规则”错误埋掉。

V1 的明确规则：

* `local_recent` / `local_archive` 的落盘与自动加载，按 `workspace_instance_id` 隔离
* `memory.search_old` 默认只查当前 `workspace_instance_id`
* 如果未来需要“同 repo 不同 clone 共享旧记忆”，走显式参数 `search_scope = same_repo`

---

# 5. Markdown 存储格式

## 5.1 全局长期记忆 `MEMORY.md`

```md
---
schema_version: 1
scope: global_long_term
revision: 42
updated_at: 2026-04-01T10:12:00Z
---

# Global Long-Term Memory

## Active

### g_01JABCDEF1234567890
- type: preference
- status: active
- confidence: high
- subject: package manager
- summary: Prefer pnpm in JavaScript/TypeScript projects unless the repo explicitly requires npm or yarn.
- rationale: Explicit user instruction.
- tags: [javascript, typescript, tooling, pnpm]
- source_refs: [session:019..., turn:turn_12]
- created_at: 2026-04-01T10:10:01Z
- updated_at: 2026-04-01T10:10:01Z
- supersedes: []
- scope_reason: Cross-workspace and durable.

## Superseded

### g_01JOLD...
- type: preference
- status: superseded
- superseded_by: g_01JABCDEF1234567890
- summary: Prefer npm.
```

## 5.2 本地短期 / 旧记忆文件

```md
---
schema_version: 1
scope: local_recent
repo_id: repo_abc123
workspace_instance_id: wsi_def456
workspace_root: /path/to/repo
date: 2026-04-01
revision: 7
updated_at: 2026-04-01T18:10:00Z
---

# Local Memory — 2026-04-01

## Open

### l_01JLOCAL123
- type: task_context
- status: open
- confidence: high
- subject: flaky auth tests
- summary: Failures are concentrated in auth/session snapshots on CI.
- next_use: Re-check snapshots before touching auth middleware.
- tags: [auth, ci, tests]
- source_refs: [session:019..., turn:turn_19]
- created_at: 2026-04-01T18:09:00Z
- updated_at: 2026-04-01T18:09:00Z
- pin_until: 2026-04-05T00:00:00Z
- scope_reason: Repo-specific and near-term.

## Closed

### l_01JLOCAL456
- type: failed_attempt
- status: closed
- summary: Re-running snapshots without clearing cache did not help.
```

## 5.3 记录级字段

每条记录至少有：

* `id`
* `type`
* `status`
* `confidence`
* `subject`
* `summary`
* `tags`
* `source_refs`
* `created_at`
* `updated_at`
* `scope_reason`

可选：

* `pin_until`
* `supersedes`
* `superseded_by`
* `rationale`
* `next_use`

这个格式的优点：

1. 人类可读。
2. patch applier 可稳定定位。
3. embedding 直接以 record 为基本 chunk。
4. 更新/废弃有历史关系，不会“偷偷改历史”。

## 5.4 状态模型：V1 先冻结成有限集合

不要再混用 `active / open / closed / superseded / deleted / disputed / tombstone` 这类半重叠概念。V1 先固定为 5 个 record status：

* `active`：当前仍有效、可复用的稳定事实/偏好/决策
* `open`：当前未完成的本地上下文，如 blocker / TODO / 失败路径 / 下一步
* `closed`：已完成或已失效的本地上下文，保留作短期参考
* `superseded`：已被新记录替代
* `deleted`：用户明确要求 forget 后留下的 tombstone

对应 section 也固定：

* global 文件只允许：`Active / Superseded / Deleted`
* local 文件只允许：`Open / Active / Closed / Superseded / Deleted`

状态迁移只允许：

* `create -> active | open`
* `open -> active | closed | superseded | deleted`
* `active -> superseded | deleted`
* `closed -> active | superseded | deleted`

自动加载时只看：

* global 的 `active`
* local 的 `open`
* local 的 `active`

不再在 V1 引入 `disputed`。如果以后真要加，必须补状态迁移表、loader 规则和 applier 规则。

---

# 6. Hook 设计：真正重点

## 6.0 启用前提

这套方案依赖 Codex hooks feature 已启用。不要默认假设本机一定开着。

先检查：

```bash
codex features list
```

确认 `codex_hooks` 为 `true`；如果不是，先启用：

```bash
codex features enable codex_hooks
```

这一步属于安装前提，不应隐含在后续实现里。

## 6.1 只注册一个总线 hook

不要同时在 `~/.codex/hooks.json` 和 `<repo>/.codex/hooks.json` 里放记忆逻辑。Codex 会把两个层级的 hooks 都加载，而且匹配到的 command hooks 会并发启动。记忆系统应该只在一个地方注册一个统一入口，例如 `~/.codex/hooks.json`，再由它读取 repo 内部配置。([OpenAI Developers][1])

### 推荐 hooks.json

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "~/.codex/memory/bin/memory-hook session-start",
            "timeout": 10,
            "statusMessage": "Prewarming memory"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.codex/memory/bin/memory-hook user-prompt-submit",
            "timeout": 15,
            "statusMessage": "Loading memory"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.codex/memory/bin/memory-hook stop",
            "timeout": 8,
            "statusMessage": "Recording memory candidate"
          }
        ]
      }
    ]
  }
}
```

## 6.2 authoritative load point：`UserPromptSubmit`

这是第一条强约束：

**真正注入记忆的 authoritative point 不是 `SessionStart`，而是 `UserPromptSubmit`。**

理由：

1. `SessionStart` 和 `UserPromptSubmit` 在首个 prompt 上存在同时触发的公开 bug 报告。
2. `UserPromptSubmit` 离“prompt 真正发送给模型”最近。
3. `UserPromptSubmit` 可以向 developer context 注入 additionalContext。([GitHub][4])

### `SessionStart` 应做什么

只做三件事：

1. 解析 `session_id / cwd / transcript_path`
2. 计算 `repo_id / workspace_instance_id`
3. 预热 snapshot cache 到
   `workspace/.codex/memory/runtime/session_<session_id>.json`

它**不负责真正注入**。

### `UserPromptSubmit` 应做什么

它是唯一真实 loader：

1. 检查 `session_<id>.json` 是否已有可用 snapshot
2. 若没有或过期，直接 inline 构建
3. 生成注入块
4. 每个 turn 都输出当前 snapshot 作为 `additionalContext`
5. “避免重复”只针对**重复构建 snapshot**，不针对**重复注入本 turn 的 developer context**

这里要明确区分两件事：

* `snapshot revision`：缓存命中键，用来避免每个 turn 都重新扫描文件和重排记录
* `additionalContext emission`：每次 `UserPromptSubmit` 都要重新输出给当前 prompt；它不是 session 级持久注入

### 注入块格式

```text
[MEMORY LOADED]
记忆已加载。

全局长期记忆：
- ...
- ...

工作区近期记忆：
- ...
- ...

历史工作区记忆不会自动加载。
如果用户提到更早的尝试、历史 bug、过去决策，或“之前我们做过什么”，
请使用 MCP 工具：memory.search_old
```

### 加载内容范围

只加载：

* `global/MEMORY.md` 的 `## Active`
* `recent/` 目录中：

  * 今天
  * 昨天
  * `pin_until > now` 的 carry-over 记录
  * 其中状态只能是 `Open / Active`

绝不自动加载：

* `archive/` 旧记忆
* `Superseded / Deleted / Closed`
* 原始 transcript
* 大段工具输出

### 预算建议

* global：最多 12–20 条 active 记录
* local recent：最多 15–25 条 active/open 记录
* 总注入预算：控制在一个固定上限，例如 2k–3k tokens

预算裁剪规则也必须 deterministic，建议直接冻结：

1. 先取 global `active`
2. 再取 local `open`
3. 再取 local `active`
4. 各组内部按固定优先级排序：
   * global：`confidence desc` -> `updated_at desc` -> `id asc`
   * local open：`pin_until desc` -> `updated_at desc` -> `id asc`
   * local active：`updated_at desc` -> `id asc`
5. 每条记录渲染成单行或短多行摘要，单条超过上限先截断字段，不要整体丢弃排序稳定性
6. 最后按总 token budget 截断尾部

这一步必须 deterministic，不要调用模型。

---

# 7. 记忆总结触发：第二个真正重点

## 7.1 不要把 `Stop` 当 session end

Codex 当前没有官方的 “SessionEnd” hook。`Stop` 是 **turn-scoped**，而且如果你在 `Stop` 里返回 `decision: "block"`，Codex 并不是“拒绝这轮”，而是会自动把你的 reason 当成新的 continuation prompt 继续跑下去。这个机制适合“再跑一轮检查”，不适合后台记忆维护。([OpenAI Developers][1])

所以：

**Stop hook 只能做“记录候选 + 排队”，不能做真正总结。**

## 7.2 Stop hook 的职责

Stop hook 只做轻量判断：

### 输入

* `session_id`
* `turn_id`
* `last_assistant_message`
* `cwd`
* `transcript_path`（可为空）
* 本地状态库里的 `last_summarized_cursor`
* append-only event log 中该 session 自上次总结以来的 turn delta

### 输出

无注入，无 continuation。

### 它做的事

1. 计算自上次总结以来的 delta
2. 如果不满足阈值，直接 no-op
3. 如果满足阈值，写入/更新一条 `summary job`

这里的 delta 真相源优先级也要固定：

1. transcript 中可精确切片的增量
2. state.sqlite 中 append-only event log
3. runtime cache 中的 per-turn delta 副本

不要只保存 “last user prompt / last assistant message” 两个字段。那样 worker 延迟消费时无法稳定重建多 turn 增量。

### 触发阈值

建议任何一条满足即触发：

* 用户显式说了“记住 / remember / forget / update memory”
* 新增 turn 数 ≥ 4
* transcript delta chars ≥ 1200
* 明确出现“决定 / 偏好 / 约束 / TODO / 下次继续 / 失败结论”
* 当前 recent memory revision 落后于对话状态明显较多

### 不触发的场景

* 纯闲聊
* 只有工具日志，没有可复用结论
* 明显重复之前总结过的内容
* 低置信度推测，没有证据落点

---

# 8. `codex exec` 总结器：必须由独立 worker 拉起

这是最关键的架构决定：

**不要从 hook 进程里直接 `codex exec`。**
要由 `memoryd` 这样的独立 worker，读取队列后在 hook 之外启动 `codex exec`。

理由有三条：

1. hook 本身应该是短任务；长模型调用会把 turn latency 放大。
2. nested `codex exec` 目前有公开 issue，子 exec 里的命令仍可能看到父 `CODEX_THREAD_ID`。
3. 你需要关闭 nested exec 自己的 hooks，否则会递归触发 memory hook。([GitHub][5])

## 8.1 worker 启动命令

`codex exec` 是稳定的非交互模式，支持 `--ephemeral`、`--json`、`--output-schema`、`--sandbox` 以及 `-c key=value` 覆盖；默认可以只读跑，也可把最终消息写到文件。([OpenAI Developers][6])

推荐命令：

```bash
env -u CODEX_THREAD_ID -u CODEX_SESSION_ID \
codex exec \
  --ephemeral \
  --sandbox read-only \
  --skip-git-repo-check \
  --json \
  --output-last-message /tmp/memory_patch_result.json \
  --output-schema ~/.codex/memory/control/schemas/memory_patch.schema.json \
  -c features.codex_hooks=false \
  -C "$WORKSPACE_ROOT" \
  - < /tmp/memory_patch_prompt.txt > /tmp/memory_patch_run.jsonl
```

这里的设计意图是：

* `--ephemeral`：不把 summarizer 自己的 rollout 落盘
* `--sandbox read-only`：总结器不应该直接改文件
* `--json`：保留完整审计流
* `--output-schema`：强制输出结构化 patch plan
* `--output-last-message`：拿到最终 JSON 结果
* `-c features.codex_hooks=false`：防止递归 hook

## 8.2 summarizer 输入包

worker 生成给 Codex 的 prompt，不要裸喂 transcript。输入包应该固定六段：

1. **Task brief**
   你是 memory summarizer，只能输出 JSON，不得直接改文件。
2. **Transcript delta**
   从 `last_summarized_cursor` 到当前 turn 的对话片段；如果 `transcript_path` 不可用，则改用 append-only event log 重建。
3. **Current active global memory**
4. **Current active local recent memory**
5. **Policy**
   什么能记、什么不能记、如何分类、如何处理冲突
6. **Output schema**
   严格 JSON schema

## 8.3 summarizer 输出 schema

不要让模型直接吐 Markdown。只允许它输出“patch plan”。

语义上必须按 action 区分，不能把所有 action 当成一坨宽松写法来解释；并且必须带上 base revision，避免并发时把过期 patch 直接写盘。

但当前 `codex exec --output-schema` 对 JSON schema 的约束更严格：它不接受 `oneOf` 这类判别联合写法，而且要求 object 的 `required` 覆盖全部 `properties`。因此 V1 的**传输层 schema**会保持 Codex 兼容的扁平 nullable 结构，而**服务端 validation / applier**继续按 action 语义做严格校验。

```json
{
  "decision": "noop | write",
  "reason": "why",
  "base_revisions": {
    "global_revision": 42,
    "local_recent_revision": 7
  },
  "global_ops": [
    {
      "action": "create",
      "record": {
        "type": "preference",
        "subject": "package manager",
        "summary": "Prefer pnpm ...",
        "confidence": "high",
        "tags": ["pnpm", "tooling"],
        "scope_reason": "cross-workspace and durable",
        "source_refs": ["session:019...,turn:turn_19"]
      }
    },
    {
      "action": "update",
      "target_id": "g_01J...",
      "record_patch": {
        "summary": "Prefer pnpm unless the repo explicitly requires npm or yarn.",
        "rationale": "Explicit user instruction."
      }
    },
    {
      "action": "supersede",
      "target_id": "g_01JOLD...",
      "replacement_record": {
        "type": "preference",
        "subject": "package manager",
        "summary": "Prefer pnpm ...",
        "confidence": "high",
        "tags": ["pnpm", "tooling"],
        "scope_reason": "cross-workspace and durable",
        "source_refs": ["session:019...,turn:turn_19"]
      }
    },
    {
      "action": "delete",
      "target_id": "g_01J...",
      "tombstone": {
        "reason": "explicit user forget",
        "source_refs": ["session:019...,turn:turn_21"]
      }
    },
    {
      "action": "demote",
      "target_id": "g_01JGLOBAL...",
      "replacement_record": {
        "type": "task_context",
        "subject": "auth snapshot issue",
        "summary": "This is repo-specific and should stay local.",
        "confidence": "high",
        "tags": ["auth", "ci"],
        "scope_reason": "repo-specific and near-term",
        "source_refs": ["session:019...,turn:turn_30"]
      }
    }
  ],
  "local_ops": [
    {
      "action": "create",
      "record": {
        "type": "task_context",
        "subject": "auth flaky tests",
        "summary": "Failures are concentrated ...",
        "confidence": "high",
        "tags": ["auth", "ci", "tests"],
        "scope_reason": "repo-specific and near-term",
        "pin_until": "2026-04-05T00:00:00Z"
      }
    },
    {
      "action": "pin",
      "target_id": "l_01J...",
      "pin": {
        "pin_until": "2026-04-05T00:00:00Z"
      }
    },
    {
      "action": "promote",
      "target_id": "l_01JLOCAL...",
      "replacement_record": {
        "type": "preference",
        "subject": "package manager",
        "summary": "Prefer pnpm unless the repo explicitly requires another tool.",
        "confidence": "high",
        "tags": ["pnpm", "tooling"],
        "scope_reason": "cross-workspace and durable",
        "source_refs": ["session:019...,turn:turn_31"]
      }
    }
  ],
  "needs_manual_review": false
}
```

上面的 JSON 只是“语义示例”，为了可读性省略了与当前 action 无关的 `null` 字段。实际给 `codex exec --output-schema` 的传输 schema 会要求这些字段显式存在，再由 server-side normalizer 把 `null` 清掉。

补充约束：

* `update` 只能带 `record_patch`，缺失字段表示“保持原值”，不是清空
* `supersede` 必须带 `replacement_record`
* `delete` 必须带 `tombstone.reason`
* applier 如果发现 `base_revisions` 已过期，必须拒绝写入并重排总结 job，而不是强行 merge

## 8.4 patch applier

模型输出只是一份 plan。真正落盘必须由 deterministic applier 做：

1. 校验 JSON schema
2. secret / token / credential denylist 过滤
3. 读当前 memory files + DB revision
4. 校验 `base_revisions`
5. 语义去重 / 精确匹配
6. 生成最终 file patch
7. temp file + atomic rename
8. 写 `ops.jsonl`
9. 触发 embedding refresh

其中第 4 步不能省。否则只要 queue 有滞后，older patch 就可能覆盖 newer state。

---

# 9. “什么能提取成记忆”与决策规则

## 9.1 可以提取的记忆

### 全局长期记忆候选

只允许以下几类：

1. 用户稳定偏好
   例如语言、工具、包管理器、回答风格、默认框架选择
2. 跨项目约束
   例如“不接受 Docker 方案”“只能本地化部署”
3. 长期身份事实
   例如用户名偏好、常用目录约定
4. 多次重复出现的稳定工作流
5. 明确被用户要求“长期记住”的内容

### 本地短期记忆候选

1. 当前 repo 目标 / 成功标准
2. 本地架构决策
3. 当前排查路径与失败尝试
4. 本地环境问题
5. 下一步 TODO
6. 当前分支/任务的关键上下文
7. 对 repo 有价值但大概率是近几天才会用到的事实

### 不能保存

1. secrets / token / cookie / 密钥
2. 原始工具输出
3. 聊天气氛
4. 没证据的猜测
5. 一次性路径噪声
6. 已经完整存在于 Git diff 中、且无需再抽象的内容

## 9.2 Global vs Local 的决策准则

### 归入 global_long_term，必须同时满足

* 跨 workspace 或跨任务稳定成立
* 预计 30 天后仍有价值
* 置信度高
* 明确用户指令、或多次被验证/重复出现

### 归入 local_recent，满足以下之一即可

* 明显 repo-specific
* 当前任务/分支直接相关
* 预计 2–14 天内会再用到
* 是 open blocker / next step / failed attempt / local decision

### 归入 no-op

* 没未来复用价值
* 只是当前 turn 的表述，不是事实/偏好/决策
* 低置信度
* 与已有记忆完全重复

## 9.3 一个可实现的打分规则

```text
reject if secret == true

score =
  explicit_user_instruction * 5 +
  verified_by_tool_or_file * 3 +
  repeated_across_sessions * 3 +
  future_reuse_likelihood * 3 -
  volatility * 3 -
  ambiguity * 4

if cross_workspace && durability >= 30d && score >= 8:
    global_long_term
elif workspace_specific && score >= 5:
    local_recent
else:
    noop
```

这套规则要写进 summarizer prompt，也要在 patch applier 侧做二次硬过滤。

---

# 10. “保存 / 修改以前的记忆”规则

不要粗暴覆盖旧内容。要分五种动作：

## 10.1 create

以前没有相同主题的记录，新增。

## 10.2 update

同一主题、同一事实，只是表达更清楚或字段更全。
例如原来写“用户偏好 pnpm”，现在补充“除非 repo 明确要求 npm/yarn”。

## 10.3 supersede

新信息和旧信息冲突。
例如原来是“prefer npm”，现在明确变成“prefer pnpm”。

处理方式：

* 旧记录移到 `Superseded`
* 新记录写 `supersedes: [old_id]`

## 10.4 delete / tombstone

仅在用户明确要求 forget 时允许。
不要物理删除，先 tombstone，保留审计。

## 10.5 promote / demote

### promote local -> global

当一条 local 事实：

* 在多个 workspace 出现
* 或被反复确认
* 或本来就是用户稳定偏好，只是早先误归类到 local

### demote global -> local

当你发现原来的“全局记忆”其实只是 repo-specific
处理方式不是直接改名，而是：

* global 记录 superseded
* 新建一条 local 记录承接

---

# 11. MCP 服务设计

Codex 支持本地 stdio MCP server，也支持 HTTP；MCP 里 **tools 是模型可自动调用的能力**，而 **resources 更偏应用侧提供上下文**。既然你要求“旧记忆只有这一条路查到”，那旧记忆检索必须暴露成 **tool**，不能只做 resource。([OpenAI Developers][7])

## 11.1 读工具

### `memory.search_old`

关键工具。

输入：

```json
{
  "workspace_instance_id": "optional, default current",
  "repo_id": "optional, used only with search_scope=same_repo",
  "search_scope": "current_workspace | same_repo",
  "query": "string",
  "top_k": 8
}
```

输出：

* old memory record 摘要
* snippet
* score
* record_id
* created_at
* tags

默认行为固定为：

* `search_scope = current_workspace`
* 只查 `local_archive`
* 不跨 clone、不跨 repo 自动放宽

### `memory.get`

按 id 取完整记录。

### `memory.get_context`

调试用，返回当前 global + recent snapshot。

## 11.2 写工具

这些工具不能只靠“建议”约束，必须有硬门禁。否则它们一旦注册成普通 tool，本质上就是主 agent 可调用的写能力。

V1 建议二选一，优先第一个：

1. **默认只给普通 coding session 注册 read-only memory server**
2. 把写工具放到单独的 `memory-admin` server / profile，只在明确的记忆管理任务里挂载

如果你坚持单 server，也必须要求 server 启动时显式 `allow_writes=true`；默认拒绝所有写操作。

### `memory.upsert`

仅在用户明确说 remember/update/forget 或管理任务时允许，并且服务端要验证“当前 server 处于 write-enabled 模式”。仅靠 AGENTS.md 约束不算安全边界。

### `memory.delete`

soft delete / tombstone。也必须走同样的 write-enabled 门禁。

### `memory.rebuild_index`

管理命令。

## 11.3 AGENTS.md 配套策略

Codex 会在 run 启动时构建 AGENTS 指令链，所以“何时主动查询旧记忆”的策略应该放在 AGENTS.md，而不是塞进记忆文件本身。([OpenAI Developers][8])

建议加入：

```md
- Older workspace memory is not auto-loaded.
- If the user refers to earlier attempts, previous fixes, old decisions, or “what we did before”,
  call MCP tool `memory.search_old` before assuming history.
- Do not call memory mutation tools unless the user explicitly asks to remember, forget, or update memory.
```

---

# 12. 向量化与索引

Qwen3 Embedding 系列当前公开信息显示：它有 0.6B / 4B / 8B 三个 embedding 模型，32K 序列长度，支持 100+ 语言、代码检索，并支持 instruction-aware embeddings。对你的场景，这很适合“中文 + 英文 + 代码片段”的混合记忆。([GitHub][9])

## 12.1 模型建议

* 当前默认部署：`text-embedding-v4`
* 如果走本地 Hugging Face 模型：`Qwen/Qwen3-Embedding-4B`
* 低资源：`Qwen/Qwen3-Embedding-0.6B`
* 高质量：`Qwen/Qwen3-Embedding-8B`

## 12.2 文档/查询 instruction

因为 Qwen3 支持 instruction-aware，建议区分 doc / query 两种 instruction：

### 文档 embedding

```text
为后续检索表示这条开发工作区记忆记录。 Represent this developer-workspace memory record for future retrieval.
```

### 查询 embedding

```text
为检索开发工作区归档记忆表示这个查询。 Represent this query for retrieving archived developer-workspace memory.
```

## 12.3 配置方式

Qwen embedding 的运行时配置按当前实现放在 `memory/.env`，并由 Git 忽略。推荐字段：

```dotenv
CODEX_MEMORY_EMBEDDING_PROVIDER=auto
CODEX_MEMORY_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
CODEX_MEMORY_EMBEDDING_API_KEY=
CODEX_MEMORY_EMBEDDING_MODEL=text-embedding-v4
CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE=openai
CODEX_MEMORY_EMBEDDING_DIMENSIONS=1024
CODEX_MEMORY_EMBEDDING_MAX_LENGTH=8192
```

规则：

* `.env` 是默认配置来源。
* 进程环境变量覆盖 `.env`。
* `CODEX_MEMORY_EMBEDDING_ENDPOINT` 继续兼容旧字段，但新的主字段是 `CODEX_MEMORY_EMBEDDING_BASE_URL`。
* 当前默认配置只需要补 `CODEX_MEMORY_EMBEDDING_API_KEY`；若使用国际地域，再把 `CODEX_MEMORY_EMBEDDING_BASE_URL` 改为 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`。
* `CODEX_MEMORY_EMBEDDING_DIMENSIONS` 当前默认是 `1024`，对应 `text-embedding-v4` 的默认输出维度；如果调整该值，应该重建索引。
* `CODEX_MEMORY_EMBEDDING_PROVIDER=auto` 时，只有远程配置完整时才启用远程 embedding；否则自动回退 lexical，避免因为空 `API_KEY` 让主链路失效。

## 12.4 chunk 策略

不要按固定 500 token 切 transcript。
**以 memory record 为主 chunk 单位**：

* 一个 record = 一个主 chunk
* 如果 details 太长，再拆副 chunk
* chunk 全都带 `record_id`

## 12.4 检索策略

默认：

1. filter `scope = local_archive`
2. filter `workspace_instance_id = current`（只有显式 `search_scope=same_repo` 时才放宽到 `repo_id = current_repo_id`）
3. vector top 40
4. BM25 top 20
5. score fusion
6. 返回 top 6–8

global / recent 也索引，但默认不通过 `search_old` 暴露给主 agent。

---

# 13. 并发与边界情况

## 13.1 多个 session 同时访问同一 repo

### 规则

* 读：并发无锁
* 写：按 `workspace_instance_id` 串行
* global 写：全局独占锁
* queue：SQLite WAL + unique idempotency key

### summary job 去重键

```text
job_key = sha256(session_id + transcript_path + start_cursor + end_cursor + prompt_version)
```

同一范围的 job 只能有一条。

## 13.2 hook 并发启动

因为同一事件的 matching hooks 会并发启动，hook 入口本身必须幂等：同一个 `session_id + turn_id + event_name` 最多执行一次有效副作用。([OpenAI Developers][1])

## 13.3 首个 prompt 的 race

`SessionStart` 只预热，`UserPromptSubmit` 才真实注入。
这样即使二者并发，也不会影响 correctness。([GitHub][4])

## 13.4 长命令缺失 `PostToolUse`

不要让 `PostToolUse` 参与“是否生成记忆”的核心判定；它最多做辅助遥测。当前公开 issue 已有长 running shell completion 丢失 post hook 的报告。([GitHub][10])

## 13.5 nested `codex exec` 污染

worker 启动 summarizer 前：

* `unset CODEX_THREAD_ID`
* `unset CODEX_SESSION_ID`
* `-c features.codex_hooks=false`

并且 summarizer 必须由 hook 外 worker 拉起，不在 hook 进程里直接 exec。([GitHub][5])

## 13.6 subagent 问题

当前公开 issue 表明，hook 输入里还没有稳定的 `agent_id / agent_type` 来区分 main agent 与 subagent。V1 不应依赖“主/子 agent 区分”做核心逻辑。我的建议是：

* V1：**只允许显式用户意图**触发高置信度 memory write
* subagent 触发的 Stop job 默认低优先级，除非 transcript delta 中有明确用户级事实
* 未来如果 Codex 提供 `agent_id / agent_type`，再把“只对 main agent 写 memory”做成强规则 ([GitHub][11])

## 13.7 transcript_path 为空

hook 输入里的 `transcript_path` 可能为 null，所以状态库里要额外保留 append-only event log，而不只是“最后一条消息”：

* session_id
* turn_id
* event_time
* user_message_delta
* assistant_message_delta
* summary_cursor_before
* summary_cursor_after

这样总结器不会依赖单一文件路径，也不会在 worker 延迟消费时丢失多 turn 增量。([OpenAI Developers][1])

## 13.8 超过两天但仍重要

有 `pin_until` 的 local recent 继续 recent load，不自动 archive。
否则你会把长期排查中的 open issue 提前丢到 old memory。

---

# 14. 我建议的实现顺序

## Phase 1：先做 deterministic 控制面

1. path resolver
2. state.sqlite
3. repo_id / workspace_instance_id model
4. status enum + section mapping
5. hooks 总线入口
6. session cache
7. recent/global loader
8. deterministic selection + token budget truncation
9. queue + idle debounce

## Phase 2：再做 codex exec summarizer

1. prompt packer
2. JSON schema
3. append-only event log reader
4. exec worker
5. patch applier with base revision check
6. audit log
7. rollback / retry

## Phase 3：最后做向量化与 MCP

1. Qwen embedding adapter
2. SQLite FTS5 + vector index
3. `memory.search_old`
4. read-only MCP registration
5. gated `memory-admin` write tools
6. AGENTS.md policy

---

# 15. 最后给你的明确建议

如果你让我只保留三条最重要的工程原则，就是这三条：

1. **真实加载点放在 `UserPromptSubmit`，`SessionStart` 只预热。**
2. **真正的记忆总结由独立 worker 用 `codex exec --output-schema` 做，hook 只排队。**
3. **模型只输出结构化 patch plan，真正写 Markdown 的必须是 deterministic applier。**

这三条定住了，你的系统效果和稳定性会比“hook 里直接总结 + 直接改文件”高一个量级。

在这三条之外，V1 还要补两个冻结项，否则后面还是会返工：

* **`repo_id` / `workspace_instance_id` 双层身份模型**
* **record status enum + deterministic budget selection**

下一步最有价值的动作，是先把 **`state.sqlite` 表结构 + `memory-hook` 输入输出契约 + `memory_patch.schema.json` + `record status enum`** 四个接口先冻结。

[1]: https://developers.openai.com/codex/hooks/ "Hooks – Codex | OpenAI Developers"
[2]: https://docs.openclaw.ai/concepts/memory "Memory Overview - OpenClaw"
[3]: https://docs.openclaw.ai/concepts/memory-search "https://docs.openclaw.ai/concepts/memory-search"
[4]: https://github.com/openai/codex/issues/15266 "UserPromptSubmit and SessionStart hooks fire simultaneously on first prompt · Issue #15266 · openai/codex · GitHub"
[5]: https://github.com/openai/codex/issues/15527 "Nested codex exec commands inherit parent CODEX_THREAD_ID instead of nested thread id · Issue #15527 · openai/codex · GitHub"
[6]: https://developers.openai.com/codex/noninteractive/ "https://developers.openai.com/codex/noninteractive/"
[7]: https://developers.openai.com/codex/mcp/ "Model Context Protocol – Codex | OpenAI Developers"
[8]: https://developers.openai.com/codex/guides/agents-md/ "Custom instructions with AGENTS.md – Codex | OpenAI Developers"
[9]: https://github.com/QwenLM/Qwen3-Embedding "GitHub - QwenLM/Qwen3-Embedding · GitHub"
[10]: https://github.com/openai/codex/issues/16246 "Hooks: PostToolUse is missing for tools that complete via exec session / polling path · Issue #16246 · openai/codex · GitHub"
[11]: https://github.com/openai/codex/issues/16226 "https://github.com/openai/codex/issues/16226"
