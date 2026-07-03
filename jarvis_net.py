#!/usr/bin/env python3
"""贾维斯 JARVIS - 本地代理自动探测（T-06 网络纪律补丁）。

背景：大陆网络直连 Binance / CoinGecko 常年超时，但本机往往跑着
xray / clash / v2ray 等本地代理（浏览器走系统代理所以看板 K 线正常，
Python requests 默认不会用）。本模块自动探测常见本地代理端口，
探测到即写入 HTTP(S)_PROXY 环境变量——requests 默认 trust_env=True，
进程内所有 HTTP 出网即全局生效。

纪律：
  - 用户已显式配置代理环境变量 → 完全不干预；
  - 未探测到本地代理 → 保持直连，行为与旧版一致；
  - 本模块写入的代理若失效（代理进程退出）→ 自动摘除回退直连。

用法（进程入口或数据层调用一次即可，重复调用有节流缓存）：
  import jarvis_net
  jarvis_net.ensure_proxy()
"""

from __future__ import annotations

import os
import socket
import time
from typing import Optional

# 常见本地代理端口（优先 http 型：socks 需 PySocks 依赖，缺依赖时跳过）
_CANDIDATES: tuple[tuple[int, str], ...] = (
    (10809, "http"),     # xray / v2rayN 默认 http 入站
    (7890, "http"),      # clash mixed/http
    (1087, "http"),      # V2rayU / ShadowsocksX-NG http
    (8118, "http"),      # privoxy
    (10808, "socks5h"),  # xray / v2rayN socks
    (7891, "socks5h"),   # clash socks
    (1080, "socks5h"),   # 通用 socks5
)
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy")
_PROBE_INTERVAL = 300.0   # 探测结果缓存 5 分钟，避免高频端口扫描

_last_probe = 0.0
_applied: Optional[str] = None   # 本模块写入的代理地址（区分用户显式配置）


def _port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _socks_available() -> bool:
    try:
        import socks  # noqa: F401  (PySocks)
        return True
    except ImportError:
        return False


def _user_proxy() -> Optional[str]:
    """用户显式设置（非本模块写入）的代理；有则尊重不干预。"""
    for k in _ENV_KEYS:
        v = os.environ.get(k)
        if v and v != _applied:
            return v
    return None


def _apply(proxy: Optional[str]) -> None:
    global _applied
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[k] = proxy
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    else:
        for k in _ENV_KEYS:
            if os.environ.get(k) == _applied:
                os.environ.pop(k, None)
    _applied = proxy


def ensure_proxy(force: bool = False) -> Optional[str]:
    """探测本地代理并设置环境变量；返回当前生效代理地址（直连返回 None）。

    force=True 跳过节流立即重新探测（供请求连续失败时的恢复路径调用）。
    """
    global _last_probe
    user = _user_proxy()
    if user:
        return user
    now = time.time()
    if not force and now - _last_probe < _PROBE_INTERVAL:
        return _applied
    _last_probe = now

    # 已应用的代理还活着就继续用；死了先摘除
    if _applied:
        try:
            port = int(_applied.rsplit(":", 1)[1])
        except ValueError:
            port = 0
        if port and _port_open(port):
            return _applied
        _apply(None)

    for port, scheme in _CANDIDATES:
        if scheme.startswith("socks") and not _socks_available():
            continue
        if _port_open(port):
            _apply(f"{scheme}://127.0.0.1:{port}")
            return _applied
    return None


if __name__ == "__main__":
    p = ensure_proxy(force=True)
    print(f"代理探测结果: {p or '未发现本地代理（保持直连）'}")
