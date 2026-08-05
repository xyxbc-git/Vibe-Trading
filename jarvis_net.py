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

import json
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


# ── 交易所 IP 封禁登记（Binance -1003/418 "banned until <ms>"）────────────────
# REST 层发现 IP 级限频封禁后按主机登记到磁盘，daemon / dashboard / sync /
# twelvesim 多进程共享；封禁期内调用方应短路跳过该主机（直接走缓存/备源），
# 避免封禁期持续撞墙被交易所延长封禁。登记/查询永不抛出。

_BAN_PATH = os.path.expanduser("~/.vibe-trading/net_ban.json")
_BAN_RELOAD_S = 5.0   # 磁盘重读节流：多进程间感知延迟上限

_ban_cache: dict[str, float] = {}
_ban_read_at = 0.0


def _ban_key(url_or_host: str) -> str:
    """URL / 主机名 → 归一化主机 key（去 scheme / 路径 / 端口，小写）。"""
    h = url_or_host or ""
    if "://" in h:
        h = h.split("://", 1)[1]
    return h.split("/", 1)[0].split(":", 1)[0].lower()


def _ban_load(now: float) -> None:
    global _ban_read_at, _ban_cache
    if now - _ban_read_at < _BAN_RELOAD_S:
        return
    _ban_read_at = now
    try:
        with open(_BAN_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        disk = ({str(k): float(v) for k, v in raw.items()}
                if isinstance(raw, dict) else {})
    except Exception:  # noqa: BLE001 — 文件缺失/损坏视为无登记
        disk = {}
    # 与进程内已知登记合并取较晚者（写盘失败时进程内记忆不丢）
    for h, t in disk.items():
        if t > _ban_cache.get(h, 0.0):
            _ban_cache[h] = t


def report_ban(url_or_host: str, until_ts: float) -> None:
    """登记主机封禁（until_ts 为秒级 epoch）。写盘失败静默，进程内仍生效。"""
    try:
        host = _ban_key(url_or_host)
        now = time.time()
        if not host or until_ts <= now:
            return
        _ban_load(now)
        if _ban_cache.get(host, 0.0) >= until_ts:
            return
        _ban_cache[host] = until_ts
        alive = {h: t for h, t in _ban_cache.items() if t > now}
        os.makedirs(os.path.dirname(_BAN_PATH), exist_ok=True)
        tmp = _BAN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alive, f)
        os.replace(tmp, _BAN_PATH)
    except Exception:  # noqa: BLE001
        pass


def banned_until(url_or_host: str) -> float:
    """主机的封禁截止（秒级 epoch）；未封禁 / 已过期返回 0.0。"""
    try:
        now = time.time()
        _ban_load(now)
        t = _ban_cache.get(_ban_key(url_or_host), 0.0)
        return t if t > now else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# ── 交易所 REST 出网分钟预算（跨进程共享 60s 滑动窗口）───────────────────────
# 背景：单进程限速挡不住 IP 级封禁——daemon / dashboard / sync / twelvesim 多
# 进程各守各的 rest_max_per_min，叠加后 IP 维度请求量翻数倍撞 418/-1003。此处
# 把 60s 滑动窗口落到共享文件（fcntl 独占锁保护读改写），多进程共用一个 IP 级
# 真实预算。任何异常（锁 / IO / 平台无 fcntl）静默放行，由调用方进程内预算兜底，
# 绝不因共享层故障阻断全部出网。
_BUDGET_PATH = os.path.expanduser("~/.vibe-trading/net_budget.json")
_BUDGET_WINDOW_S = 60.0


def _budget_try(host: str, limit: int, now: float) -> tuple[bool, float]:
    """加锁读改写共享窗口：授权记账返回 (True, 0)，超限返回 (False, 需等秒数)。

    平台无 fcntl / 文件异常时抛出，由 budget_take 兜底放行。
    """
    import fcntl
    os.makedirs(os.path.dirname(_BUDGET_PATH), exist_ok=True)
    with open(_BUDGET_PATH, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            txt = f.read()
            try:
                raw = json.loads(txt) if txt.strip() else {}
                if not isinstance(raw, dict):
                    raw = {}
            except Exception:  # noqa: BLE001 — 文件损坏视为空窗口
                raw = {}
            win = [float(t) for t in raw.get(host, [])
                   if isinstance(t, (int, float))]
            win = [t for t in win if now - t < _BUDGET_WINDOW_S]
            if len(win) < limit:
                win.append(now)
                granted, wait = True, 0.0
            else:
                granted, wait = False, (win[0] + _BUDGET_WINDOW_S) - now
            raw[host] = win
            # 顺带清理其它 host 过期窗口，防共享文件无界增长
            for h in list(raw.keys()):
                if h == host:
                    continue
                kept = [t for t in raw.get(h, [])
                        if isinstance(t, (int, float))
                        and now - t < _BUDGET_WINDOW_S]
                if kept:
                    raw[h] = kept
                else:
                    raw.pop(h, None)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(raw))
            f.flush()
            return granted, wait
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def budget_take(host: str, limit: int, *, block: bool = False,
                max_wait: float = 15.0) -> bool:
    """占用一次对 host 的 IP 级出网额度（跨进程共享 60s 滑动窗口）。

    block=True 时最多等 max_wait 秒让滑窗腾位；limit ≤ 0 视为不限。
    任何异常（平台无 fcntl / 文件故障）静默放行 True，由调用方进程内预算兜底。
    """
    try:
        host = _ban_key(host)
        limit = int(limit)
    except Exception:  # noqa: BLE001
        return True
    if not host or limit <= 0:
        return True
    deadline = time.time() + (max_wait if block else 0.0)
    while True:
        now = time.time()
        try:
            granted, wait = _budget_try(host, limit, now)
        except Exception:  # noqa: BLE001 — 锁 / IO / 平台不支持 → 放行兜底
            return True
        if granted:
            return True
        if now >= deadline:
            return False
        time.sleep(min(max(wait, 0.05), 0.5))


if __name__ == "__main__":
    p = ensure_proxy(force=True)
    print(f"代理探测结果: {p or '未发现本地代理（保持直连）'}")
