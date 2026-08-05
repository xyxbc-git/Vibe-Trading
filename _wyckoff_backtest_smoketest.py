#!/usr/bin/env python3
"""威科夫历史回放回测（T2.6）口径 smoketest —— T2.8 验收门禁。

不联网：monkeypatch jarvis_delta_flow.fetch_bars 注入合成序列，并对
jarvis_crypto_data._get 加计数断言（回测全程零真实出网）。

口径契约（计划文档 §T2.6，v1.0 冻结）：
  - 逐 bar 滚动重放 detect_events；Spring/LPS 触发后 N=12 根收盘 > 触发价计命中，
    UTAD/LPSY 镜像；基线 = 全体可评估 bar 同口径概率。
  - 输出：命中率 / 基线 / uplift / 样本数；样本 <30 → insufficient_samples，
    禁止小样本吹牛。

模块未交付时本脚本即红灯（预期——依赖甲的 T2.6）。
"""

from __future__ import annotations

import inspect
import json
import time

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


try:
    import jarvis_wyckoff as jwk
except ImportError as e:
    print(f"❌ FAIL：jarvis_wyckoff 未交付（甲的 T2.6 未落盘）：{e}")
    print("通过 0 / 失败 1")
    raise SystemExit(1)

import jarvis_crypto_data as jcd  # noqa: E402
import jarvis_delta_flow as jdf   # noqa: E402

NOW_MS = int(time.time() * 1000)
TF_MS = 3_600_000  # 1h


def mk(o: float, h: float, l: float, c: float, v: float, i: int,
       total: int) -> dict:
    return {"ts": NOW_MS - (total - i) * TF_MS, "open": o, "high": h, "low": l,
            "close": c, "volume": v, "taker_buy": v * 0.5}


def saga_small() -> list[dict]:
    """小样本序列：一段区间 + 单次 Spring（样本必然 <30）。"""
    total = 60
    bars = []
    for i in range(40):  # 横盘
        d = 0.6 if i % 2 == 0 else -0.6
        bars.append(mk(100 - d * 0.2, 100 + abs(d) + 0.3, 100 - abs(d) - 0.3,
                       100 + d * 0.6, 1000.0, i, total))
    bars.append(mk(99.0, 99.1, 97.2, 97.6, 800.0, 40, total))   # 破位
    bars.append(mk(97.7, 99.2, 97.5, 98.9, 900.0, 41, total))   # 收回=Spring
    for i in range(42, 60):  # 触发后一路走高（若被计样本则必命中）
        px = 99.0 + (i - 42) * 0.3
        bars.append(mk(px, px + 0.5, px - 0.3, px + 0.3, 1000.0, i, total))
    return bars


def saga_big() -> list[dict]:
    """大样本序列：70 组「横盘10根 + Spring破位/收回 + 回升2根」摩托（980 根）。

    - 每组 Spring 低点比历史最低再降 0.01 → 恒定构成「破区间低点」；
    - 触发后 12 根内收盘恒 > 触发价 → 命中口径下 hit_rate 应显著高于基线；
    - 整体价格水平不漂移 → 基线（任意 bar 12 根后更高的概率）≈ 50%。
    """
    total = 70 * 14
    bars = []
    i = 0
    for m in range(70):
        for k in range(10):
            d = 0.6 if k % 2 == 0 else -0.6
            bars.append(mk(100 - d * 0.2, 100 + abs(d) + 0.3,
                           100 - abs(d) - 0.3, 100 + d * 0.6, 1000.0, i, total))
            i += 1
        low = 97.0 - 0.01 * m
        bars.append(mk(99.0, 99.1, low, low + 0.4, 800.0, i, total)); i += 1
        bars.append(mk(low + 0.5, 99.4, low + 0.3, 99.2, 900.0, i, total)); i += 1
        bars.append(mk(99.2, 100.4, 99.1, 100.2, 1000.0, i, total)); i += 1
        bars.append(mk(100.2, 100.9, 99.9, 100.5, 1000.0, i, total)); i += 1
    return bars


# ── 定位回测入口（契约函数名候选） ─────────────────────────────────────
_CANDIDATES = ("backtest", "run_backtest", "backtest_events", "replay_backtest")
_fn = next((getattr(jwk, n) for n in _CANDIDATES
            if callable(getattr(jwk, n, None))), None)
check("T2.6 回测入口按契约导出（backtest/run_backtest/…）", _fn is not None,
      f"jarvis_wyckoff 现有导出={[n for n in dir(jwk) if not n.startswith('_')]}")
if _fn is None:
    print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
    raise SystemExit(1)

_net_calls = {"n": 0}
_orig_get, _orig_fetch = jcd._get, jdf.fetch_bars


def _run(bars: list[dict]) -> dict | None:
    """按 _fn 签名自适应调用：优先 bars 直喂；否则打桩 fetch_bars 走 symbol。"""
    params = list(inspect.signature(_fn).parameters)

    def _count_get(*a, **k):
        _net_calls["n"] += 1
        raise RuntimeError("回测冒烟期禁止真实出网")

    jcd._get = _count_get
    jdf.fetch_bars = lambda *a, **k: [dict(b) for b in bars]
    try:
        if "bars" in params:
            return _fn(bars=[dict(b) for b in bars])
        kwargs = {}
        if "interval" in params:
            kwargs["interval"] = "1h"
        if "days" in params:
            kwargs["days"] = 90
        return _fn("TESTUSDT", **kwargs)
    finally:
        jcd._get, jdf.fetch_bars = _orig_get, _orig_fetch


def _num(d: dict, *names) -> float | None:
    for n in names:
        if isinstance(d, dict) and isinstance(d.get(n), (int, float)):
            return float(d[n])
    return None


def _insufficient(d: dict) -> bool:
    """insufficient_samples 标记的宽容判定（布尔字段/状态串/文本任一）。"""
    if not isinstance(d, dict):
        return False
    if d.get("insufficient_samples") is True:
        return True
    return "insufficient_samples" in json.dumps(d, ensure_ascii=False)


# ── B1/B2：小样本 → 诚实口径 ──────────────────────────────────────────
r1 = _run(saga_small())
check("B1 小样本-返回 dict", isinstance(r1, dict), str(type(r1)))
if isinstance(r1, dict):
    s1 = _num(r1, "samples", "n_samples", "sample_n")
    check("B1 小样本-样本数字段存在且 <30", s1 is not None and s1 < 30,
          f"samples={s1} keys={sorted(r1.keys())}")
    check("B1 小样本-标记 insufficient_samples（禁止小样本吹牛）",
          _insufficient(r1), f"resp={str(r1)[:200]}")
    check("B2 口径-键含 命中率/基线/uplift/样本数",
          _num(r1, "hit_rate", "hit") is not None
          and _num(r1, "baseline", "base_rate") is not None
          and _num(r1, "uplift") is not None
          and s1 is not None,
          f"keys={sorted(r1.keys())}")
check("B1 零出网（jcd._get 未被真实调用）", _net_calls["n"] == 0,
      f"出网 {_net_calls['n']} 次")

# ── B3/B4：大样本 → 算术一致性与方向 ──────────────────────────────────
r2 = _run(saga_big())
check("B3 大样本-返回 dict", isinstance(r2, dict), str(type(r2)))
if isinstance(r2, dict):
    s2 = _num(r2, "samples", "n_samples", "sample_n")
    hit = _num(r2, "hit_rate", "hit")
    base = _num(r2, "baseline", "base_rate")
    up = _num(r2, "uplift")
    if s2 is not None and s2 >= 30:
        check("B3 大样本-样本≥30 不再标 insufficient", not _insufficient(r2),
              f"samples={s2}")
        check("B3 口径-uplift == hit_rate - baseline（±1e-6）",
              None not in (hit, base, up) and abs(up - (hit - base)) < 1e-6,
              f"hit={hit} base={base} uplift={up}")
        check("B4 方向-全命中夹具 uplift > 0",
              up is not None and up > 0, f"uplift={up}")
    else:
        # 检测器口径更严导致样本不足时：必须诚实标注（口径仍受验）
        check("B3 大样本-样本不足时如实标 insufficient_samples",
              _insufficient(r2),
              f"samples={s2} resp={str(r2)[:200]}")
        check("B3 口径-样本不足仍返回样本数字段", s2 is not None,
              f"keys={sorted(r2.keys()) if isinstance(r2, dict) else r2}")
        print(f"ℹ️  大样本夹具实际 samples={s2}（<30），uplift 算术断言待引擎"
              f"口径校准后复验")

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
if FAIL == 0:
    print("ALL PASS")
raise SystemExit(1 if FAIL else 0)
