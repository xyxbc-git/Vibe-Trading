#!/bin/bash
# ============================================================
# 贾维斯 JARVIS —— 一键切换到 PostgreSQL（可双击运行）
#
# 做四件事（安全、可回退）：
#   1) 停掉正在运行的 jarvis_daemon / jarvis_dashboard（先记住它们的启动命令）
#   2) 备份本地 SQLite 库（jarvis_journal.db → *.pre-pg-时间戳.bak）
#   3) 跑一次迁移脚本 jarvis_migrate_to_pg.py（幂等，补齐停机前新写入的行）
#   4) 用原样的命令把 daemon / dashboard 重新拉起（自动经 db.json 走 pg）
#
# 前提：~/.vibe-trading/db.json 里已配好 pg 连接串（本次迁移已写好）。
# 回退：删除 ~/.vibe-trading/db.json 即刻回到 SQLite，数据仍在本地库。
# ============================================================

set -uo pipefail

ROOT="/Users/jolly/MyselfProject/TwosZiyan/AILiangH/Vibe-Trading"
PY="$ROOT/.venv/bin/python"
VT_DIR="$HOME/.vibe-trading"
DB_JSON="$VT_DIR/db.json"
SQLITE="$VT_DIR/jarvis_journal.db"

cd "$ROOT" || { echo "❌ 进不去 $ROOT"; exit 1; }

echo "=================================================="
echo "   贾维斯 → PostgreSQL 一键切换"
echo "=================================================="

# ---- 0. 前置检查 ----
if [ ! -x "$PY" ]; then echo "❌ 找不到 venv 解释器：$PY"; exit 1; fi
if [ ! -f "$DB_JSON" ]; then
  echo "⚠ 未发现 $DB_JSON —— pg 未激活。"
  echo "  若要启用 pg，请先创建该文件并填 {\"url\":\"postgresql://jarvis:密码@127.0.0.1:5432/jarvis\"}"
  read -r -p "  仍要继续（仅备份+尝试迁移）？[y/N] " ans
  [ "${ans:-N}" = "y" ] || [ "${ans:-N}" = "Y" ] || { echo "已取消。"; exit 0; }
fi

# ---- 1. 记住并停掉正在运行的进程 ----
DAEMON_PID="$(pgrep -f 'jarvis_daemon.py' | head -1 || true)"
DASH_PID="$(pgrep -f 'jarvis_dashboard.py' | head -1 || true)"
DAEMON_CMD=""; DASH_CMD=""
[ -n "$DAEMON_PID" ] && DAEMON_CMD="$(ps -o command= -p "$DAEMON_PID")"
[ -n "$DASH_PID" ]   && DASH_CMD="$(ps -o command= -p "$DASH_PID")"

echo ""
echo "→ 检测到进程："
echo "   daemon    : ${DAEMON_PID:-（未运行）}"
echo "   dashboard : ${DASH_PID:-（未运行）}"

stop_pid () {
  local pid="$1" name="$2"
  [ -z "$pid" ] && return 0
  echo "→ 停止 $name (pid $pid) ..."
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || { echo "   已退出"; return 0; }
    sleep 1
  done
  echo "   仍在 → 强制结束"; kill -9 "$pid" 2>/dev/null || true
}
stop_pid "$DAEMON_PID" "daemon"
stop_pid "$DASH_PID" "dashboard"

# ---- 2. 备份本地 SQLite ----
if [ -f "$SQLITE" ]; then
  BAK="$SQLITE.pre-pg-$(date +%Y%m%d-%H%M%S).bak"
  cp "$SQLITE" "$BAK" && echo "→ 已备份 SQLite → $BAK"
fi

# ---- 3. 跑迁移（幂等：ON CONFLICT DO NOTHING，可重复）----
echo ""
echo "→ 执行迁移 jarvis_migrate_to_pg.py ..."
"$PY" jarvis_migrate_to_pg.py
MIG_RC=$?
if [ $MIG_RC -ne 0 ]; then
  echo "❌ 迁移脚本返回非 0（$MIG_RC）。未重启进程，请排查后再试。"
  echo "   注：本地 SQLite 未被改动，可随时用回。"
  exit $MIG_RC
fi

# ---- 4. 用原命令重启（自动经 db.json 走 pg）----
echo ""
echo "→ 重启进程（走 pg）..."
if [ -n "$DAEMON_CMD" ]; then
  nohup bash -c "$DAEMON_CMD" >> "$VT_DIR/jarvis_daemon_run.log" 2>&1 &
  echo "   daemon 已拉起 → 日志 $VT_DIR/jarvis_daemon_run.log"
else
  echo "   （原先没在跑 daemon，跳过。手动启动示例："
  echo "     $PY jarvis_daemon.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval-hours 24.0 --intraday ）"
fi
if [ -n "$DASH_CMD" ]; then
  nohup bash -c "$DASH_CMD" >> "$VT_DIR/jarvis_dashboard_run.log" 2>&1 &
  echo "   dashboard 已拉起 → 日志 $VT_DIR/jarvis_dashboard_run.log"
else
  echo "   （原先没在跑 dashboard，跳过。手动启动示例：$PY jarvis_dashboard.py --port 7899 ）"
fi

echo ""
echo "=================================================="
echo "✅ 切换完成。贾维斯现在读写 PostgreSQL(jarvis 库)。"
echo "   回退：删除 $DB_JSON 后重启进程即回到 SQLite。"
echo "=================================================="
