#!/bin/bash
# ============================================================
# 贾维斯 — 全手动「一键启动」（B 方案：完全手动掌控，不靠 launchd 自愈）
#
# 起全部：daemon(读 watchlist) + sync + twelvesim + dashboard(:7899) + 桌面
#   - 先 bootout launchd 托管的同名服务，避免 KeepAlive 与手动进程抢
#   - 再 nohup 后台起（关终端不影响）
#   - daemon 不带 --symbols → 读配置中心 watchlist（当前 BTCUSDT/ETHUSDT）
#
# 用法： ./start-all.sh
# 停止： ./stop-all.sh
#
# 前置：币安 IP 封禁需已解除（当日 418 到 18:19:52），否则拉不到实时数据。
# 提示：手动模式无崩溃自愈——进程挂了需自己重跑本脚本。
# ============================================================
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
U="$(id -u)"; DOMAIN="gui/$U"; LA="$HOME/Library/LaunchAgents"
LOG="$HOME/.vibe-trading/log"; mkdir -p "$LOG"

echo "== 1. 先停 launchd 托管，避免与手动进程抢（KeepAlive）=="
for svc in com.jarvis.daemon com.jarvis.sync com.jarvis.twelvesim com.jarvis.dashboard; do
  [ -f "$LA/$svc.plist" ] && launchctl bootout "$DOMAIN/$svc" 2>/dev/null && echo "   OK  已停 launchd $svc"
done

echo "== 2. 清手动 nohup 残留，避免重复起 =="
for pat in "jarvis_daemon.py" "jarvis_sync.py" "jarvis_twelve_trader.py"; do
  pids="$(pgrep -f "$pat" 2>/dev/null || true)"
  # shellcheck disable=SC2086
  [ -n "$pids" ] && kill $pids 2>/dev/null && echo "   OK  清残留 $pat (PID $pids)"
done
sleep 1

cd "$ROOT" || exit 1

echo "== 3. 启动 daemon（读 watchlist，不带 --symbols）=="
nohup "$PY" jarvis_daemon.py --interval-hours 24.0 --intraday >> "$LOG/jarvis_daemon.log" 2>&1 &
echo "   daemon PID $!（日志 $LOG/jarvis_daemon.log）"

echo "== 4. 启动 sync =="
nohup "$PY" jarvis_sync.py >> "$LOG/jarvis_sync.launchd.log" 2>&1 &
echo "   sync PID $!（业务日志 ~/.vibe-trading/sync/sync.log）"

echo "== 5. 启动 twelvesim =="
nohup "$PY" jarvis_twelve_trader.py run --interval-min 5 >> "$LOG/jarvis_twelvesim.log" 2>&1 &
echo "   twelvesim PID $!（日志 $LOG/jarvis_twelvesim.log）"

echo "== 6. 启动 dashboard + 桌面（复用 start.sh）=="
"$ROOT/start.sh"

echo ""
echo "== 全部启动完成（手动模式，无自愈；进程挂了自己重跑本脚本）=="
echo "验证： curl -s http://127.0.0.1:10808/api/watchlist   应=[BTCUSDT,ETHUSDT]"
