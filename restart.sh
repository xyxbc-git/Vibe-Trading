#!/bin/bash
# 贾维斯桌面终端 — 一键重启（stop.sh → start.sh，参数原样透传给 start.sh）
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/stop.sh"
echo ""
sleep 1
exec "$ROOT/start.sh" "$@"
