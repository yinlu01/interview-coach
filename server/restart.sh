#!/usr/bin/env bash
# 重启后端，并确保进程加载的是磁盘上的最新代码。
#
# 踩过的坑：直接 pkill 后立刻启动，旧进程往往还没释放端口，
# 新进程因 "address already in use" 悄悄退出，于是服务一直在跑旧代码，
# 看起来"改了没生效"。这里改成：杀 → 轮询端口直到真正空闲 → 启动 → 校验 mtime。
set -euo pipefail

PORT="${PORT:-8800}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-/Users/yinlu01/.workbuddy/binaries/python/envs/default/bin/python}"
LOG="$(cd "$HERE/.." && pwd)/data/server.log"

mkdir -p "$(dirname "$LOG")"

echo "→ 停止旧进程"
pkill -f "uvicorn app:app" 2>/dev/null || true
# 兜底：按端口占用者强杀
for i in $(seq 1 15); do
  PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  [ -z "$PID" ] && break
  kill -9 "$PID" 2>/dev/null || true
  sleep 1
done

echo "→ 等待端口 $PORT 释放"
for i in $(seq 1 15); do
  if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 1
done
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ 端口 $PORT 仍被占用，放弃"; exit 1
fi

if [ "${NO_START:-0}" = "1" ]; then
  echo "✓ 端口已释放（NO_START=1，交给外部拉起进程）"
  exit 0
fi

echo "→ 启动（日志 $LOG）"
cd "$HERE"
nohup "$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
disown 2>/dev/null || true

echo "→ 等待就绪并校验代码版本"
DISK="$(stat -f "%m" "$HERE/app.py")"
for i in $(seq 1 40); do
  BODY="$(curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/health" || true)"
  if [ -n "$BODY" ]; then
    RUN="$(printf '%s' "$BODY" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["build"]["app_mtime"])' 2>/dev/null || echo 0)"
    if [ "$RUN" = "$DISK" ]; then
      echo "✓ 已就绪 http://127.0.0.1:$PORT  （代码版本匹配 $DISK）"
      exit 0
    fi
    echo "  …进程已起但加载的是旧代码（进程 $RUN ≠ 磁盘 $DISK），继续等"
  fi
  sleep 1
done

echo "✗ 启动超时或版本不匹配"
tail -20 "$LOG"
exit 1
