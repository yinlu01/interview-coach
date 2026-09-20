"""全量验收：三种面试人格各跑一场真实面试 + 持久化 + 报告 + 删除 + 语音。

用法：
  python acceptance.py          # 全量（真实花钱，约 $0.001）
  python acceptance.py --quick  # 只跑结构校验，不建新会话
  python acceptance.py --asr    # 只跑语音链路
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8821"
STATE = Path(__file__).parent.parent / "data" / "acceptance.json"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESUME = ("尹小璐 · AI 产品经理 / 金融资管 AI 技术平台架构\n"
          "主导 agent 技术中台建设（L3 四层架构：应用层/平台层/资产层/数据层），"
          "新业务接入周期从两周缩短到三天；推进大模型安全网关与数据分级加密传输落地；清华 MBA 在读。")
JD = "AI 产品经理（Agent 平台方向）：负责企业级 Agent 平台规划落地，定义平台与业务边界，建立交付效果测评体系。"

# 每类面试给一组"有梯度"的回答：前几题答得好，中间故意答得弱（触发追问），最后一题反问
GOOD = ("我在上一家负责 agent 技术中台的规划与落地。背景是投研、风控、合规多个团队各自接模型、"
        "重复造轮子，我把它收敛成四层架构：应用层跑业务 Agent，平台层收数据工程、模型工程和 Agent 工程，"
        "资产层沉淀记忆库、工具库和编排模板，最下面是数据层。结果是新业务接入从两周缩短到三天。")
WEAK = "做过一些。"          # 故意极短 → 应触发低置信门控 + 强制追问
MID = ("我做过权衡。当时考虑过把评测和观测都收进平台，但业务方反馈接入成本太高，"
       "最后只保留工具调用、上下文管理、效果评测三项必须复用的能力，其余交回业务。")
CLOSING = "我想问一下，你们平台团队和业务团队的边界是怎么划的？"

PLAN = {
    "technical":  [GOOD, MID, WEAK, MID, GOOD, MID, GOOD, CLOSING],
    "hr":         [GOOD, MID, WEAK, GOOD, MID, GOOD, MID, CLOSING],
    "executive":  [GOOD, MID, GOOD, WEAK, MID, GOOD, MID, CLOSING],
}

FAILS: list[str] = []


def check(cond: bool, msg: str):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS.append(msg)


def req(path: str, body: dict | None = None, method: str | None = None,
        raw: bytes | None = None, ctype: str = "application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    m = method or ("POST" if data is not None else "GET")
    r = opener.open(urllib.request.Request(BASE + path, data=data, method=m,
                                           headers={"Content-Type": ctype}), timeout=180)
    return json.loads(r.read())


def run_one(itype: str, total: int = 8) -> dict:
    print(f"\n=== {itype}（{total} 题）===")
    t0 = time.time()
    s = req("/api/sessions", {"type": itype, "total": total, "resume": RESUME, "jd": JD})
    sid = s["id"]
    check(bool(s.get("greeting")), f"开场白非空：{s['greeting'][:34]}…")
    check(bool(s["question"]["content"]), f"第 1 题非空：{s['question']['content'][:34]}…")

    evals, kinds, costs, questions = [], [], [], [s["question"]["content"]]
    i = 0
    while True:
        ans = PLAN[itype][i % len(PLAN[itype])]
        d = req(f"/api/sessions/{sid}/answer", {"text": ans})
        ev, nxt = d["evaluation"], d["next"]
        evals.append(ev)
        kinds.append(nxt["kind"])
        costs.append(ev.get("cost") or 0)
        if nxt.get("content"):
            questions.append(nxt["content"])
        dim = ev.get("dims")
        print(f"  Q{ev['qno']} {nxt['kind']:<8} "
              f"{('分数 ' + str([round(x,1) for x in dim]) if dim else '（收尾题不计分' + '）'):<30} "
              f"档位 {ev.get('band') or '-':<8} 置信 {ev.get('confidence')} 引擎 {ev.get('engine')}")
        i += 1
        if nxt["kind"] == "done" or i > 20:
            break

    # 题量控制
    main_answers = sum(1 for k in kinds if k != "followup")
    check(main_answers == total, f"主问题数为 {total}（实际 {main_answers}）")
    check(kinds[-1] == "done", "最后一步为 done")
    check(sum(1 for k in kinds if k == "followup") <= 3, "追问次数 ≤ 3")
    check(any(e.get("engine") == "skip-closing" for e in evals), "收尾反问题未计分")

    # 回归：模型偶尔把 JSON 示例原样吐出来（{{"greeting":…}}），绝不能当成题目显示
    junk = [q for q in questions if q.strip().startswith("{") or "{{" in q]
    check(not junk, f"所有题目均为正常问句（{len(questions)} 题，无 JSON 残片）")
    check(all(len(q.strip()) >= 6 for q in questions), "无空题目")

    # 极短回答：必须如实打低分，且被打上 too_short 标记（提示用户补内容）。
    # 注意不能断言 low=True——JEV 对"答得很短"往往很确定，置信度反而高。
    weak_ev = [e for e in evals if e.get("dims") and e.get("answer") == WEAK]
    if weak_ev:
        e0 = weak_ev[0]
        check(min(e0["dims"]) < 2.5, "极短回答如实低分：各维 "
              + str([round(x, 1) for x in e0["dims"]]))
        check(bool(e0.get("too_short")), "极短回答标记 too_short")
    else:
        check(False, "未找到极短回答的评分记录")

    rep = req(f"/api/sessions/{sid}/finish", {})["report"]
    check(rep["overall"] > 0, f"报告总分 {rep['overall']}")
    check(len(rep["dims"]) == 5, f"五维均分 {[round(x,2) for x in rep['dims']]}")
    check(len(rep["improvements"]) == 3, "复盘建议 3 条")
    check(all(x.get("issue") and x.get("action") for x in rep["improvements"]), "每条建议含问题与行动")
    check(len(rep["summary"]) >= 30, f"总体评价 {len(rep['summary'])} 字")
    check(len(rep.get("highlights") or []) >= 1, f"亮点 {len(rep.get('highlights') or [])} 条")
    # 复盘必须由模型生成：走本地兜底时三场文案会一模一样，这里是关键防线
    check(rep.get("review_engine") == "llm",
          f"复盘由 LLM 生成（engine={rep.get('review_engine')}）")
    print(f"    总体评价：{rep['summary'][:70]}…")
    for x in rep["improvements"]:
        print(f"    · [{x.get('dim')}] {x.get('issue','')[:36]} → {x.get('action','')[:40]}")

    el = round(time.time() - t0, 1)
    print(f"  用时 {el}s · JEV 成本 ${sum(costs):.6f}")
    return {"sid": sid, "type": itype, "elapsed": el, "cost": round(sum(costs), 6),
            "overall": rep["overall"], "followups": sum(1 for k in kinds if k == "followup"),
            "kinds": kinds, "summary": rep.get("summary", "")}


def persistence(runs: list[dict]):
    print("\n=== 持久化 ===")
    for r in runs:
        d = req(f"/api/sessions/{r['sid']}")
        ev = req(f"/api/sessions/{r['sid']}/report")
        n_msg, n_ev = len(d["messages"]), len(d["evals"])
        check(n_msg >= 9, f"{r['type']} 消息落库 {n_msg} 条")
        check(n_ev >= 7, f"{r['type']} 评分落库 {n_ev} 条")
        check(d["session"]["status"] == "done" or d["session"]["status"] == "closing",
              f"{r['type']} 状态 {d['session']['status']}")
        rep = ev["report"]
        check(rep["overall"] > 0, f"{r['type']} 报告可回读（总分 {rep['overall']}）")
        check(all(e.get("engine") != "skip-closing" for e in d["evals"]),
              f"{r['type']} 收尾题未写进评分表")


def history_and_delete(runs: list[dict]):
    print("\n=== 历史列表与删除 ===")
    lst = req("/api/sessions")["sessions"]
    check(len(lst) >= len(runs), f"历史列表 {len(lst)} 场")
    check(all(x.get("title") for x in lst[:5]), "每场有标题")
    done = [x for x in lst if x.get("has_report")]
    check(len(done) >= len(runs), f"已完成并带报告 {len(done)} 场")

    victim = runs[-1]["sid"]
    req(f"/api/sessions/{victim}", method="DELETE")
    lst2 = req("/api/sessions")["sessions"]
    check(all(x["id"] != victim for x in lst2), "删除后不再出现在列表")
    try:
        req(f"/api/sessions/{victim}")
        check(False, "删除后详情应 404")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "删除后详情返回 404")


def asr_test():
    print("\n=== 语音转写 ===")
    wav = Path("/tmp/ic_asr.aiff")
    text = "我在上一家负责agent技术中台的建设，把业务接入周期从两周缩短到三天。"
    subprocess.run(["say", "-v", "Tingting", "-o", str(wav), text], check=True)
    b = "----icb"
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.aiff"\r\n'
            f"Content-Type: audio/aiff\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{b}--\r\n".encode()
    d = req("/api/asr", raw=body, ctype=f"multipart/form-data; boundary={b}")
    print(f"  原文：{text}")
    print(f"  识别：{d.get('text')}  ({d.get('asr_seconds')}s)")
    check(bool(d.get("text")), "转写非空")
    check("中台" in (d.get("text") or ""), "中文识别准确")


def bad_input():
    print("\n=== 异常输入 ===")
    try:
        req("/api/sessions", {"type": "unknown", "total": 8, "resume": "", "jd": ""})
        check(False, "未知面试类型应 400")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "未知面试类型返回 400")
    s = req("/api/sessions", {"type": "technical", "total": 8, "resume": "", "jd": ""})
    check(bool(s["question"]["content"]), "空简历/JD 也能出题（降级为通用题）")
    req(f"/api/sessions/{s['id']}", method="DELETE")
    try:
        req("/api/sessions/nonexistent-id")
        check(False, "不存在的会话应 404")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "不存在的会话返回 404")


def main():
    t0 = time.time()
    print("=== health ===")
    h = req("/api/health")
    check(h["ok"] and h["has_llm_key"] and h["has_jev_key"],
          f"LLM {h['llm']} · JEV {h['jev']} · key 已配置")

    runs = [run_one(t) for t in ("technical", "hr", "executive")]
    # 跨场校验：三场的复盘文案若一字不差，说明模型没参与、走了模板
    summaries = [r.get("summary", "") for r in runs]
    check(len(set(summaries)) == len(summaries), "三场复盘文案各不相同（非模板套话）")
    persistence(runs)
    history_and_delete(runs)
    asr_test()
    bad_input()

    STATE.write_text(json.dumps({"runs": runs, "elapsed": round(time.time() - t0, 1),
                                 "cost": round(sum(r["cost"] for r in runs), 6)},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{'='*44}")
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print(f"✅ 全部通过 · 3 场真实面试 · 总耗时 {round(time.time()-t0,1)}s")


if __name__ == "__main__":
    if "--asr" in sys.argv:
        asr_test()
    else:
        main()
