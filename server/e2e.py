"""端到端冒烟：真实调用 LLM + JEV，走完一整场，并校验持久化与复盘报告。

用法：
  python e2e.py            # phase1：创建 + 答完 + 生成报告（真实花钱，约 $0.0003）
  python e2e.py --verify   # phase2：重启服务后校验数据仍在（不花钱）
  python e2e.py --asr      # 语音链路：用 macOS say 合成中文语音 → /api/asr
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8890"
STATE = Path(__file__).parent.parent / "data" / "e2e_state.json"

# 绕过本机 HTTP 代理（否则 127.0.0.1 会被代理拦成 502）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(path: str, body: dict | None = None, method: str | None = None, raw: bytes | None = None,
        ctype: str = "application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    m = method or ("POST" if data is not None else "GET")
    r = opener.open(urllib.request.Request(BASE + path, data=data, method=m,
                                           headers={"Content-Type": ctype}), timeout=180)
    return json.loads(r.read())


ANSWERS = [
    "我在上一家负责 agent 技术中台的规划与落地。背景是投研、风控、合规多个团队各自接模型、重复造轮子，我把它收敛成四层架构：应用层跑业务Agent，平台层收数据工程、模型工程和Agent工程，资产层沉淀记忆库、工具库和编排模板，最下面是数据层。结果是新业务接入从两周缩短到三天。",
    "最难的权衡是平台该做多厚。一开始想把评测、观测都收进来，但业务方抱怨接入成本高，我改成只收三件必须复用的能力：工具调用、上下文管理、效果评测，其余交给业务自己。代价是平台初期看起来比较薄，好处是三个月内接入了六个团队。",
    "失败过一次。第一版记忆设计用了全量对话存储，结果上下文超长、成本翻倍，上线两周就回滚了。复盘后改成按任务切片加摘要，长对话成本降了大概六成。这件事让我意识到平台能力必须先量成本再谈体验。",
    "我会先看业务是不是重复发生。判断标准有三个：是不是三个以上团队都要做、是不是和业务流程解耦、是不是有明确的复用接口。三条都满足才进平台，否则就让业务自己先跑，跑通再抽象。",
    "我做过多轮取舍。去年有两个方向：继续做平台能力，还是直接做一个投研Agent拿业务结果。我选了前者，因为当时平台是瓶颈，业务侧已经有人在做。判断依据是接入周期这个指标，两周说明瓶颈在平台。",
    "团队协作上，我每周和工程、业务各开一次对齐会，把平台路线图公开出去，让业务方知道三个月后能拿到什么。冲突主要来自排期，我用接入优先级排序而不是谁嗓门大。",
    "我理解的AI产品经理，核心不是写prompt，而是定义问题边界和验收标准。我会先和业务一起定什么叫效果好，再倒推需要哪些能力，最后才谈模型选型。",
    "我想问的是，你们现在平台团队和业务团队的边界是怎么划的？以及如果业务方坚持要定制能力，团队一般怎么处理？",
]

RESUME = ("尹小璐 · AI 产品经理 / 金融资管 AI 技术平台架构\n"
          "主导 agent 技术中台建设（L3 四层架构：应用层/平台层/资产层/数据层），"
          "新业务接入周期从两周缩短到三天；清华 MBA 在读。")
JD = "AI 产品经理（Agent 平台方向）：负责企业级 Agent 平台规划落地，定义平台与业务边界，建立交付效果测评体系。"


def phase1():
    t0 = time.time()
    s = req("/api/sessions", {"type": "technical", "total": 8, "resume": RESUME, "jd": JD})
    sid = s["id"]
    print(f"[1] 创建会话 {sid} · {s['title']}")
    print(f"    开场：{s['greeting'][:50]}")
    print(f"    Q1：{s['question']['content'][:60]}")

    costs, bands, kinds = [], [], []
    i = 0
    while True:
        ans = ANSWERS[i % len(ANSWERS)]
        d = req(f"/api/sessions/{sid}/answer", {"text": ans})
        ev = d["evaluation"]
        costs.append(ev.get("cost") or 0)
        kinds.append(d["next"]["kind"])
        if ev.get("band"):
            bands.append(ev["band"])
        print(f"[2.{i+1}] {d['next']['kind']:<8} 分数 {ev['dims']} 档位 {ev['band'] or '-':<9} "
              f"置信 {ev['confidence']} 引擎 {ev['engine']}")
        i += 1
        if d["next"]["kind"] == "done" or i > 16:
            break

    rep = req(f"/api/sessions/{sid}/finish", {})["report"]
    print(f"[3] 报告：总分 {rep['overall']} · 维度 {rep['dims']} · "
          f"主问题 {rep['main_count']} 追问 {rep['followup_count']}")
    print(f"    复盘：{rep['summary'][:80]}…")
    for x in rep["improvements"]:
        print(f"    - [{x.get('dim')}] {x.get('issue','')[:40]} → {x.get('action','')[:40]}")
    print(f"    亮点：{rep['highlights']}")

    lst = req("/api/sessions")["sessions"]
    print(f"[4] 历史列表 {len(lst)} 场，最新：{lst[0]['title']} 总分 {lst[0]['overall']} 状态 {lst[0]['status']}")

    STATE.write_text(json.dumps({"sid": sid, "elapsed": round(time.time() - t0, 1),
                                 "cost": round(sum(costs), 6)}, ensure_ascii=False), encoding="utf-8")
    print(f"[5] 总耗时 {round(time.time()-t0,1)}s · JEV 成本 ${sum(costs):.6f} · 覆盖档位 {sorted(set(bands))}")
    print("OK — phase1 完成，state 已写入", STATE.name)


def verify():
    st = json.loads(STATE.read_text(encoding="utf-8"))
    sid = st["sid"]
    d = req(f"/api/sessions/{sid}")
    ls = req("/api/sessions")["sessions"]
    row = next((x for x in ls if x["id"] == sid), None)
    print(f"[v1] 重启后读取会话 {sid}：消息 {len(d['messages'])} 条 · 评分 {len(d['evals'])} 条 · 状态 {d['session']['status']}")
    print(f"[v2] 历史列表命中：{row['title']} 总分 {row['overall']} 有报告 {row['has_report']}")
    r = req(f"/api/sessions/{sid}/report")["report"]
    print(f"[v3] 报告持久化：总分 {r['overall']} · 复盘 {len(r['improvements'])} 条 · 摘要 {len(r['summary'])} 字")
    assert len(d["messages"]) >= 8, "消息未持久化"
    assert len(d["evals"]) >= 6, "评分未持久化"
    assert all(e["engine"] != "skip-closing" for e in d["evals"]), "收尾题不应计分入库"
    assert r["overall"] > 0, "报告总分异常"
    assert len(r["improvements"]) == 3, "复盘建议应为 3 条"
    assert row["has_report"], "历史列表未标记报告"
    print("OK — 持久化与报告校验通过")


def multipart(field: str, filename: str, data: bytes, ctype: str) -> tuple[bytes, str]:
    b = "----icboundary"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def asr_test():
    wav = Path("/tmp/ic_asr.aiff")
    text = "我在上一家负责agent技术中台的建设，把业务接入周期从两周缩短到三天。"
    subprocess.run(["say", "-v", "Tingting", "-o", str(wav), text], check=True)
    body, ctype = multipart("file", "answer.aiff", wav.read_bytes(), "audio/aiff")
    d = req("/api/asr", raw=body, ctype=ctype)
    print(f"[asr] 原文：{text}")
    print(f"[asr] 识别：{d.get('text')}")
    print(f"[asr] 耗时：{d.get('asr_seconds')}s")
    assert d.get("text"), "转写为空"
    print("OK — 语音链路通过")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    elif "--asr" in sys.argv:
        asr_test()
    else:
        phase1()
