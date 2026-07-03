#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-02 · 把 quantdinger-mcp 配进 Vibe-Trading 的 MCP 列表（对话即下单/查数）。

Vibe-Trading agent 启动时读取 ``~/.vibe-trading/agent.json`` 的 ``mcpServers``
（见 agent/src/config/loader.py / schema.py）。本脚本把 QuantDinger 的 MCP
server 以 stdio 方式合并进去，非破坏式：只增/改 ``quantdinger`` 一个键，
其余配置原样保留。

启动命令解析顺序（在目标机上自动探测，越靠前越优先）：
  1. ``uvx quantdinger-mcp``        —— 已装 uv（官方推荐，首跑自动拉包）
  2. ``quantdinger-mcp``            —— 已 pip install quantdinger-mcp
  3. ``python3 -m quantdinger_mcp`` —— 本仓库源码兜底（PYTHONPATH 指到
     QuantDinger/mcp_server/src，只需 python 环境里有 mcp>=1.2 与 httpx）

用法：
  python3 scripts/setup_quantdinger_mcp.py \
      --base-url http://localhost:8888 --token qd_agent_xxx
  python3 scripts/setup_quantdinger_mcp.py --dry-run       # 只打印不写盘
  QUANTDINGER_AGENT_TOKEN=qd_agent_xxx python3 scripts/setup_quantdinger_mcp.py

安全边界（与 QuantDinger docs/agent/MCP_SETUP.md 一致）：
  - MCP server 只包 R/W/B（读/工作区写/回测）工具，不暴露 quick-trade 下单、
    admin token、密钥库——真实下单仍走 jarvis_executor 的 REST 护栏链路。
  - token 是纸交易（paper-only）默认；写入的是本机用户级配置文件，不进 git。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent   # AILiangH/
MCP_SRC = REPO_ROOT / "QuantDinger" / "mcp_server" / "src"
CONFIG_PATH = Path.home() / ".vibe-trading" / "agent.json"
SERVER_KEY = "quantdinger"


def detect_launcher() -> tuple[str, list[str], dict[str, str]]:
    """按优先级探测 quantdinger-mcp 的启动方式，返回 (command, args, extra_env)。"""
    if shutil.which("uvx"):
        return "uvx", ["quantdinger-mcp"], {}
    if shutil.which("quantdinger-mcp"):
        return "quantdinger-mcp", [], {}
    # 源码兜底：不依赖任何安装，只要 python 环境有 mcp/httpx。
    # 包内无 __main__.py，入口是 server.py 的 main()，故 -m 指到 .server。
    # 优先用 Vibe-Trading 自己的 venv（其内已带 mcp/httpx）。
    venv_py = REPO_ROOT / "Vibe-Trading" / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else (sys.executable or "python3")
    return py, ["-m", "quantdinger_mcp.server"], {"PYTHONPATH": str(MCP_SRC)}


def build_entry(base_url: str, token: str) -> dict:
    command, args, extra_env = detect_launcher()
    env = {
        "QUANTDINGER_BASE_URL": base_url,
        "QUANTDINGER_AGENT_TOKEN": token,
        **extra_env,
    }
    return {
        "type": "stdio",
        "command": command,
        "args": args,
        "env": env,
        # 回测/优化类工具（wait_for_job）耗时远超默认 30s
        "toolTimeout": 120.0,
        "enabledTools": ["*"],
    }


def merge_config(entry: dict, path: Path) -> dict:
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"✗ 现有 {path} 不是合法 JSON，拒绝覆盖：{e}")
        if not isinstance(cfg, dict):
            raise SystemExit(f"✗ 现有 {path} 顶层不是对象，拒绝覆盖")
    servers = cfg.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit("✗ 现有 mcpServers 不是对象，拒绝覆盖")
    servers[SERVER_KEY] = entry
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="把 quantdinger-mcp 合并进 ~/.vibe-trading/agent.json")
    ap.add_argument("--base-url", default=os.environ.get("QUANTDINGER_BASE_URL", "http://localhost:8888"),
                    help="QuantDinger backend 地址（默认 http://localhost:8888，自托管栈）")
    ap.add_argument("--token", default=os.environ.get("QUANTDINGER_AGENT_TOKEN", ""),
                    help="Agent token（网页 Profile → My Agent Token 签发，qd_agent_ 开头）")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="agent.json 路径（默认 ~/.vibe-trading/agent.json）")
    ap.add_argument("--dry-run", action="store_true", help="只打印合并结果，不写盘")
    args = ap.parse_args()

    if not args.token and not args.dry_run:
        print("✗ 缺 token：--token qd_agent_xxx 或 export QUANTDINGER_AGENT_TOKEN=...")
        print("  （在 QuantDinger 网页 Profile → My Agent Token 签发，建议只给 R+B 起步）")
        return 1

    entry = build_entry(args.base_url, args.token or "qd_agent_REPLACE_ME")
    path = Path(args.config).expanduser()
    cfg = merge_config(entry, path)
    text = json.dumps(cfg, ensure_ascii=False, indent=2)

    launcher = entry["command"] + (" " + " ".join(entry["args"]) if entry["args"] else "")
    print(f"→ 启动方式: {launcher}")
    if "PYTHONPATH" in entry["env"]:
        print(f"  （源码兜底，PYTHONPATH={entry['env']['PYTHONPATH']}；需 pip install 'mcp>=1.2' httpx）")
    if args.dry_run:
        print(text)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)
    print(f"✓ 已写入 {path}（mcpServers.{SERVER_KEY}）")
    print("  重启 vibe-trading chat 后生效；试试对贾维斯说：")
    print("  「用 quantdinger 拉 BTC/USDT 最近 90 根日线」「查我的策略列表」「跑一个回测」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
