#!/bin/bash
# ============================================================
# 贾维斯 — 全手动「一键停止」（B 方案：完全手动掌控，不靠 launchd 自愈）
#
# 停掉三类：
#   1. launchd 托管的 4 个服务（bootout，本次登录会话内不再自愈拉起）
#   2. 手动 nohup 起的残留 python 进程（daemon/sync/twelvesim）
#   3. dashboard(:7899) + 桌面 Electron（复用 stop.sh）
#
# 用法： ./stop-all.sh
# ============================================================
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
U="$(id -u)"; DOMAIN="gui/$U"; LA="$HOME/Library/LaunchAgents"

echo "== 1. 停 launchd 托管服务（本次会话不再自愈拉起）=="
for svc in com.jarvis.daemon com.jarvis.sync com.jarvis.twelvesim com.jarvis.dashboard; do
  if [ -f "$LA/$svc.plist" ]; then
    if launchctl bootout "$DOMAIN/$svc" 2>/dev/null; then
      echo "   OK  已停 $svc"
    else
      echo "   -   $svc 未在 launchd 运行"
    fi
  fi
done

echo "== 2. 停手动 nohup 残留进程 =="
for pat in "jarvis_daemon.py" "jarvis_sync.py" "jarvis_twelve_trader.py"; do
  pids="$(pgrep -f "$pat" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null && echo "   OK  已停 $pat (PID $pids)"
  else
    echo "   -   $pat 无残留"
  fi
done

echo "== 3. 停 dashboard + 桌面（复用 stop.sh）=="
"$ROOT/stop.sh" 2>/dev/null || echo "   (stop.sh 返回非 0，通常是本来就没在跑)"

echo ""
echo "== 全部已停 =="
echo "注：plist 仍在，重启电脑后 launchd 会按 RunAtLoad 再拉起这些服务。"
echo "如需彻底手动（重启电脑也不自动起），运行一次禁用（gui 域，无需 sudo）："
echo "  for s in com.jarvis.daemon com.jarvis.sync com.jarvis.twelvesim com.jarvis.dashboard; do launchctl disable gui/$U/\$s; done"
echo "想恢复开机自启：把上面 disable 改成 enable 再跑一次。"
