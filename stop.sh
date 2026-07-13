#!/bin/bash
# ============================================================
# 贾维斯桌面终端 — 总关闭脚本（桌面应用 + 后端 7899）
#
# 用法：
#   ./stop.sh                   # 关桌面应用 + 后端（先 SIGTERM，8s 超时 SIGKILL）
#   JARVIS_PORT=7899 ./stop.sh  # 自定义后端端口（默认 7899）
#
# 行为说明（幂等，未运行不报错）：
#   1. 桌面应用：按项目路径精确匹配（本项目 dev Electron / 打包版
#      JARVIS Terminal.app / concurrently+vite），不会误杀 Cursor 等
#      其它 Electron 应用
#   2. 后端：按「端口监听者」定位（能抓住 PPID=1 的孤儿残留进程），
#      pid 文件仅作辅助清理
# ============================================================

set -u

PORT="${JARVIS_PORT:-7899}"
RUN_DIR="$HOME/.vibe-trading"
KILL_WAIT=8   # SIGTERM 后等待秒数，超时升级 SIGKILL

# 向一组 PID 先发 SIGTERM，超时未退再 SIGKILL；$1=描述 $2...=pids
graceful_kill() {
  local desc="$1"; shift
  local pids=("$@")
  [ "${#pids[@]}" -eq 0 ] && return 0
  echo "→ 关闭${desc}：PID ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null
  for _ in $(seq 1 $((KILL_WAIT * 2))); do
    local alive=0
    for p in "${pids[@]}"; do
      kill -0 "$p" 2>/dev/null && alive=1
    done
    [ "$alive" = "0" ] && { echo "  ✓ ${desc}已退出"; return 0; }
    sleep 0.5
  done
  echo "  ⚠ ${desc}${KILL_WAIT}s 内未退出，SIGKILL 强制终止"
  kill -KILL "${pids[@]}" 2>/dev/null
  return 0
}

collect() {  # 按 pgrep -f 模式收集 PID（无匹配输出空）
  pgrep -f "$1" 2>/dev/null || true
}

# ---------- 1. 桌面应用 ----------
# dev 形态：concurrently → (vite, electron)；只匹配本项目路径，先杀父进程组
desktop_pids=()
while IFS= read -r p; do [ -n "$p" ] && desktop_pids+=("$p"); done < <(
  {
    collect "Vibe-Trading/desktop/node_modules/.bin/concurrently"
    collect "Vibe-Trading/desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    collect "JARVIS Terminal.app/Contents/MacOS/JARVIS Terminal"
  } | sort -un
)
if [ "${#desktop_pids[@]}" -gt 0 ]; then
  graceful_kill "桌面应用" "${desktop_pids[@]}"
else
  echo "✓ 桌面应用未在运行"
fi

# dev 残留的 vite / esbuild（concurrently 被杀后一般会带走，兜底再扫一次）
sleep 1
vite_pids=()
while IFS= read -r p; do [ -n "$p" ] && vite_pids+=("$p"); done < <(
  {
    collect "Vibe-Trading/desktop/node_modules/.bin/vite"
    collect "Vibe-Trading/desktop/node_modules/@esbuild"
  } | sort -un
)
[ "${#vite_pids[@]}" -gt 0 ] && graceful_kill "vite/esbuild 残留" "${vite_pids[@]}"

# ---------- 2. 后端（端口监听者定位，覆盖孤儿进程） ----------
backend_pids=()
while IFS= read -r p; do [ -n "$p" ] && backend_pids+=("$p"); done < <(
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -un
)
if [ "${#backend_pids[@]}" -gt 0 ]; then
  graceful_kill "后端（端口 ${PORT}）" "${backend_pids[@]}"
else
  echo "✓ 后端（端口 ${PORT}）未在运行"
fi

# ---------- 3. 清理 pid 文件 ----------
rm -f "$RUN_DIR/dashboard-${PORT}.pid" "$RUN_DIR/desktop-dev.pid" 2>/dev/null

echo "✓ 关闭流程完成"
