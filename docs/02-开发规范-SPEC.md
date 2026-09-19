# Interview Coach · 开发规范（SPEC v1.1）

> 配套文档：《01-产品设计文档-PRD.md》
> 原则：**两模块强解耦（面试官 LLM / JEV 测评）**、配置驱动、本地优先、成本可忽略但延迟敏感。

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-09-19 | 首版 |
| **v1.1** | **2026-09-20** | **输入简化：简历/JD 只走纯文本，各一个 textarea。移除文件上传端点与 docparse 依赖（`pdfplumber` / `python-docx` 不再需要）；`POST /api/sessions` 只收 `resume_text` / `jd_text` 字符串，均可为空；前端支持 txt/md 拖入读文本。二期再开上传解析** |

---

## 1. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | **Python 3.12 + FastAPI** | 与既有 JEV/Whisper 管线同栈；SSE 流式推送原生支持 |
| 前端 | **React + Vite + TypeScript**（或一期极简版：单页原生 JS，见 §9） | 打分卡需要状态管理；SSE 消费简单 |
| 面试官 LLM | **MiniMax-M3**，OpenAI 兼容端点 `https://api.minimaxi.com/v1` | 已实测：0.96s、JSON 干净。坑：需 `reasoning_split: true` 防 `<think>` 混入 content |
| 测评 | **TypeSafe Jev Decisions API**，经 OpenRouter：`https://openrouter.ai/api/alpha/decisions`，model `~typesafe/jev-latest` | 已实测 200；单次 ≈ $0.000022 |
| ASR | **mlx-whisper**（本地），解释器 `~/.workbuddy/binaries/python/envs/default/bin/python` | 已有管线，环境坑已趟平 |
| 材料输入 | **纯文本粘贴**（前端单个 textarea ×2） | 一期不做文件解析，零依赖；txt/md 由浏览器 `File.text()` 直接读 |
| 持久化 | **SQLite**（单文件）+ 原始媒体文件落盘 | 一期无多用户，最简可靠 |
| 部署 | 本机运行（127.0.0.1），一键 `make dev` | 一期自用，不上云 |

**Python 环境铁律**：主服务用 uv 管理（`uv sync`）；Whisper 子进程固定用 `~/.workbuddy/binaries/python/envs/default/bin/python`（那里才有 mlx_whisper + imageio_ffmpeg）。

---

## 2. 仓库结构

```
interview-coach/
├── docs/
│   ├── 01-产品设计文档-PRD.md
│   └── 02-开发规范-SPEC.md        ← 本文档
├── server/
│   ├── pyproject.toml             # uv 管理
│   ├── app/
│   │   ├── main.py                # FastAPI 入口 + 路由注册
│   │   ├── config.py              # 全部配置（env 读取，见 §8）
│   │   ├── routes/
│   │   │   ├── sessions.py        # 会话 CRUD / 创建（只收纯文本材料）
│   │   │   ├── chat.py            # SSE：消息发送、事件流
│   │   │   └── reports.py         # 报告生成与读取
│   │   ├── services/
│   │   │   ├── interviewer.py     # 面试官 Agent（LLM）
│   │   │   ├── evaluator.py       # JEV 测评模块
│   │   │   ├── asr.py             # Whisper 子进程封装
│   │   │   ├── (docparse.py)      # 【二期】PDF/docx 上传解析，一期不存在
│   │   │   └── report.py          # 报告聚合
│   │   ├── llm/
│   │   │   ├── client.py          # OpenAI 兼容客户端（含 reasoning_split 处理）
│   │   │   └── prompts/
│   │   │       ├── interviewer_technical.md
│   │   │       ├── interviewer_hr.md
│   │   │       ├── interviewer_exec.md
│   │   │       └── report_writer.md
│   │   ├── models/
│   │   │   └── schemas.py         # Pydantic 数据模型（见 §4）
│   │   └── db/
│   │       ├── database.py        # SQLite 连接
│   │       └── migrations.sql
│   └── tests/
│       ├── test_evaluator.py      # 含离线 mock，不花钱
│       ├── test_interviewer.py
│       └── test_budget.py         # 题量/追问预算状态机
├── web/
│   ├── package.json
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/{Setup,Session,Report}.tsx
│   │   ├── components/{ChatStream,ScoreCard,VoiceButton,ProgressHeader}.tsx
│   │   └── api/client.ts
│   └── ...
├── Makefile                       # make dev / make test / make check
└── README.md
```

---

## 3. 核心流程与时序

```
用户发送回答
   │
   ▼
POST /api/sessions/{id}/messages （text 或 audio_id）
   │
   ├─► [同步] 落库 user message
   │
   ├─► [异步任务 A：面试官] interviewer.respond(history, resume, jd, budget_state)
   │        └─ SSE 事件: interviewer_token* → interviewer_done(question_no?, type=followup|next|closing)
   │
   └─► [异步任务 B：JEV 评测] evaluator.evaluate(question, answer, jd_ctx)
            └─ SSE 事件: evaluation_pending → evaluation_done(scorecard) | evaluation_failed
```

- A、B **并行启动，互不阻塞**；前端按事件类型分别渲染。
- SSE 事件统一信封：`{"event": "...", "data": {...}, "seq": n}`，断线用 `seq` 续传（一期可用"重连后全量拉取兜底"简化）。

---

## 4. 数据模型（Pydantic / SQLite schema）

```python
class InterviewType(str, Enum):
    technical = "technical"
    hr = "hr"
    executive = "executive"

class SessionStatus(str, Enum):
    setup = "setup"          # 已填材料，未开始
    active = "active"
    closing = "closing"      # 收尾语阶段
    done = "done"
    abandoned = "abandoned"

class Session:
    id: str                  # uuid4
    interview_type: InterviewType
    total_questions: int     # 8 | 9 | 10
    main_q_answered: int     # 已回答的主问题数
    status: SessionStatus
    started_at: datetime | None
    resume_text: str
    jd_text: str
    created_at: datetime

class Message:
    id: str
    session_id: str
    role: Literal["interviewer", "user", "system"]
    kind: Literal["question", "followup", "answer", "closing", "meta"]
    question_no: int | None  # 所属主问题序号（追问与其主问题同号）
    content: str
    audio_path: str | None   # 语音原始文件
    created_at: datetime

class Evaluation:            # 一条回答 ↔ 一条评测（1:1）
    id: str
    message_id: str          # 指向 user 的 answer
    question_no: int
    is_followup: bool
    relevance: float          # 0-4
    completeness: float       # 0-4
    structure: float          # 0-4
    conciseness: float        # 0-1（noul，1=无冗余）
    specificity: float        # 0-4
    overall_band: Literal["excellent","good","pass","weak"]
    confidence: float         # JEV answers 自带置信度（取各问题最小值）
    low_confidence: bool      # confidence < CONFIDENCE_FLOOR
    raw_response: str         # JEV 原始 JSON，落库备查
    created_at: datetime
```

### 4.1 材料输入契约（一期：纯文本，无上传端点）

```http
POST /api/sessions
Content-Type: application/json

{
  "type": "technical",          # technical | hr | executive
  "total": 8,                   # 8 | 9 | 10
  "resume": "<简历全文，可为空字符串>",
  "jd":     "<JD 全文，可为空字符串>"
}
→ 200 {"id": "…", …}
```

- 后端**不接收任何文件**；没有 `/api/upload` 路由，没有 multipart 处理。
- 长度限制：`resume` ≤ 8000 字符、`jd` ≤ 4000 字符，超出在**前端**截断并提示（后端也做一次硬截断防御，不报错）。
- 空值语义：两者都可为空。为空时面试官 prompt 的材料段写"候选人未提供简历/JD，请基于岗位通用要求提问"。
- 前端实现（一期单文件版 `web/index.html`）：
  - `#resumeText` / `#jdText` 两个 `<textarea class="paste">`，各配一个「用示例填充」次要按钮；
  - 拖入 `.txt / .md` → `File.text()` 读入填入；其他扩展名在框内给出一行提示，请用户复制文本（不静默失败）；
  - **不再有 dropzone / 文件 chip / 上传进度**等 UI。

---

## 5. 面试官 Agent 规格（services/interviewer.py）

### 5.1 职责边界

- **只做**：开场白、出主问题、追问（≤1）、收尾语、报告文字建议（复用同客户端另一 prompt）。
- **不做**：任何评分、任何"你答得不错"之类的评价性措辞（prompt 中明令禁止，防止与打分卡信息冲突）。

### 5.2 Prompt 结构（每种类型一个 md 模板）

```
[角色] 你是{技术/HR/高管}面试官，人格设定与追问风格见 PRD §5.4
[材料] 简历（全文）、JD（全文）、已提取的关键考察点
[状态] 当前第 N/{total} 题、是否处于追问中、已用时长 X 分钟
[规则]
 1. 每次只问一个问题；主问题之间必须基于候选人上一回答自然过渡
 2. 处于追问态时只能追问，追问后必须切下一题
 3. main_q_answered == total-1 时，最后一题用"反问/开放收尾题"
 4. 禁止评价候选人表现；禁止给分；禁止透露评分维度
 5. 输出 JSON：{"type": "question"|"followup"|"next"|"closing", "content": "..."}
```

> 输出用 JSON 模式（response_format / 前置 system 强约束 + 解析兜底剥离 ``` 围栏，复用 jev-ultrafast 的 `clean_json` 思路）。

### 5.3 题量预算状态机（tests/test_budget.py 必测）

```
状态: ASKING(n) --用户回答--> FOLLOWUP_OR_NEXT
FOLLOWUP_OR_NEXT:
  - followup_count[n] == 0 → 可追问 → ASKING(n, followup)
  - followup_count[n] == 1 或 LLM 判断不需追问 → NEXT → ASKING(n+1)
  - n+1 == total → 收尾题（反问）→ CLOSING → DONE
用户点"提前结束" → 任意状态 → CLOSING → DONE
```

不变量：`main_q_answered ≤ total_questions`；每主问题 `followup_count ≤ 1`；状态迁移只由后端裁决，**不信任 LLM 输出的 type 字段做状态变更**（type 仅作参考，代码按状态机校正）。

---

## 6. JEV 测评模块规格（services/evaluator.py）

### 6.1 请求构造（一次 Decisions 请求，6 问并行）

```python
state = {
    "interview_question": question_text,          # 本次被评的问题（含追问语境）
    "candidate_answer": answer_text,
    "jd_key_requirements": jd_digest,             # 预先抽取的 JD 要点（≤200字）
    "expected_answer_hints": question.hints,      # 出题时 LLM 一并给出的要点提示（见 6.3）
}
questions = {
    "relevance":    {"type": "score", "range": [0, 4], "rubric": "4=直接回答所问；2=部分相关；0=答非所问"},
    "completeness": {"type": "score", "range": [0, 4], "rubric": "对 expected_answer_hints 的覆盖程度"},
    "structure":    {"type": "score", "range": [0, 4], "rubric": "结构清晰、逻辑连贯、有框架"},
    "redundancy":   {"type": "noul"},             # 1=存在重复冗余；coniseness = 1 - value
    "specificity":  {"type": "score", "range": [0, 4], "rubric": "具体事例/数字/细节支撑程度"},
    "overall_band": {"type": "choice", "options": ["excellent", "good", "pass", "weak"]},
}
```

- 端点/鉴权/模型名见 §8 环境变量；复用已验证的调用方式（OpenRouter 接受 instructions 字典形式，勿再拍平）。
- **心法**：一个问题只背一个维度（Composite Scoring），不要写"请综合评估 X 和 Y"的复合问题。

### 6.2 鲁棒性规则

| 情况 | 处理 |
|---|---|
| `Invalid TypeSafe response`（概率和≠1 等校验失败） | **可重试错误**：指数退避重试 2 次（200ms/600ms），仍失败 → `evaluation_failed`，面试继续 |
| `dim_conf < CONFIDENCE_FLOOR`（默认 **0.45**） | 整卡降级：`low=true`，前端打灰，**且不进均分** |
| `band_conf < BAND_CONFIDENCE_FLOOR`（默认 **0.30**） | 仅 `band_low=true`，总评徽章半透明 + 角标 `?`，**不影响分数** |
| 超时（3s） | 取消并按失败处理 |
| 回答 < 15 字或纯语气词 | 跳过 JEV，直接落"未评分"（省调用量，也避免模型硬评） |

> **实测校准（2026-09-20，真实调用）**：JEV 的 `confidence` 不是 0~1 均匀分布，实测 score 维度 ≈ **0.5–0.8**、choice 档位 ≈ **0.41–0.55**。
> 因此 **不能照搬"0.60 才算可信"的直觉**——那会把几乎每一张卡都误判成低置信。
> 现口径：`CONFIDENCE_FLOOR=0.45`（管维度分）、`BAND_CONFIDENCE_FLOOR=0.30`（只管档位徽章）。
> 实测单次成本 **$0.0000366**，端到端（JEV 与面试官 LLM 并行）**1.8s** 返回打分 + 下一题。

### 6.3 要点提示（hints）生成

出主问题时，面试官 LLM 在输出 JSON 中附带 `"hints": ["要点1", "要点2", ...]`（它出题时本来就知道想考察什么，顺手产出，零额外调用）。hints 同时用于：completeness 评分依据 + 报告页"这题在考什么"。

### 6.4 汇总口径（report.py）

- 维度均分：只统计 `low_confidence=false` 的评测；追问评测**不参与**维度均分，仅入逐题明细。
- 总评分布：各 band 计数；雷达图取 5 个维度均分归一化。
- 改进建议：把全场（问题、回答、分数、hints）交给 LLM（report_writer.md），要求输出 ≤3 条、每条必须引用具体某题原文作证据。

---

## 7. 语音与材料输入规格

### 7.1 ASR（services/asr.py）

- 接口：`POST /api/asr`（multipart audio，≤ 2 分钟 / ≤ 10MB，超限 413）
- 流程：webm → ffmpeg（imageio_ffmpeg 自带的二进制）转 16k wav → mlx-whisper（zh，`initial_prompt="面试回答场景"` 抑制幻觉）→ 返回 `{text, duration_ms}`
- **不自动发送**：前端拿到文字回填输入框，由用户确认发送。
- 子进程调用 + 30s 超时；失败返回可读错误，不影响打字路径。

### 7.2 材料输入（一期：纯文本）

**后端无解析逻辑。** `resume_text` / `jd_text` 直接来自请求体字符串，落库前做：

1. 去首尾空白、统一换行；
2. 硬截断：简历 8000 字符、JD 4000 字符；
3. 空字符串 → prompt 材料段写"未提供"，不报错。

前端负责把用户材料变成文本：

| 输入方式 | 处理 |
|---|---|
| 直接粘贴 | 原样 |
| 拖入 `.txt / .md` | `await file.text()` 填入框内 |
| 拖入 `.pdf / .docx` | 框内提示"暂不支持解析 xxx，请打开后复制文本粘贴" |

> 二期若要恢复上传解析，新增 `services/docparse.py`（pdfplumber + python-docx）与 `POST /api/upload`，并把 `UPLOAD_DIR` 重新启用；本期 `UPLOAD_DIR` 仅用于 ASR 音频。

---

## 8. 配置与环境变量（server/.env，模板 .env.example 入库）

```bash
# LLM（面试官）
TEXT_MODEL_BASE_URL=https://api.minimaxi.com/v1
TEXT_MODEL_API_KEY=sk-cp-...
TEXT_MODEL=MiniMax-M3

# JEV（测评，经 OpenRouter）
TYPESAFE_BASE_URL=https://openrouter.ai/api/alpha/decisions
TYPESAFE_API_KEY=sk-or-v1-...
TYPESAFE_MODEL=~typesafe/jev-latest

# 测评策略
CONFIDENCE_FLOOR=0.45          # score 维度门控
BAND_CONFIDENCE_FLOOR=0.30     # choice 档位门控（四选一置信度天然更低）
EVAL_RETRY=2
EVAL_TIMEOUT_S=3

# ASR
WHISPER_PYTHON=~/.workbuddy/binaries/python/envs/default/bin/python
ASR_MAX_SECONDS=120

# 应用
DB_PATH=./data/interview.db
UPLOAD_DIR=./data/uploads
```

**铁律**：key 只进 `.env`（gitignore）；改 `.env` 必须重启进程（jev-ultrafast 实测教训：启动时读一次）。

---

## 9. 前端规格要点

- 三页：`Setup`（粘贴简历 + 粘贴 JD + 选类型 + 题数）→ `Session`（对话）→ `Report`（报告）。
- SSE 消费：一个 `EventSource`，按 event 分发 reducer；`evaluation_pending` 渲染打分卡骨架屏。
- 打分卡组件：5 维数字 + 总评徽章；`low_confidence` 时整体 60% 透明度 + 灰字提示。
- 录音组件：MediaRecorder（audio/webm）；录中超时自动停（120s）；状态机 idle→recording→transcribing→filled。
- 一期极简备选：若想更快见效，可先出"原生 HTML + HTMX"版（后端渲染），打分卡用 SSE 局部刷新——功能不减，砍掉构建链。**默认走 React，这是可选项。**

---

## 10. 质量规范

- **测试分层**（花钱的测试要显式标记）：
  - 离线单测（默认跑，零成本）：预算状态机、schema 校验、JEV 响应解析（用录制的真实响应 fixture）、重试逻辑、文本截断与空材料兜底。
  - 冒烟脚本（手动，标 `@pytest.mark.live`）：真实调一次 JEV 6 问、MiniMax 一轮对话、Whisper 一段 5s 音频。
- **日志**：每场面试的 LLM/JEV 原始请求响应落 `data/logs/{session_id}/`，出问题可回放。
- **代码规约**：ruff + mypy（server）/ eslint + tsc（web）；函数职责单一，evaluator 不得 import interviewer（解耦由 import 图保证，CI 加 `import-linter` 契约检查）。
- **验收清单（M4）**：
  - [ ] 纯文字走完一场 8 题技术面，报告正确
  - [ ] 语音走完一场，转写回填可用
  - [ ] JEV 挂掉（拔 key）面试仍可完整进行
  - [ ] 中途刷新页面，会话可恢复
  - [ ] 一场总 JEV 成本 < $0.001（日志核对）

---

## 11. 里程碑（对应 PRD §8，含关键交付物）

| 里程碑 | 交付物 | 完成判据 |
|---|---|---|
| M1 | interviewer.py + prompts + 预算状态机 + SSE | 纯文字完整面试，状态机单测全绿 |
| M2 | evaluator.py + 打分卡 UI + 报告聚合 | live 冒烟过，P50 打分 ≤1.5s |
| M3 | asr.py + 录音组件 | 5s/60s 音频实测转写可用 |
| M4 | 报告页 + 持久化 + 恢复 + 验收清单 | 清单全勾，v1.0 自用 |
