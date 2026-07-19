#!/usr/bin/env python3
"""盘口深度透视冒烟：纯函数验证 步长归整 / 桶聚合 / 累计深度 / 失衡比。

不联网：直接调 nice_step()/aggregate_book()。
"""

from __future__ import annotations

import jarvis_depth_view as jdv

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


# ── 1) nice_step：归整到 1/2/5×10^k ──────────────────────────────────
check("step-0.0007→0.0005", jdv.nice_step(0.0007) == 0.0005, f"{jdv.nice_step(0.0007)}")
check("step-37→50", jdv.nice_step(37) == 50, f"{jdv.nice_step(37)}")
check("step-12→10", jdv.nice_step(12) == 10, f"{jdv.nice_step(12)}")
check("step-2.4→2", jdv.nice_step(2.4) == 2, f"{jdv.nice_step(2.4)}")
check("step-非法→1", jdv.nice_step(-1) == 1.0 and jdv.nice_step(0) == 1.0)

# ── 2) 桶聚合：买盘向下、卖盘向上取整，不跨 mid ─────────────────────
bids = [["59995", "1.0"], ["59990", "2.0"], ["59985", "0.5"], ["59960", "3.0"]]
asks = [["60005", "1.5"], ["60010", "1.0"], ["60030", "2.0"]]
out = jdv.aggregate_book(bids, asks, bucket=10, max_buckets=10)
check("聚合-mid", out["mid"] == 60000, f"mid={out['mid']}")
check("聚合-价差", out["best_bid"] == 59995 and out["best_ask"] == 60005)
b0 = out["bids"][0]
check("聚合-买盘首桶向下取整", b0["price"] == 59990 and abs(b0["qty"] - 3.0) < 1e-9,
      f"b0={b0}")   # 59995、59990 同落 59990 桶
a0 = out["asks"][0]
check("聚合-卖盘首桶向上取整", a0["price"] == 60010 and abs(a0["qty"] - 2.5) < 1e-9,
      f"a0={a0}")   # 60005、60010 同落 60010 桶
check("聚合-买盘降序", [r["price"] for r in out["bids"]]
      == sorted([r["price"] for r in out["bids"]], reverse=True))
check("聚合-卖盘升序", [r["price"] for r in out["asks"]]
      == sorted([r["price"] for r in out["asks"]]))

# ── 3) 累计深度单调递增 ─────────────────────────────────────────────
cums = [r["cum_usd"] for r in out["bids"]]
check("累计-单调", all(cums[i] <= cums[i + 1] for i in range(len(cums) - 1)), f"{cums}")
check("累计-末桶=总和", abs(cums[-1] - sum(r["usd"] for r in out["bids"])) < 0.01)

# ── 4) 失衡比：买厚 → ratio > 1 ─────────────────────────────────────
imb = out["imbalance"]
expect_ratio = imb["bid_usd_10"] / imb["ask_usd_10"]
check("失衡-口径", abs(imb["ratio"] - round(expect_ratio, 3)) < 1e-9, f"{imb}")

# ── 5) 空侧容错 ─────────────────────────────────────────────────────
out2 = jdv.aggregate_book([], asks, bucket=10)
check("空买盘-不崩", out2["bids"] == [] and out2["best_bid"] is None)
check("空买盘-ratio=None", out2["imbalance"]["ratio"] is None
      or out2["imbalance"]["bid_usd_10"] == 0)

# ── 6) 自适应步长（bucket 不传）─────────────────────────────────────
out3 = jdv.aggregate_book(bids, asks)
check("自适应-步长为好看值", out3["bucket"] in (10.0, 20.0),
      f"bucket={out3['bucket']}")  # 60000×0.0002=12 → 10

# ── 7) 坏数据容错 ───────────────────────────────────────────────────
out4 = jdv.aggregate_book([["abc", "1"], ["59990", "-2"], ["59980", "1"]],
                          [["60010", "x"]], bucket=10)
check("坏数据-跳过", len(out4["bids"]) == 1 and out4["asks"] == [])

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
