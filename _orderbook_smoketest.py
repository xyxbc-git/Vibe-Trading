#!/usr/bin/env python3
"""jarvis_orderbook 离线冒烟测试（纯函数 + 注入式，不联网）。

覆盖：增量应用/删档、快照衔接判定（合约 pu / 现货 U 双协议）、缓冲回放
（丢旧、断档拒绝）、ingest 断档 resync、切片聚合、落库/区间查询/降采样、
保留期清理、限流门禁。
"""

from __future__ import annotations

import os
import tempfile
import time

import jarvis_orderbook as job

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "✅" if cond else "❌"
    print(f"{tag} {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ── 1. apply_diff：加档/改档/删档/脏数据 ──
bids: dict = {}
asks: dict = {}
job.apply_diff(bids, asks, {"b": [["100.0", "2"], ["99.5", "1"]],
                            "a": [["100.5", "3"]]})
check("apply_diff 建档", bids == {100.0: 2.0, 99.5: 1.0} and asks == {100.5: 3.0})
job.apply_diff(bids, asks, {"b": [["100.0", "5"], ["99.5", "0"]],
                            "a": [["bad", "x"], ["101.0", "1"]]})
check("apply_diff 改档+删档+脏数据跳过",
      bids == {100.0: 5.0} and asks == {100.5: 3.0, 101.0: 1.0})

# ── 2. 快照衔接 / 连续性（合约 pu 协议）──
check("covers 合约：U<=id<=u", job.covers_snapshot(90, 110, 89, 100))
check("covers 合约：id 越界拒绝", not job.covers_snapshot(101, 110, 100, 100))
check("continuous 合约：pu==prev_u", job.is_continuous(111, 110, 110))
check("continuous 合约：断档拒绝", not job.is_continuous(120, 115, 110))

# ── 3. 快照衔接 / 连续性（现货无 pu 协议）──
check("covers 现货：U<=id+1<=u", job.covers_snapshot(101, 105, None, 100))
check("covers 现货：越界拒绝", not job.covers_snapshot(102, 105, None, 100))
check("continuous 现货：U==prev_u+1", job.is_continuous(106, None, 105))
check("continuous 现货：断档拒绝", not job.is_continuous(108, None, 105))

# ── 4. replay_buffer：丢旧增量 + 衔接回放 + 断档拒绝 ──
b0 = {100.0: 1.0}
a0 = {101.0: 1.0}
buf = [
    {"U": 80, "u": 95, "pu": 79, "b": [["100.0", "9"]], "a": []},   # u<=id 丢弃
    {"U": 96, "u": 105, "pu": 95, "b": [["99.0", "2"]], "a": []},   # 覆盖 id=100
    {"U": 106, "u": 110, "pu": 105, "b": [], "a": [["102.0", "4"]]},
]
ok, last_u = job.replay_buffer(b0, a0, buf, 100)
check("replay 衔接回放", ok and last_u == 110
      and b0 == {100.0: 1.0, 99.0: 2.0} and a0 == {101.0: 1.0, 102.0: 4.0})
ok2, _ = job.replay_buffer({}, {}, [
    {"U": 96, "u": 105, "pu": 95, "b": [], "a": []},
    {"U": 120, "u": 130, "pu": 119, "b": [], "a": []},  # pu!=105 断档
], 100)
check("replay 断档拒绝", not ok2)
ok3, last3 = job.replay_buffer({1.0: 1.0}, {2.0: 1.0}, [], 100)
check("replay 空缓冲成功", ok3 and last3 == 100)

# ── 5. ingest：同步态连续应用 / 断档自动置 resync ──
job.reset_state()
st = job._sym_state("TESTUSDT")
st.update({"synced": True, "last_u": 100, "bids": {100.0: 1.0},
           "asks": {101.0: 1.0}, "market": "futures"})
job.ingest("TESTUSDT", {"U": 101, "u": 105, "pu": 100,
                        "b": [["99.0", "3"]], "a": []})
check("ingest 连续增量应用", st["last_u"] == 105 and st["bids"].get(99.0) == 3.0)
job.ingest("TESTUSDT", {"U": 101, "u": 105, "pu": 100, "b": [], "a": []})
check("ingest 重复帧丢弃", st["last_u"] == 105)
job.ingest("TESTUSDT", {"U": 120, "u": 125, "pu": 119,
                        "b": [["98.0", "1"]], "a": []})
check("ingest 断档置 resync", not st["synced"] and len(st["buffer"]) == 1
      and st["resyncs"] == 1 and 98.0 not in st["bids"])

# ── 6. slice_book：桶聚合 + 档数上限 ──
sl = job.slice_book({100.0: 1.0, 99.98: 2.0, 99.0: 1.0},
                    {100.4: 1.0, 100.42: 1.0, 102.0: 5.0}, levels=2)
check("slice_book 结构", sl is not None and sl["bucket"] > 0
      and len(sl["bids"]) == 2 and len(sl["asks"]) == 2)
check("slice_book 空簿返回 None", job.slice_book({}, {1.0: 1.0}) is None)

# ── 7. downsample_slices ──
rows = [{"ts": t, "bucket": 1.0, "bids": [], "asks": []}
        for t in (0, 20, 59, 60, 61, 130)]
out, trunc = job.downsample_slices(rows, 60)
check("downsample 每分钟末片", [r["ts"] for r in out] == [0, 60, 120]
      and not trunc, str([r["ts"] for r in out]))

# ── 8. 落库 / 区间查询 / 保留期（注入临时 SQLite）──
with tempfile.TemporaryDirectory() as td:
    dbp = os.path.join(td, "t.db")
    job.init_db(dbp)
    job.reset_state()
    now = time.time()
    st = job._sym_state("TESTUSDT")
    st.update({"synced": True, "market": "futures"})
    # 三片：两片在已完结分钟、一片在当前分钟（不落）
    cur_min = int(now) // 60
    st["slices"].append({"ts": (cur_min - 2) * 60 + 10, "bucket": 0.5,
                         "mid": 100.0, "bids": [[100.0, 1000.0]],
                         "asks": [[100.5, 900.0]]})
    st["slices"].append({"ts": (cur_min - 2) * 60 + 50, "bucket": 0.5,
                         "mid": 100.1, "bids": [[100.0, 1100.0]],
                         "asks": [[100.5, 800.0]]})
    st["slices"].append({"ts": cur_min * 60 + 5, "bucket": 0.5,
                         "mid": 100.2, "bids": [[100.0, 1200.0]],
                         "asks": [[100.5, 700.0]]})
    n = job.flush_slices(now_s=now, db_path=dbp)
    check("flush 分钟降采样（同分钟取末片、当前分钟不落）", n == 1)
    n2 = job.flush_slices(now_s=now, db_path=dbp)
    check("flush 幂等（水位推进后不重写）", n2 == 0)
    hm = job.heatmap("TESTUSDT", (cur_min - 5) * 60, int(now) + 60,
                     "1m", db_path=dbp)
    check("heatmap 库+内存合并", hm["ok"] and len(hm["slices"]) == 2
          and hm["slices"][0]["bids"][0][1] == 1100.0,
          repr(hm)[:200])
    removed = job.prune_old(now_s=now + 400 * 86400, db_path=dbp)
    check("保留期清理", removed >= 1)

# ── 9. 限流门禁 ──
job.reset_state()
st = job._sym_state("TESTUSDT")
now = time.time()
check("限流：首拉放行", job._snapshot_allowed(st, now))
st["snap_last_ts"] = now - 5
job._SNAP["global_last_ts"] = now - 10
check("限流：每币 30s 间隔拦截", not job._snapshot_allowed(st, now))
st["snap_last_ts"] = now - 60
job._SNAP["global_last_ts"] = now - 1
check("限流：全局 2s 间隔拦截", not job._snapshot_allowed(st, now))
job._SNAP["global_last_ts"] = now - 10
job._SNAP["ban_until"] = now + 60
check("限流：418 全局冷却短路", not job._snapshot_allowed(st, now))
job._SNAP["ban_until"] = 0.0
st["snap_fails"] = 2
st["snap_last_ts"] = now - 90
check("限流：失败退避（2 次失败需 120s）", not job._snapshot_allowed(st, now))
st["snap_last_ts"] = now - 130
check("限流：退避期满放行", job._snapshot_allowed(st, now))

job.reset_state()
print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {FAILS}")
    raise SystemExit(1)
print("✅ orderbook smoketest 全部通过")
