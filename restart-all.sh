#!/bin/bash
# ============================================================
# 贾维斯 — 一键重启（让「币种同步/降频/新增币种入口+检测/daemon 根治」全部改动生效）
#
# 覆盖 4 个 launchd 服务 + 桌面：
#   - dashboard   : 新增币种入口/检测接口(/api/symbol/check、/watchlist/add) + 降频治本
#   - sync        : delete-absent 清 tape_bar 旧币 + 降频治本
#   - daemon      : 读 watchlist（不再固化 --symbols）+ 降 2 币 + 降频治本
#   - twelvesim   : 降频治本
#
# 用法：
#   ./restart-all.sh
#
# 前置（需你手动，脚本不代做）：
#   ① 币安 IP 封禁需已解除（当日 418 封禁到 18:19:52 后再跑，否则拉不到实时数据）
#   ② 旧币清理需 root 一次性授权（见脚本末尾第 5 步提示的 GRANT 语句）
#
# 安全：本脚本只重启进程 + 改本地配置，不做任何 DB 特权/删除操作。
# ============================================================
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
U="$(id -u)"; DOMAIN="gui/$U"; LA="$HOME/Library/LaunchAgents"

echo "==============================================="
echo " 贾维斯一键重启：让今天全部改动生效"
echo "==============================================="

# ---------- 1. 治本降频全局值 40 -> 180 ----------
echo "[1/5] rest_max_per_min -> 180（治本跨进程共享后是全局每分钟总额）"
"$PY" - <<'PYEOF'
try:
    import jarvis_config as c
    c.save({"rest_max_per_min": 180}, source="restart-all")
    print("      现值 =", c.get("rest_max_per_min"))
except Exception as e:  # noqa: BLE001
    print("      [WARN] 写配置失败：", e)
    print("      可手动改 ~/.vibe-trading/config.yaml 的 data.rest_max_per_min: 180")
PYEOF

# ---------- 2. 重装 daemon plist（不固化 --symbols -> 读 watchlist）----------
echo "[2/5] 重装 daemon launchd plist（读 watchlist，不再写死币种）"
if "$PY" "$ROOT/jarvis_daemon.py" --install-launchd --intraday >/dev/null 2>&1; then
  echo "      plist 已更新"
else
  echo "      [WARN] 重装失败，请手动：$PY jarvis_daemon.py --install-launchd --intraday"
fi

# ---------- 3. 重启后台 launchd 服务 ----------
echo "[3/5] 重启 launchd 服务：daemon / sync / twelvesim"
restart_svc() {
  local svc="$1" plist="$LA/$1.plist"
  if [ ! -f "$plist" ]; then echo "      - 跳过 $svc（无 plist）"; return; fi
  launchctl bootout "$DOMAIN/$svc" 2>/dev/null
  sleep 1
  if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then
    echo "      OK  $svc 已重启（bootstrap 重读 plist）"
  elif launchctl kickstart -k "$DOMAIN/$svc" 2>/dev/null; then
    echo "      OK  $svc 已重启（kickstart 兜底）"
  else
    echo "      [WARN] $svc 重启失败，请手动 launchctl bootstrap $DOMAIN $plist"
  fi
}
restart_svc com.jarvis.daemon
restart_svc com.jarvis.sync
restart_svc com.jarvis.twelvesim

# ---------- 4. 重启 dashboard + 桌面（沿用现有 restart.sh，按端口抓取无论谁起的）----------
echo "[4/5] 重启 dashboard + 桌面应用（restart.sh）"
if "$ROOT/restart.sh" >/dev/null 2>&1; then
  echo "      OK  dashboard + 桌面已重启"
else
  echo "      [WARN] restart.sh 返回非 0，请查 ~/.vibe-trading/dashboard-7899.log"
fi

# ---------- 5. 提示 root 授权（旧币清理，脚本不代执行 DB 特权操作）----------
echo "[5/5] 旧币清理需一次性 root 授权（本脚本不代执行 DB 特权操作）："
echo "      GRANT DELETE ON jiaweisi.jarvis_tape_bar TO 'jarvis_sync'@'localhost'; FLUSH PRIVILEGES;"
echo "      （用户名/host 以 agent-11 的 sql/jarvis_mysql_watchlist_cleanup.sql B 段为准）"
echo "      授权后 sync 下一轮自动清 tape_bar 旧 5 币（signal_change 已豁免不动）"

echo "==============================================="
echo " 完成。验证："
echo "  - curl -s http://127.0.0.1:7899/api/watchlist   应=[BTCUSDT,ETHUSDT]"
echo "  - tail -f ~/.vibe-trading/jarvis_data_degrade.log   看 418 是否消失"
echo "  - 桌面顶栏价/K线随行情更新；顶栏「+ 添加币种」可用"
echo "==============================================="
