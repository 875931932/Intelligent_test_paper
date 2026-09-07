#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

stop_one() {
  local name="$1"
  local file="$ROOT_DIR/var/run/$name.pid"
  [[ -f "$file" ]] || return 0
  local pid
  pid="$(cat "$file")"
  # pid 文件损坏（非纯数字）时只清理文件，避免误杀其他进程
  [[ "$pid" =~ ^[0-9]+$ ]] || { rm -f "$file"; echo "removed invalid pid file for $name"; return 0; }
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$file"
    echo "$name already stopped"
    return 0
  fi

  # 优雅关闭：SIGTERM 后等待进程真正退出。知识目录构建的 LLM 调用可能长达
  # 数分钟，uvicorn 收到 SIGTERM 会等请求结束才退出；若不等待就启动新进程，
  # 8000 端口未释放会导致新进程因 Address already in use 启动失败，
  # 最终仍是旧代码在服务。这里给 30 秒宽限，超时则强制 SIGKILL。
  kill "$pid" 2>/dev/null || true
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 30 ]]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$name (pid $pid) did not stop gracefully, force killing"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$file"
  echo "stopped $name"
}

for name in worker api; do
  stop_one "$name"
done
