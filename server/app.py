"""Interview Coach · 后端

三个模块，彼此解耦：
  interviewer —— MiniMax 聊天模型，只提问/追问/收尾，禁止评价
  evaluator   —— TypeSafe Jev Decisions API，一次请求并行 6 个原子问题打分
  reviewer    —— 复盘报告：分数由代码统计，文字建议由 LLM 写

存储：SQLite（data/interview.db）—— 会话、消息、评分、报告全部落盘，重启可恢复。

启动：
  ~/.workbuddy/binaries/python/envs/default/bin/python -m uvicorn app:app --port 8890 --app-dir .
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
from asr import transcribe as asr_transcribe

HERE = Path(__file__).parent
WEB = HERE.parent / "web"
DATA = HERE.parent / "data"
if not DATA.exists():
    DATA.mkdir(parents=True)

# ---------- env ----------
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

TEXT_BASE = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
TEXT_KEY = os.environ.get("TEXT_MODEL_API_KEY", "")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "MiniMax-M3")
JEV_URL = os.environ.get("TYPESAFE_BASE_URL", "https://openrouter.ai/api/alpha/decisions")
JEV_KEY = os.environ.get("TYPESAFE_API_KEY", "")
JEV_MODEL = os.environ.get("TYPESAFE_MODEL", "~typesafe/jev-latest")

# 实测（2026-09-20 真实调用）：JEV confidence 不是 0~1 均匀分布。
#   score 维度 ≈0.5-0.8；choice 四选一 ≈0.41-0.55（天然分散，天然更低）。
# 因此两条门控分开设阈值，且都不能照搬"0.6 才算可信"的直觉。
DIM_FLOOR = float(os.environ.get("CONFIDENCE_FLOOR", "0.45"))          # score 维度门控
BAND_FLOOR = float(os.environ.get("BAND_CONFIDENCE_FLOOR", "0.30"))    # band 门控，只影响徽章
MAX_FOLLOWUPS = int(os.environ.get("MAX_FOLLOWUPS", "3"))              # 整场追问上限
SHORT_ANSWER_CHARS = int(os.environ.get("SHORT_ANSWER_CHARS", "15"))   # 低于此字数标记"回答偏短"

# trust_env=False：不读 HTTP_PROXY 等环境代理。本机代理（127.0.0.1:59122）时好时坏，
# 一旦挂掉所有模型请求被代理拦死 → ConnectError，整场面试只剩兜底题（用户实测踩到）。
# MiniMax 是国内端点本就该直连；OpenRouter 实测直连也可达（2026-09-20 验证）。
client = httpx.AsyncClient(timeout=60, trust_env=False)

# ---------- 面试官人格 ----------
PERSONAS = {
    "technical": "技术面试官。深挖项目细节、技术选型理由、难点与权衡；追问要顺着技术细节往下钻。",
    "hr": "HR 面试官。考察动机、行为事例、稳定性、自我认知；追问要落在「当时你具体做了什么、结果如何」。",
    "executive": "高管面试官。考察行业判断、战略思考、团队搭建、成长性；追问要逼出判断依据与代价意识。",
}
TAGS = {"technical": "技术面", "hr": "HR 面", "executive": "高管面"}
DIM_LABELS = ["切题", "完整", "条理", "简洁", "具体"]

SYSTEM = """你是{persona}

材料：
简历：{resume}
岗位 JD：{jd}

规则：
1. 每次只输出一个问题，口语化、简短（不超过 60 字），像真人面试官说话。
2. 下一个问题必须基于候选人刚才的回答自然过渡，不要照本宣科念题库。
3. 严禁评价、夸奖、批评、给分或透露任何评分标准，你只负责提问。
4. 最后一题（第 {total} 题）必须是开放收尾题，例如「你有什么想问我的」，收尾题不参与评分。
5. 同时输出这道题的考察要点 hints（2-3 条，每条不超过 12 字），用于后续评分。
6. 追问节奏：如果候选人刚才的回答偏短、缺少具体事例或数字、或绕开了问题，move 选 "followup"
   顺着细节往下问（每题最多 1 次，整场至少追问 2 次）；回答已经充分时 move 选 "next"。

只输出 JSON，不要任何解释：
{{"move":"{moves}","content":"问题内容","hints":["要点1","要点2"]}}"""

OPENING = """输出开场白和第一题，只输出 JSON：
{{"greeting":"一句话自我介绍+说明这场面试的形式，不超过 50 字","content":"第 1 题","hints":["要点1","要点2"]}}"""

REVIEW_SYSTEM = """你是资深面试官兼求职教练。下面是一场模拟面试的逐题评分与回答摘要。

请写出复盘，只输出 JSON，不要任何解释：
{{"summary":"总体判断，150 字以内，说实话、不客套、不夸奖",
  "highlights":["亮点1","亮点2"],
  "improvements":[{{"dim":"维度名","issue":"具体表现出的不足","action":"下次怎么改，给可执行做法"}}]}}

要求：
1. improvements 固定 3 条，优先挑平均得分最低的维度。
2. 每条必须引用候选人的真实回答内容作为证据，不要泛泛而谈。
3. action 要具体到"下一场就能做"，例如"先说结论再补论据"，不要写"加强练习"。
4. 全部用中文，语气直接、专业、不打击人。"""


def clean_json(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    return fence.group(1).strip() if fence else text


def _loads(text: str):
    """安静的 json.loads：失败返回 None，不抛异常。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_json(text: str) -> str:
    """从混杂文本里抠出第一个完整的最外层 JSON 对象（按括号配对，不靠贪婪正则）。"""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return ""


async def llm(messages: list[dict], temperature: float = 0.7) -> dict:
    """MiniMax OpenAI 兼容端点。reasoning_split 防止 <think> 混入 content。"""
    extras = {"reasoning_split": True} if "minimaxi.com" in TEXT_BASE else {}
    r = await client.post(
        TEXT_BASE + "/chat/completions",
        headers={"Authorization": f"Bearer {TEXT_KEY}"},
        json={"model": TEXT_MODEL, "messages": messages,
              "response_format": {"type": "json_object"},
              "temperature": temperature, **extras},
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    t = clean_json(content).strip()

    # 1) 整段就是合法 JSON 对象 → 直接用。
    #    注意：不能强制要求某个字段存在——复盘返回的是 {summary/highlights/improvements}，
    #    没有 content 字段，早期版本因此误判为解析失败、永远走兜底模板。
    d = _loads(t)
    if isinstance(d, dict):
        return d

    # 2) 文本里内嵌了 JSON（模型不遵守 json_object 时）——用括号配对把最外层对象抠出来
    d = _loads(_extract_json(t))
    if isinstance(d, dict):
        return d

    s = t.strip('"')

    # 3) 模型偶尔把示例原样吐出来（还带着 {{ }} 双括号），这时它不是"纯文本问题"，
    #    直接当题目用会把 JSON 残片显示给用户。先把多余括号收成单层再试一次。
    if s.startswith("{"):
        fixed = re.sub(r"^\s*\{+", "{", s)
        fixed = re.sub(r"\}+\s*$", "}", fixed)
        d = _loads(fixed)
        if isinstance(d, dict):
            return d
        print("[warn] llm 返回 JSON 残片，不作为题目使用：", s[:200], flush=True)
        raise ValueError("llm returned json fragment")

    # 4) 实测坑：MiniMax 开 response_format=json_object 仍经常直接输出纯文本问题，
    #    而且内容质量很好。别浪费这次调用——像问题的纯文本直接包装使用。
    if s and len(s) < 220 and ("？" in s or "?" in s):
        return {"move": "", "content": s, "hints": [], "raw_text": True}

    print("[warn] llm 返回无法解析：", content[:300], flush=True)
    raise ValueError("llm json parse fail")


def build_messages(s: dict, force_followup: bool = False) -> list[dict]:
    if force_followup:
        moves = "followup"
    else:
        moves = "next" if s["followup_used"] >= 1 else "next|followup"
    system = SYSTEM.format(persona=PERSONAS[s["type"]],
                           resume=(s["resume"] or "（未提供）")[:3000],
                           jd=(s["jd"] or "（未提供）")[:1500],
                           total=s["total"], moves=moves)
    if force_followup:
        # 后端判定这题答得薄弱，要求必须追问（不信任模型"自觉"）
        system += ("\n\n【强制追问】候选人刚才的回答明显偏弱（偏题 / 缺要点 / 缺具体事例）。"
                   "你必须针对他回答里最薄弱的一点追问一句，不要换新话题。")
    msgs = [{"role": "system", "content": system}]
    for m in s["history"]:
        msgs.append({"role": "assistant" if m["role"] == "interviewer" else "user",
                     "content": m["content"]})
    return msgs


# ---------- JEV 测评（SPEC §6） ----------
QUESTIONS = {
    "relevance": {"type": "score", "instructions": "该回答是否直接回应了面试官问的这个问题？", "criteria": ["完全答非所问", "完全正面回应"]},
    "completeness": {"type": "score", "instructions": "该回答覆盖了这道题考察要点（hints）的程度？", "criteria": ["要点基本没覆盖", "要点全部覆盖"]},
    "structure": {"type": "score", "instructions": "该回答是否结构清晰、逻辑连贯（如 STAR、总分总）？", "criteria": ["散乱无结构", "结构非常清晰"]},
    "redundancy": {"type": "noul", "instructions": "该回答是否包含重复、绕圈、车轱辘话？", "criteria": {"true": "有明显冗余", "false": "简洁无冗余"}},
    "specificity": {"type": "score", "instructions": "该回答是否包含具体事例、数字或细节支撑？", "criteria": ["全是抽象空话", "有大量具体事例和数据"]},
    "overall_band": {"type": "choice", "instructions": "综合这一轮回答的面试表现属于哪一档？", "criteria": {"excellent": "优秀，可直接使用", "good": "良好，小修即可", "pass": "合格，勉强过关", "weak": "待改进，明显不足"}},
}


async def jev_evaluate(question: str, hints: list[str], answer: str) -> dict:
    state = {"interview_question": question, "expected_hints": hints, "candidate_answer": answer[:4000]}
    for attempt in range(3):
        try:
            r = await client.post(JEV_URL, headers={"Authorization": f"Bearer {JEV_KEY}"},
                                  json={"model": JEV_MODEL, "state": state, "questions": QUESTIONS})
            if r.status_code in (429, 500, 502, 503, 529):
                await asyncio.sleep(0.4 * 2 ** attempt)
                continue
            r.raise_for_status()
            ans = r.json()["answers"]
            dims = [round(float(ans["relevance"]["score"]) * 4, 1),
                    round(float(ans["completeness"]["score"]) * 4, 1),
                    round(float(ans["structure"]["score"]) * 4, 1),
                    round((1 - float(ans["redundancy"]["noul"])) * 4, 1),
                    round(float(ans["specificity"]["score"]) * 4, 1)]
            # 实测坑：单维度 confidence 抖动极大（见过 0.03 也见过 0.96）。
            # 用 min() 会让一个维度的抖动把整卡判成"低置信"→ 被踢出均分 → 答得差的题
            # 反而不计入总分，分数虚高。改用均值代表整卡可信度，逐维值另存备查。
            confs = [c for c in [ans["relevance"].get("confidence"),
                                 ans["completeness"].get("confidence"),
                                 ans["structure"].get("confidence"),
                                 ans["specificity"].get("confidence")] if c is not None]
            dim_conf = sum(confs) / len(confs) if confs else 0.0
            band_conf = ans["overall_band"].get("confidence") or 0
            return {"dims": dims, "band": ans["overall_band"]["choice"],
                    "confidence": round(dim_conf, 2), "band_confidence": round(band_conf, 2),
                    "dim_confs": [round(c, 2) for c in confs],
                    "low": dim_conf < DIM_FLOOR,           # 整卡降级 → 不进均分
                    "band_low": band_conf < BAND_FLOOR,   # 仅徽章半透明
                    "engine": "jev", "cost": r.json().get("usage", {}).get("cost")}
        except Exception as e:                                    # 可重试错误，不中断面试
            if attempt == 2:
                return {"dims": None, "band": None, "engine": "failed", "error": str(e)[:120]}
            await asyncio.sleep(0.4 * 2 ** attempt)
    return {"dims": None, "band": None, "engine": "failed"}


def fallback_evaluate(answer: str, hints: list[str]) -> dict:
    """JEV 不可用时的本地兜底（保证面试不中断）。"""
    t = answer or ""
    link_words = ["首先", "其次", "然后", "最后", "因为", "所以", "总结", "一方面", "结果是"]
    nums = len(re.findall(r"\d+", t))
    hit = sum(1 for h in hints if any(w in t for w in re.findall(r"[\u4e00-\u9fa5]{2,4}", h)))
    sents = [x.strip() for x in re.split(r"[。！？；\n]", t) if len(x.strip()) > 6]
    dup = len(sents) - len(set(sents))
    rel = min(4, 1.8 + min(3, len(t) / 90) * 0.7)
    comp = min(4, 0.8 + hit / max(1, len(hints)) * 3.0)
    struct = min(4, 1.0 + sum(t.count(c) for c in link_words) * 0.75)
    conc = max(0, 4 - dup * 1.3 - max(0, (len(t) - 420) / 160))
    spec = min(4, 0.5 + nums * 0.7 + (0.8 if "比如" in t or "例如" in t else 0))
    dims = [round(rel, 1), round(comp, 1), round(struct, 1), round(conc, 1), round(spec, 1)]
    avg = (dims[0] + dims[1] + dims[2] + dims[4]) / 4
    band = "excellent" if avg >= 3.4 else "good" if avg >= 2.6 else "pass" if avg >= 1.7 else "weak"
    return {"dims": dims, "band": band, "confidence": 0.5, "band_confidence": 0.5,
            "low": True, "band_low": False, "engine": "local-fallback"}


# ---------- 会话（内存镜像 + SQLite 落盘） ----------
SESSIONS: dict[str, dict] = {}


def load_session(sid: str) -> dict | None:
    if sid in SESSIONS:
        return SESSIONS[sid]
    row = store.get_session(sid)
    if not row:
        return None
    msgs = store.list_messages(sid)
    evals = store.list_evaluations(sid)
    s = {"id": sid, "type": row["type"], "total": row["total"], "resume": row["resume"], "jd": row["jd"],
         "mainQ": row["main_q"], "followup_used": row["followup_used"],
         "awaiting_followup": bool(row["awaiting_followup"]), "status": row["status"],
         "followups_total": sum(1 for e in evals if e.get("is_followup")),
         "title": row["title"], "created_at": row["created_at"],
         "history": [{"role": m["role"], "content": m["content"], "kind": m["kind"], "qno": m["qno"]}
                     for m in msgs],
         "evals": evals,
         "current": json.loads(row["current_json"] or "{}")}
    SESSIONS[sid] = s
    return s


def persist(s: dict) -> None:
    store.update_session(s["id"], main_q=s["mainQ"], followup_used=s["followup_used"],
                         awaiting_followup=1 if s["awaiting_followup"] else 0,
                         status=s.get("status", "active"),
                         current_json=json.dumps(s.get("current", {}), ensure_ascii=False))


def make_title(type_: str, jd: str) -> str:
    jd = (jd or "").strip().replace("\n", " ")
    head = jd[:14] + ("…" if len(jd) > 14 else "")
    return f"{TAGS[type_]} · {head}" if head else f"{TAGS[type_]} · 通用"


# ---------- 报告与复盘 ----------
def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def local_review(dims: list[float], evals: list[dict]) -> dict:
    """LLM 不可用时的兜底复盘（纯规则，保证报告永远出得来）。"""
    weakest = min(range(4), key=lambda i: dims[i])
    tips = {
        0: ("先复述问题再作答，开口第一句就点明结论，避免铺垫过长时间。", ),
        1: ("每个回答至少覆盖「背景 - 我的动作 - 结果」三段，缺哪段补哪段。", ),
        2: ("用「第一 / 第二 / 最后」或 STAR 起手，让结构一眼可见。", ),
        3: ("删掉重复表述，一句话只说一件事，控制在 90 秒内讲完。", ),
        4: ("每个论断配一个数字或实例，比如「周期从两周降到三天」。", ),
    }
    best = max(range(5), key=lambda i: dims[i])
    return {
        "summary": f"本场 {len(evals)} 个主问题的平均表现中，{DIM_LABELS[best]}相对最好，{DIM_LABELS[weakest]}最需要补。"
                   f"整体建议：先给结论、再补证据、最后收口到结果。",
        "highlights": [f"{DIM_LABELS[best]}维度表现最好（{dims[best]}/4），保持这个习惯。",
                       "回答中能给出具体项目与数字时，评分明显更高。"],
        "improvements": [
            {"dim": DIM_LABELS[weakest], "issue": f"{DIM_LABELS[weakest]}平均 {dims[weakest]}/4，是本场最弱项。",
             "action": tips[weakest][0]},
            {"dim": DIM_LABELS[(weakest + 1) % 4], "issue": f"{DIM_LABELS[(weakest + 1) % 4]}平均 {dims[(weakest + 1) % 4]}/4。",
             "action": tips[(weakest + 1) % 4][0]},
            {"dim": "整体节奏", "issue": "回答长度不稳定，长回答容易冗余、短回答容易丢要点。",
             "action": "统一用 60-90 秒一段的节奏：一句结论 + 两个论据 + 一个结果。"},
        ],
    }


async def gen_next(s: dict, force_followup: bool = False, tries: int = 2) -> dict:
    """出下一题。模型偶尔吐 JSON 残片或空内容，重试一次；仍不行返回 {}，由调用方兜底。"""
    for i in range(tries):
        try:
            out = await asyncio.wait_for(
                llm(build_messages(s, force_followup=force_followup),
                    temperature=0.7 if i == 0 else 0.4),
                timeout=45)
        except Exception as e:
            print(f"[warn] 出题第 {i+1} 次失败：", repr(e)[:150], flush=True)
            continue
        c = (out.get("content") or "").strip()
        if c and not c.startswith("{") and len(c) >= 6:
            return out
        print(f"[warn] 出题第 {i+1} 次内容不合法：", c[:120], flush=True)
    return {}


async def llm_review(s: dict, evals: list[dict], dims: list[float]) -> dict:
    payload = {
        "interview_type": TAGS[s["type"]],
        "main_question_count": s["mainQ"],      # 明确告诉模型主问题数，防止把追问/收尾也算进去
        "dimension_averages": dict(zip(DIM_LABELS, dims)),
        "items": [{"qno": e["qno"], "question": e["question"],
                   "answer": (e["answer"] or "")[:280],
                   "scores": dict(zip(DIM_LABELS, e["dims"])),
                   "band": e["band"]}
                  for e in evals
                  if not e["is_followup"] and e.get("dims")][:10],   # 收尾题 dims=None，不进复盘
    }
    # 复盘是本项目的核心交付物，不能靠模型"这次心情好"。实测 MiniMax 有一定概率
    # 输出非 JSON，所以给两次机会：第二次降温 + 追加硬约束，仍失败才走本地兜底。
    last_err = None
    for attempt in range(2):
        try:
            sysmsg = REVIEW_SYSTEM
            if attempt:
                sysmsg += ("\n\n【重要】上一次你没有输出合法 JSON。这次只输出一个 JSON 对象，"
                           "不要 markdown 代码块、不要任何前后说明文字。")
            out = await asyncio.wait_for(
                llm([{"role": "system", "content": sysmsg},
                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    temperature=0.4 if attempt == 0 else 0.2),
                timeout=45)
            if not isinstance(out.get("improvements"), list) or not out.get("summary"):
                raise ValueError("bad review schema: " + str(list(out.keys()))[:120])
            out["_engine"] = "llm"      # 可观测：复盘到底走没走模型
            if attempt:
                print(f"[info] 复盘在第 {attempt+1} 次尝试成功", flush=True)
            return out
        except Exception as e:
            last_err = e
            print(f"[warn] 复盘第 {attempt+1} 次失败：", repr(e)[:200], flush=True)

    print("[warn] 复盘最终走本地兜底：", repr(last_err)[:200], flush=True)
    r = local_review(dims, evals)
    r["_engine"] = "local"
    return r


async def build_report(sid: str) -> dict:
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    evals = store.list_evaluations(sid)
    main = [e for e in evals if not e["is_followup"]]
    scored = [e for e in main if not e["low"]] or main
    dims = [_mean([e["dims"][i] for e in scored]) for i in range(5)] if scored else [0.0] * 5
    overall = _mean([dims[0], dims[1], dims[2], dims[4]])
    duration = int(time.time() - s.get("created_at", time.time()))

    review = await llm_review(s, evals, dims)
    rep = {"overall": overall, "dims": dims, "main_count": s["mainQ"],
           "followup_count": sum(1 for e in evals if e["is_followup"]),
           "duration_s": duration,
           "unscored": 1 if s["mainQ"] >= s["total"] else 0,   # 收尾反问题不计分
           "summary": review.get("summary", ""),
           "highlights": review.get("highlights", [])[:3],
           "improvements": (review.get("improvements") or [])[:3],
           "engines": sorted({e["engine"] for e in evals}),
           "review_engine": review.get("_engine", "local"),
           "cost": round(sum(e.get("cost") or 0 for e in evals), 6)}
    store.save_report(sid, rep)
    store.update_session(sid, status="done")
    s["status"] = "done"
    return rep


# ---------- API ----------
app = FastAPI()


class CreateIn(BaseModel):
    type: str = "technical"
    total: int = 8
    resume: str = ""
    jd: str = ""


class AnswerIn(BaseModel):
    text: str


@app.get("/api/health")
async def health():
    import asr as _asr
    return {"ok": True, "llm": TEXT_MODEL, "jev": JEV_MODEL,
            "has_llm_key": bool(TEXT_KEY), "has_jev_key": bool(JEV_KEY),
            # 部署自检：确认进程加载的是磁盘上的最新代码，避免"改了没生效"
            "build": {"app_mtime": int(Path(__file__).stat().st_mtime),
                      "asr_mtime": int(Path(_asr.__file__).stat().st_mtime),
                      "converters": len(_asr._candidates())}}


@app.post("/api/sessions")
async def create_session(body: CreateIn):
    if body.type not in PERSONAS:
        raise HTTPException(400, "unknown interview type")
    sid = uuid.uuid4().hex[:10]
    store.create_session(sid, body.type, body.total, body.resume[:8000], body.jd[:4000],
                         make_title(body.type, body.jd))
    s = {"id": sid, "type": body.type, "total": body.total, "resume": body.resume[:8000],
         "jd": body.jd[:4000], "mainQ": 0, "followup_used": 0, "awaiting_followup": False,
         "followups_total": 0,
         "status": "active", "title": make_title(body.type, body.jd), "created_at": time.time(),
         "history": [], "evals": [], "current": {}}
    SESSIONS[sid] = s

    # 开场第一题是整场门面：模型偶尔会吐 JSON 残片或空内容，给两次机会，
    # 并且只接受"看起来是正常问句"的结果，否则用固定开场题兜底。
    q, hints = "", []
    for attempt in range(2):
        try:
            out = await asyncio.wait_for(
                llm(build_messages(s) + [{"role": "user", "content": OPENING}],
                    temperature=0.7 if attempt == 0 else 0.4),
                timeout=45)
        except Exception:
            out = {}
        c = (out.get("content") or "").strip()
        if c and not c.startswith("{") and not c.startswith('"') and len(c) >= 6:
            q, hints = c, out.get("hints") or []
            break
    if not q:
        q = "先做个自我介绍吧，重点说和这个岗位相关的部分。"
    if not isinstance(hints, list) or not hints:
        hints = ["自我介绍", "岗位匹配"]
    greeting = out.get("greeting") or f"你好，我是今天的面试官，这场是{TAGS[body.type]}，一共 {body.total} 个问题，我们开始吧。"

    s["current"] = {"content": q, "hints": hints}
    s["greeting"] = greeting
    s["history"].append({"role": "interviewer", "content": q, "kind": "question", "qno": 1})
    store.add_message(sid, "interviewer", greeting, "greeting", 0)
    store.add_message(sid, "interviewer", q, "question", 1)
    persist(s)
    return {"id": sid, "greeting": greeting, "question": {"content": q, "hints": hints},
            "qno": 1, "total": s["total"], "title": s["title"]}


@app.get("/api/sessions")
async def list_sessions():
    rows = store.list_sessions()
    return {"sessions": [{
        "id": r["id"], "title": r["title"], "type": r["type"], "tag": TAGS.get(r["type"], r["type"]),
        "total": r["total"], "mainQ": r["main_q"], "status": r["status"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "overall": r.get("overall"), "has_report": r.get("has_report"),
    } for r in rows]}


@app.get("/api/sessions/{sid}")
async def get_session_api(sid: str):
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return {"session": {"id": sid, "type": s["type"], "tag": TAGS[s["type"]], "title": s["title"],
                        "total": s["total"], "mainQ": s["mainQ"], "status": s["status"],
                        "created_at": s.get("created_at"), "resume": s["resume"], "jd": s["jd"]},
            "messages": store.list_messages(sid),
            "evals": store.list_evaluations(sid),
            "report": store.get_report(sid),
            "current": s.get("current", {})}


@app.delete("/api/sessions/{sid}")
async def delete_session_api(sid: str):
    SESSIONS.pop(sid, None)
    store.delete_session(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/answer")
async def answer(sid: str, body: AnswerIn):
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    if s.get("status") == "done":
        raise HTTPException(400, "session already finished")
    text = body.text.strip()
    s["history"].append({"role": "user", "content": text})
    is_followup = s["awaiting_followup"]
    qno = s["mainQ"] + 1
    store.add_message(sid, "user", text, "answer", qno)

    q_text = s["current"].get("content", "")
    hints = s["current"].get("hints", [])
    is_closing = qno >= s["total"]          # 最后一题是收尾反问，不该按答题量规打分

    if is_closing:
        # 收尾反问不计分，也不需要再出下一题
        ev = {"engine": "skip-closing", "dims": None, "band": "", "confidence": 0,
              "band_confidence": 0, "low": False, "band_low": False}
        nxt = {"move": "next", "content": "", "hints": []}
    else:
        # 评分与出下一题并行，互不阻塞（SPEC §3）
        ev_task = asyncio.create_task(jev_evaluate(q_text, hints, text))
        next_task = asyncio.create_task(gen_next(s))
        ev, nxt = await asyncio.gather(ev_task, next_task, return_exceptions=True)
        if isinstance(ev, Exception) or not isinstance(ev, dict) or ev.get("dims") is None:
            ev = fallback_evaluate(text, hints)
        # gen_next 重试后仍拿不到合法题目时返回 {}，这里一并兜底，绝不把空题目丢给用户
        if isinstance(nxt, Exception) or not (isinstance(nxt, dict)
                                              and (nxt.get("content") or "").strip()):
            print("[warn] 出题失败，走兜底：", repr(nxt)[:240], flush=True)
            nxt = {"move": "next", "content": "好，那换个话题——讲讲你最近做的一个关键决定，当时是怎么取舍的？",
                   "hints": ["决定", "取舍", "结果"]}

    ev.update({"qno": qno, "is_followup": is_followup, "question": q_text,
               "hints": hints, "answer": text,
               # 过短的回答即使被打出分数也不可靠，明确告知用户"仅供参考"
               "too_short": len(text) < SHORT_ANSWER_CHARS})
    s["evals"].append(ev)
    if ev.get("engine") != "skip-closing":
        store.add_evaluation(sid, ev)

    if isinstance(nxt, Exception):
        nxt = {"move": "next", "content": "好，我们下一题。能再讲讲你最近做的一个决定吗？",
               "hints": ["决定", "理由", "结果"]}
    move = nxt.get("move") or "next"

    # 后端确定性兜底：实测 MiniMax 几乎从不主动选 followup（整场 0 次追问）。
    # 所以由 JEV 分数来裁决——切题/完整/具体任一维度过低，就强制补一轮追问。
    d = ev.get("dims")
    if (d and not is_closing and s["followup_used"] < 1
            and s["mainQ"] < s["total"] - 1
            and s.get("followups_total", 0) < 3
            and min(d[0], d[1], d[4]) < 2.4
            and move != "followup"):
        try:
            f = await gen_next(s, force_followup=True)
        except Exception:
            f = {}
        # 关键：gen_next 失败返回 {}（不抛异常），若不检查就把空追问发给前端，
        # 用户会看到"第 n 题·追问"气泡里空空如也，像卡死（用户实测踩到）。
        # 失败则放弃这轮追问，沿用刚才兜底出的下一题。
        if (f.get("content") or "").strip():
            nxt = f
            move = f.get("move") or "followup"

    # 上限必须在这里再判一次：上面那段只在「模型没主动追问」时检查过总数，
    # 模型自己选 followup 时会绕过限制，导致整场追问失控。
    # 最后一道保险：任何路径都不允许把空题目/空追问发给前端
    if not (nxt.get("content") or "").strip():
        nxt = {"move": "next", "content": "好，我们继续。能再讲讲你最近做的一个关键决定吗？",
               "hints": ["决定", "理由", "结果"]}
        if move == "followup":
            move = "next"
    if (move == "followup" and s["followup_used"] < 1 and s["mainQ"] < s["total"] - 1
            and s.get("followups_total", 0) < MAX_FOLLOWUPS):
        s["followup_used"] = 1
        s["awaiting_followup"] = True
        s["followups_total"] = s.get("followups_total", 0) + 1
        kind = "followup"
    else:
        s["mainQ"] += 1
        s["followup_used"] = 0
        s["awaiting_followup"] = False
        kind = "closing" if s["mainQ"] >= s["total"] else "next"

    if s["mainQ"] >= s["total"]:          # 题量用尽，不再出新题
        kind = "done"
        s["status"] = "closing"
        s["current"] = {"content": "", "hints": []}
    else:
        new_q = nxt.get("content", "")
        # 防复读：实测模型偶尔把上一题/追问原样再问一遍（尤其回答雷同时）。
        # 与上一条面试官消息一字不差就换固定题，绝不复读。
        last_q = next((m["content"] for m in reversed(s["history"])
                       if m["role"] == "interviewer"), "")
        if new_q.strip() and last_q.strip() and new_q.strip() == last_q.strip():
            print("[warn] 模型复读上一题，换兜底题", flush=True)
            new_q = "好，那我们换个角度——聊聊你最近一年最有成就感的一件事，以及它难在哪里？"
            nxt = {"move": "next", "content": new_q, "hints": ["事件", "难点", "结果"]}
            move = "next"
        s["current"] = {"content": new_q, "hints": nxt.get("hints", [])}
        s["history"].append({"role": "interviewer", "content": s["current"]["content"],
                             "kind": kind, "qno": s["mainQ"] + 1})
        store.add_message(sid, "interviewer", s["current"]["content"], kind, s["mainQ"] + 1)
    persist(s)

    return {"evaluation": ev,
            "next": {"kind": kind, "content": s["current"]["content"],
                     "hints": s["current"]["hints"], "qno": min(s["mainQ"] + 1, s["total"])},
            "progress": {"mainQ": s["mainQ"], "total": s["total"]}}


@app.post("/api/sessions/{sid}/finish")
async def finish(sid: str):
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    rep = await build_report(sid)
    return {"report": rep}


@app.get("/api/sessions/{sid}/report")
async def report(sid: str):
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    rep = store.get_report(sid)
    if not rep:
        rep = await build_report(sid)
    return {"session": {"id": sid, "type": s["type"], "tag": TAGS[s["type"]], "title": s["title"],
                        "total": s["total"], "mainQ": s["mainQ"], "status": s["status"]},
            "report": rep,
            "evals": store.list_evaluations(sid),
            "messages": store.list_messages(sid)}


@app.post("/api/asr")
async def asr(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(413, "audio too large")
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    try:
        out = await asyncio.wait_for(asr_transcribe(data, suffix), timeout=180)
        return {"text": out["text"], "asr_seconds": out["asr_seconds"]}
    except Exception as e:
        return JSONResponse({"text": "", "error": str(e)[:200]}, status_code=500)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB)), name="web")
