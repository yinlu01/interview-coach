"""本地语音转文字：浏览器录音 → 16k 单声道 wav → mlx-whisper（Apple Silicon）。

一期口径：只做 ≤2 分钟的短语音；不自动发送，文字回填输入框由用户确认。

踩坑记录（别改回去）：
- 启动时"预检"ffmpeg（跑 -version 再决定用哪个）不可靠：预检失败会静默退化成
  裸 'ffmpeg'，而本机 PATH 上没有 ffmpeg，最终 FileNotFoundError。
  正确做法是**转写时逐个候选真跑转换**，谁跑通用谁。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

# 模型已在本机缓存。默认离线加载：联网时 HF 请求走本机代理会 502，白等十几秒。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

AUDIO_DIR = Path(__file__).parent.parent / "data" / "audio"
if not AUDIO_DIR.exists():
    AUDIO_DIR.mkdir(parents=True)

MODEL = "mlx-community/whisper-large-v3-turbo"

SITE_PKG = ("/Users/yinlu01/.workbuddy/binaries/python/envs/default/lib/"
            "python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1")


def _candidates() -> list[str]:
    """所有可能可用的转码器，按可靠性排序（去重、只保留存在的）。"""
    out: list[str] = []
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p:
            out.append(p)
    except Exception:
        pass
    out += [SITE_PKG, "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg", shutil.which("ffmpeg") or ""]
    seen, res = set(), []
    for c in out:
        if c and c not in seen and Path(c).exists():
            seen.add(c)
            res.append(c)
    return res


def _patch_path() -> None:
    """把 ffmpeg 所在目录注入 PATH。

    关键坑：mlx-whisper 内部自己 subprocess 调裸 'ffmpeg' 来读音频，
    不走我们的 _convert。本机 PATH 上没有 ffmpeg → FileNotFoundError: 'ffmpeg'。
    所以光自己找到 ffmpeg 不够，必须让它也能找到。
    """
    cur = os.environ.get("PATH", "")
    for c in _candidates():
        d = str(Path(c).parent)
        if d not in cur.split(":"):
            cur = f"{cur}:{d}" if cur else d
    os.environ["PATH"] = cur


_patch_path()


def _convert(src: Path, wav: Path) -> tuple[str | None, str]:
    """逐个候选真跑转换；返回 (成功的转码器, 全部失败原因)。"""
    errs = []
    for c in _candidates():
        try:
            if wav.exists():
                wav.unlink()
            r = subprocess.run([c, "-y", "-loglevel", "error", "-i", str(src),
                                "-ar", "16000", "-ac", "1", str(wav)],
                               capture_output=True, timeout=90)
            if wav.exists() and wav.stat().st_size > 1000:
                return c, ""
            errs.append(f"{Path(c).name}: rc={r.returncode} "
                        + (r.stderr or b"").decode("utf-8", "ignore").strip()[-120:])
        except Exception as e:
            errs.append(f"{Path(c).name}: {type(e).__name__} {e}")
    return None, " | ".join(errs)


def _run(data: bytes, suffix: str) -> dict:
    import mlx_whisper

    uid = uuid.uuid4().hex[:10]
    src = AUDIO_DIR / f"{uid}{suffix}"
    wav = AUDIO_DIR / f"{uid}.wav"
    src.write_bytes(data)

    used, err = _convert(src, wav)
    if not used:
        # 转换失败就不要拿原始文件硬喂 whisper（webm/aiff 它读不了，会静默返回空）
        for p in (src, wav):
            try:
                p.unlink()
            except OSError:
                pass
        raise RuntimeError(f"音频转码失败（已试 {len(_candidates())} 个转码器）：{err}")

    t0 = time.time()
    try:
        out = mlx_whisper.transcribe(str(wav), path_or_hf_repo=MODEL, language="zh",
                                     initial_prompt="以下是一段中文面试回答的语音。")
    except Exception:                      # 离线失败（缓存缺失）时退回联网拉取一次
        os.environ.pop("HF_HUB_OFFLINE", None)
        out = mlx_whisper.transcribe(str(wav), path_or_hf_repo=MODEL, language="zh",
                                     initial_prompt="以下是一段中文面试回答的语音。")
    text = (out.get("text") or "").strip()
    dur = round(time.time() - t0, 2)

    for p in (src, wav):
        try:
            p.unlink()
        except OSError:
            pass
    return {"text": text, "asr_seconds": dur, "converter": Path(used).name}


async def transcribe(data: bytes, suffix: str = ".webm") -> dict:
    """在线程池里跑，避免阻塞事件循环（转录通常 1-10s）。"""
    return await asyncio.to_thread(_run, data, suffix)
