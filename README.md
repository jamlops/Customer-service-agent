# 客服 AI Agent 系统

一个可运行、可扩展的智能客服系统骨架：**Agent 调度引擎 + 多工具编排 + 上下路由 + 三层对话记忆**，
支撑连续多轮交互。核心链路零第三方依赖（纯标准库），离线即可跑通完整演示与测试；
需要真实模型时切换到任意 OpenAI 兼容服务即可。

## 快速开始

```bash
python main.py                     # 运行内置 6 轮多轮场景演示（离线、确定性，默认 pipeline 模式）
python main.py --mode llm          # LLM 全权语义路由 + 专家 Agent
python main.py --mode orchestrator # function calling 多 Agent 协作（总控 Agent 统一调度）
python main.py --chat              # 交互式对话
python main.py --chat --session my-session-id
python main.py --llm openai        # 接入真实 LLM（环境变量 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL）
python -m unittest discover -s tests   # 21 个单元测试
```

可选 Web API：

```bash
pip install fastapi uvicorn
uvicorn app.api.server:app --port 8000
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" \
     -d '{"message": "我想退款", "session_id": "s1"}'
```

## Web 客服工作台

项目内置同源网页 UI，无需 Node.js 或单独的前端服务：

```bash
copy .env.example .env
# 在 .env 中填写 DEEPSEEK_API_KEY
pip install -r requirements.txt
uvicorn app.api.server:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。界面提供本地会话列表、历史恢复、快捷问题、
响应式移动布局和执行详情面板，并可在发送前选择以下内部预设 Agent：

| 预设 | 服务端行为 |
|---|---|
| 智能调度 | 沿用 `pipeline` / `llm` / `orchestrator` 当前模式自动路由 |
| 订单售后 | 本轮直达 `OrderAgent` |
| 政策顾问 | 本轮直达 `FAQAgent` |
| 人工服务 | 本轮直达 `HumanAgent` 并创建工单 |

DeepSeek 默认配置为 `https://api.deepseek.com`、`deepseek-v4-flash`、thinking enabled、
reasoning effort high。API Key 只从 `DEEPSEEK_API_KEY`（或兼容的 `LLM_API_KEY`）读取，
本地 `.env` 已被 Git 忽略，服务端的 `/health` 仅返回是否已配置，不返回密钥内容。

### 前台与人工客服后台

- 客户前台：`http://127.0.0.1:8000/`
- 客服后台：`http://127.0.0.1:8000/admin`

转人工后会创建持久化工单并进入 `waiting` 状态。此时 DeepSeek 读取短期对话、长期摘要和
已知槽位，作为“排队托管 Agent”继续回应，但会明确保持 AI 身份，不承诺接通时间，也不编造
订单事实。后台客服点击“接单”后工单变为 `active`，客户新消息只进入人工会话，不再触发模型；
后台回复通过前台轮询自动出现。客服点击“结束会话”后状态变为 `closed`，后续消息重新进入
智能 Agent 路由。

这里有两层兜底：业务路由先由确定性规则和内部 Agent 处理，只有长尾问法、FAQ 答案组织、
摘要压缩及人工排队托管调用 DeepSeek；DeepSeek 遇到网络、鉴权或限流错误时，
`ResilientLLM` 自动退回本地 `MockLLM`，保证服务仍有基础响应。

### 客服后台增强能力

后台支持工单全文搜索、服务数据面板、客户备注，以及高风险用户、有违规用户、信誉良好用户、
VIP 用户和企业客户五类持久化标记。会话支持图片、Emoji、正在输入状态、消息到达提示音，
点击消息可就地选择“回复”或“复制”。图片仅接受 JPG/PNG/GIF/WebP，单张最大 5 MB。

客服预回答默认使用本机 Ollama 的 OpenAI 兼容接口：

```bash
ollama serve
ollama pull qwen2.5:7b
```

可在 `.env` 中通过 `LOCAL_LLM_BASE_URL`、`LOCAL_LLM_MODEL`、`LOCAL_LLM_API_KEY` 和
`LOCAL_LLM_AUTO_SUGGEST` 调整。新客户消息到达时可自动生成建议，客服也可点击输入框旁的
AI 图标手动请求；建议只进入草稿区，由客服确认后发送。本地服务未启动时界面会显示明确错误，
不会将客户上下文改发给 DeepSeek。

在后台「AI 设置 → 客服预回答模型」中选择 **DeepSeek Agent** 后，预回答不再用独立提示词
直连模型，而是复用客户端 `/chat` 的同一套 Agent 回复链路（`Scheduler.preview_reply`）：
同样经过短期记忆/长期摘要/槽位、上下文路由与专家/托管 Agent、只读工具调用，因此后台草稿与
客户实际会收到的 Agent 回复一致。该预览在会话的**深拷贝**上运行且不落库，不会给真实会话
追加消息、增加轮次或改动槽位；工具层额外屏蔽 `create_ticket`、`submit_refund` 等写操作，
路由落到“转人工/无法判定”时改由排队托管 Agent 直接生成草稿，保证预回答只读、无业务副作用。
返回结果会附带来源 Agent、路由原因与调用工具，后台面板在模型名旁同步展示来源 Agent。

内置演示场景（`main.py`）一次跑完全部核心机制：

| 轮次 | 用户输入 | 展示的机制 |
|---|---|---|
| 1 | 想申请退款 | 路由到订单 Agent → 缺订单号，反问并挂起待办流程 |
| 2 | 订单号是 SO… | 待办续接回原流程；DAG 并行拉取订单+物流+退款资格 |
| 3 | 确认，提交退款 | 确认门禁放行敏感工具 `submit_refund` |
| 4 | 查下物流 | 槽位记忆复用，无需再问订单号 |
| 5 | 支持 7 天无理由退货吗 | 路由到 FAQ Agent，知识库检索 + 答案组织 |
| 6 | 投诉，转人工 | 路由到人工 Agent，工单附带会话摘要与槽位 |

## 三种运行模式

同一套记忆、工具、持久化底座，三种调度形态按 `--mode` 切换：

| 模式 | 决策方式 | 适用场景 |
|---|---|---|
| `pipeline`（默认） | 规则优先路由 → 专家 Agent 执行；LLM 仅做兜底分类 | 高频意图占比大、要求零延迟与强可控 |
| `llm` | 每轮由 LLM 依据“Agent 职责 + 会话上下文（待办流程/槽位/近期对话）”做语义路由，规则退化为安全网 | 意图长尾、表述多样，规则维护成本高 |
| `orchestrator` | 无路由器；总控 Agent 通过 function calling 统一调度“业务工具 + 专家 Agent（Agent-as-Tool）”，循环决策直到产出答复 | 复杂诉求需要多能力自由组合 |

`orchestrator` 模式的协作结构：

```
用户 ◄──► OrchestratorAgent（LLM 大脑，function calling 循环）
              │
              ├── 业务工具直连：query_order / query_logistics / check_refund_eligibility
              │                 submit_refund（确认门禁）/ search_knowledge / create_ticket
              │
              └── 专家 Agent（Agent-as-Tool）：
                    ask_order_agent    ──► OrderAgent（内部自带 DAG 并行编排与退款状态机）
                    ask_policy_agent   ──► FAQAgent（知识库检索）
                    transfer_to_human  ──► HumanAgent（创建工单；FAQ 情绪不佳时自动接力移交）
```

多轮断点续接在编排模式下同样成立：编排对话草稿（含每次工具调用与结果）作为检查点
持久化在会话状态里，下一轮恢复后总控看得到“上轮做到哪一步”；敏感操作被确认门禁
拦截时，用户回复“确认”即自动放行重试。

> 说明：MockLLM 用规则剧本“模拟”真实模型的两类决策（语义路由 / function calling 编排），
> 因此三种模式离线即可演示；接 `--llm openai` 后，`llm` 与 `orchestrator` 模式即由真实
> 模型接管语义决策，代码零改动。

## 架构总览

```
                      ┌────────────────────────────────────────────────┐
   用户消息 ─────────► │              调度引擎 Scheduler                 │
                      │  (app/engine/scheduler.py)                     │
                      │                                                │
                      │  1 记忆写前更新 ──► 2 上下文路由 ──► 3 Agent执行 │
                      │        ▲                 │             │        │
                      │        │          Router │      handoff循环      │
                      │        │           ▼             ▼        │    │
                      │  5 记忆写回    ┌──────────────────────────┐   │    │
                      │        │      │ order / faq / human Agent│◄──┘    │
                      │        │      └──────────┬───────────────┘        │
                      │        ▼                 ▼                        │
                      │  6 持久化      ToolExecutor（并行 / DAG / 确认门禁）│
                      └────────┬─────────────────┬────────────────────────┘
                               ▼                 ▼
                    FileSessionStore      ToolRegistry（6 个内置工具，
                    data/sessions/*.json  可替换为真实业务系统）
```

```
Customer-service-agent/
├── main.py                    # 演示 / 交互入口
├── app/
│   ├── core/
│   │   ├── models.py          # Message / SessionData / ToolCall / RouteDecision 等数据模型
│   │   ├── llm.py             # LLM 抽象：MockLLM（离线）+ OpenAICompatLLM（真实模型）
│   │   └── context.py         # RunContext：单次请求的共享执行环境 + trace 轨迹
│   ├── memory/                # 对话记忆
│   │   ├── manager.py         # MemoryManager：短期窗口 + 长期摘要 + 槽位
│   │   ├── extractor.py       # 槽位抽取（正则，可换 LLM/NLU）
│   │   └── store.py           # 会话持久化（JSON 文件，可换 Redis/DB）
│   ├── tools/                 # 工具层
│   │   ├── base.py            # ToolSpec / BaseTool / ToolRegistry
│   │   ├── executor.py        # 单次调用 / 并行扇出 / DAG 计划编排 / 确认门禁
│   │   └── builtin.py         # 6 个内置 Mock 工具 + Mock 业务数据
│   ├── agents/                # Agent 层
│   │   ├── base.py            # BaseAgent / AgentDescriptor
│   │   ├── router.py          # 上下文路由（rules / llm 双模式）：规则 → 待办续接 → LLM 分类
│   │   ├── orchestrator.py    # 总控编排 Agent：Agent-as-Tool 委派 + function calling 循环
│   │   ├── order_agent.py     # 订单售后（退款状态机：核查→确认→提交）
│   │   ├── faq_agent.py       # 政策问答（知识库检索）
│   │   └── human_agent.py     # 转人工（创建工单）
│   ├── engine/scheduler.py    # 调度引擎：完整请求管线
│   └── api/server.py          # 可选 FastAPI 服务
└── tests/test_system.py       # 记忆/编排/路由/多轮端到端 测试
```

## 四个核心机制的设计

### 1. Agent 调度引擎（`engine/scheduler.py`）

每个请求走固定管线：**记忆写前更新 → 上下文路由 → Agent 执行（含移交循环）→ 记忆写回 → 持久化**。

- **移交循环**：Agent 可返回 `AgentResult(handoff="human")` 把控制权交给引擎，引擎在
  `MAX_HANDOFFS=3` 内切换目标 Agent 重入执行（如 FAQ 未命中且用户情绪不佳 → 转人工）；
- **异常隔离**：单个 Agent/工具抛异常被引擎兜底为一句道歉回复并记入 trace，不击穿服务；
- **可观测性**：每轮产生结构化 `trace`（agent_start / tool_start / tool_end / handoff /
  memory_slot / memory_summary …），接日志/监控即可；`python main.py --trace` 可查看；
- **并发友好**：Agent 全部无状态单例，可变状态集中在可持久化的 `SessionData`，
  多会话并发互不干扰。

### 2. 多工具编排（`tools/`）

- **注册表**：每个工具自带 JSON Schema（`ToolSpec`），同一份 schema 既给执行器做参数校验，
  也直接转成 LLM function calling 的 `tools` 入参；
- **三种执行形态**（`ToolExecutor`）：
  - `call()`：单次调用，统一处理注册校验、确认门禁、异常转失败结果、轨迹；
  - `execute_calls()`：一批独立调用线程池并行扇出；
  - `run_plan()`：声明式 DAG 编排——步骤显式声明 `depends_on`，同层并行、跨层串行；
    参数支持 `slot:order_id`（取槽位记忆）与 `step:step_id.field`（取上游结果）两种引用；
    上游失败自动跳过下游；
- **确认门禁**：敏感工具（如 `submit_refund`）标记 `requires_confirm=True`，
  执行器在 `ctx.state["user_confirmed"]` 未置位时拒绝执行——Agent 只能在用户明确确认后放行，
  这是客服场景的合规刚需。

### 3. 上下路由（`agents/router.py`）

路由器支持两种模式（`--mode` 切换）：

**rules 模式（pipeline 默认）** —— 三级策略按序生效：

1. **高优先级规则**：显式诉求（转人工/投诉）直接命中，保证用户任何时候都能逃离机器人；
2. **待办续接（上下文路由的关键）**：会话存在未完成流程（`ctx.state.pending`，含
   agent/action/stage）时，把消息续接回原 Agent 的对应阶段——用户补一个订单号、
   回一个“确认”，不需要重新表达意图，流程从断点继续；
3. **规则关键词 → LLM 意图分类兜底**：都没有命中时由 LLM 按 Agent 职责描述分类；
   仍失败则返回 `None`，由引擎做澄清反问。

**llm 模式（LLM 全权路由）** —— 每轮都由 LLM 语义决策，路由提示词包含待办流程、
槽位与近期对话，因此“是否续接原流程”也交给模型判断；规则退化为 LLM 未命中时的安全网。

规则优先级刻意设计为 **human > 政策类 FAQ > 订单**：“退货的条件是什么”去 FAQ，
“我想退货”去订单 Agent。

### 4. 对话记忆与多轮支撑（`memory/`）

三层记忆组合，全部随 `SessionData` 持久化（JSON 文件，接口可换 Redis/DB）：

| 层 | 载体 | 作用 |
|---|---|---|
| 短期记忆 | `messages`，滑动窗口 10 条 | 逐字保留近期对话，进提示词 |
| 长期记忆 | 被淘汰消息积攒 3 条后由 LLM 压缩为 `summary` | 跨窗口保留对话要点，转人工工单自动附带 |
| 槽位记忆 | `slots`（订单号/手机号/用户 ID 等） | 跨轮、跨 Agent 复用结构化实体，如退款后查物流不再问订单号 |

另外用一个持久化的 `state.pending` 保存**流程断点**（等待订单号/等待确认），
这是多轮任务型对话不“失忆”的关键；`state.user_confirmed` 则驱动确认门禁。

一次请求中记忆的时序：用户消息先写入并抽取槽位（保证本轮立即可用）→ 路由与 Agent 执行
期间随时可读 → 回复入库后触发窗口淘汰与摘要压缩 → 整体会话落盘。

## 扩展指南

- **加一个业务工具**：继承 `BaseTool`，填 `ToolSpec`（名称/描述/JSON Schema），实现 `run()`，
  在 `register_builtin_tools`（或你自己的装配处）注册即可；若 Agent 交给 LLM 自主选工具，
  把 `registry.schemas(allowed)` 传给 `llm.chat(tools=...)` 并用 `executor.execute_calls()`
  消费返回的 `tool_calls`。
- **加一个业务 Agent**：继承 `BaseAgent`，写好 `AgentDescriptor`（职责描述含“关键词:”段落，
  供 MockLLM/路由使用），在 `Scheduler` 的 `agents` 字典里注册；路由规则零改动即可被发现。
  在 `orchestrator` 模式下，它还会被自动包装成可委派函数（`AgentDelegationTool`）暴露给总控 LLM。
- **接入真实 LLM**：`--llm openai` 或 `Scheduler(llm=OpenAICompatLLM(...))`；
  Mock 与真实实现共用同一接口，业务代码无感。
- **替换存储**：实现 `get_or_create()/save()` 两个方法即可换成 Redis/MySQL/会话服务。
- **真实业务系统**：替换 `tools/builtin.py` 中各工具的 `run()`（查库/调内部 API），
  Mock 数据仅用于演示。

## 生产化建议

- **幂等与并发**：会话文件存储为单进程演示实现；上生产换 Redis 并以 `session_id`
  加分布式锁或按会话哈希做路由，避免并发写覆盖。
- **敏感操作**：退款/改地址等一律走 `requires_confirm` 门禁 + 服务端二次校验；
  审计日志直接消费 `RunContext.trace`。
- **安全**：槽位抽取目前含手机号正则，落库前按需脱敏（PII）；工具层对入参做白名单校验，
  防止 LLM 生成的参数注入。
- **评测**：把路由正确率、槽位抽取 F1、任务完成率做成离线回归集，
  `tests/test_system.py` 的端到端用例可以直接演进为回归基线。
