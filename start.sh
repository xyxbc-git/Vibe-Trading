#!/bin/bash
# ============================================================
# 贾维斯桌面终端 — 总启动脚本（后端 7899 + Electron 桌面应用）
#
# 用法：
#   ./start.sh                  # 一键拉起后端 + 桌面应用（已在运行的部分自动复用）
#   ./start.sh --backend-only   # 只拉后端
#   ./start.sh --restart        # 先执行 stop.sh 再全新启动
#   JARVIS_PORT=7899 ./start.sh # 自定义后端端口（默认 7899）
#
# 行为说明：
#   - 后端已在监听 → 不重复拉起，直接复用（想强制重启用 --restart）
#   - 后端日志： ~/.vibe-trading/dashboard-<端口>.log
#   - 健康检查通过（/api/wallet 返回 200）后才拉桌面应用
#   - 桌面应用优先用打包版 dist/mac*/JARVIS Terminal.app，没有则回退
#     dev 模式（npm run dev，日志 ~/.vibe-trading/desktop-dev.log）
#   - 注：Electron 主进程自身也会 spawn 一个后端（electron/main.mjs 的
#     startPythonBackend）。脚本已先占住端口，那个重复进程会因
#     「Address already in use」立即退出，属预期无害行为。
# ============================================================

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${JARVIS_PORT:-7899}"
RUN_DIR="$HOME/.vibe-trading"
BACKEND_LOG="$RUN_DIR/dashboard-${PORT}.log"
BACKEND_PID_FILE="$RUN_DIR/dashboard-${PORT}.pid"
DESKTOP_LOG="$RUN_DIR/desktop-dev.log"
DESKTOP_PID_FILE="$RUN_DIR/desktop-dev.pid"
PYTHON="$ROOT/.venv/bin/python"
HEALTH_URL="http://127.0.0.1:${PORT}/api/wallet"

BACKEND_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --backend-only) BACKEND_ONLY=1 ;;
    --restart)      "$ROOT/stop.sh"; echo "" ;;
  esac
done

mkdir -p "$RUN_DIR"

# ---------- 1. 后端（FastAPI :${PORT}） ----------
backend_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [ -n "$backend_pid" ]; then
  echo "✓ 后端已在运行（PID ${backend_pid}，端口 ${PORT}），复用现有实例"
else
  if [ ! -x "$PYTHON" ]; then
    echo "✗ 未找到虚拟环境 Python：${PYTHON}（先在 Vibe-Trading 下创建 .venv）"
    exit 1
  fi
  echo "→ 启动后端：jarvis_dashboard.py --port ${PORT}（日志 ${BACKEND_LOG}）"
  cd "$ROOT" || exit 1
  nohup "$PYTHON" jarvis_dashboard.py --port "$PORT" >> "$BACKEND_LOG" 2>&1 &
  backend_pid=$!
  echo "$backend_pid" > "$BACKEND_PID_FILE"
  echo "  后端 PID ${backend_pid}（已写入 ${BACKEND_PID_FILE}）"
fi

# ---------- 2. 健康检查（最多等 40s） ----------
echo -n "→ 等待后端就绪"
ok=0
for _ in $(seq 1 80); do
  if curl -sf -o /dev/null -m 2 "$HEALTH_URL"; then
    ok=1
    break
  fi
  echo -n "."
  sleep 0.5
done
echo ""
if [ "$ok" = "1" ]; then
  echo "✓ 后端健康检查通过：$HEALTH_URL"
else
  echo "✗ 后端 ${PORT} 在 40s 内未就绪，请查日志：tail -f $BACKEND_LOG"
  exit 1
fi

[ "$BACKEND_ONLY" = "1" ] && { echo "✓ 完成（--backend-only）"; exit 0; }

# ---------- 3. 桌面应用 ----------
if pgrep -f "Vibe-Trading/desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron" >/dev/null 2>&1 \
   || pgrep -f "JARVIS Terminal.app/Contents/MacOS" >/dev/null 2>&1; then
  echo "✓ 桌面应用已在运行，跳过启动"
  exit 0
fi

packaged_app="$(ls -d "$ROOT"/desktop/dist/mac*/"JARVIS Terminal.app" 2>/dev/null | head -1 || true)"
if [ -n "$packaged_app" ]; then
  echo "→ 启动打包版桌面应用：$packaged_app"
  open "$packaged_app"
else
  echo "→ 未找到打包版，dev 模式启动桌面应用（日志 ${DESKTOP_LOG}）"
  cd "$ROOT/desktop" || exit 1
  nohup npm run dev >> "$DESKTOP_LOG" 2>&1 &
  desktop_pid=$!
  echo "$desktop_pid" > "$DESKTOP_PID_FILE"
  echo "  desktop dev PID ${desktop_pid}（已写入 ${DESKTOP_PID_FILE}）"
fi

echo "✓ 全部启动完成：后端 http://127.0.0.1:${PORT} + 桌面应用"
