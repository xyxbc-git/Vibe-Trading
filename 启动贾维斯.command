#!/bin/bash
# ============================================================
# 贾维斯 JARVIS 一键启动脚本（macOS 可双击运行）
# 进入 Vibe-Trading 交互聊天模式
#
# [T-03/M4] 可选一并拉起 QuantDinger 执行外壳栈（pg+redis+backend+frontend）：
#   方式一：参数    ./启动贾维斯.command --with-qd
#   方式二：环境变量 JARVIS_WITH_QD=1 ./启动贾维斯.command
#   不带任一开关时，行为与旧版完全一致（仅进 Vibe-Trading 聊天，不碰 Docker）。
# ============================================================

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ---- 可选：拉起 QuantDinger 栈（默认关闭，需显式开关）----
WITH_QD=0
for arg in "$@"; do
  [ "$arg" = "--with-qd" ] && WITH_QD=1
done
[ "${JARVIS_WITH_QD:-0}" = "1" ] && WITH_QD=1

if [ "$WITH_QD" = "1" ]; then
  QD_DIR="$ROOT/QuantDinger"
  QD_COMPOSE="docker-compose.local.yml"
  echo "=================================================="
  echo "   [可选] 启动 QuantDinger 执行外壳栈"
  echo "=================================================="
  if ! command -v docker >/dev/null 2>&1; then
    echo "⚠ 未检测到 docker，跳过 QuantDinger 栈启动（仅进 Vibe-Trading 聊天）。"
  elif [ ! -f "$QD_DIR/$QD_COMPOSE" ]; then
    echo "⚠ 未找到 $QD_DIR/$QD_COMPOSE，跳过 QuantDinger 栈启动。"
  elif [ ! -f "$QD_DIR/backend.env" ]; then
    echo "⚠ 缺少 $QD_DIR/backend.env（首次需 cp backend_api_python/env.example backend.env），跳过。"
  else
    echo "→ docker compose -f $QD_COMPOSE up -d （pg16/redis7/backend:5000/frontend:8888）"
    ( cd "$QD_DIR" && docker compose -f "$QD_COMPOSE" up -d ) \
      && echo "✓ QuantDinger 栈已拉起，监控面板: http://localhost:8888 （quantdinger/123456）" \
      || echo "⚠ QuantDinger 栈启动失败，继续进入 Vibe-Trading 聊天（执行下单将不可用）。"
  fi
  echo ""
fi

# 切到 Vibe-Trading 目录
cd "$ROOT/Vibe-Trading" || exit 1

echo "=================================================="
echo "   启动贾维斯 JARVIS (Vibe-Trading 交互模式)"
echo "   数据源: binance | 大脑: DeepSeek"
echo "=================================================="
echo ""
echo "用法示例（直接输入自然语言）："
echo '  分析 BTC-USDT 最近 60 天走势，给支撑阻力和多空观点'
echo '  回测 ETH-USDT 20/50 均线策略最近一年，报告收益和回撤'
echo '  用 binance 数据分析 SOL-USDT 趋势'
echo ""
echo "输入 exit 或按 Ctrl+C 退出。"
echo "=================================================="
echo ""

# 启动交互式聊天
exec ./.venv/bin/vibe-trading chat
