"""Offline smoketest for T-05 WF / Purged K-Fold in jarvis_factor_validate. No network."""
import jarvis_factor_validate as jfv

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# 1. _signal_indices breakout20 must equal an independent naive impl (same factor as production)
closes = [100 + i for i in range(40)]   # strictly increasing -> every i>=19 is a 20d high
dd = [0.0] * 40
sig = jfv._signal_indices("breakout20", closes, dd, 0.0)
naive = {i for i in range(40) if i >= 19 and closes[i] >= max(closes[i - 19:i + 1])}
check("breakout20 == naive", sig == naive, str(sorted(sig))[:60])
check("breakout20 starts at 19", min(sig) == 19 and len(sig) == 40 - 19)

closes2 = [100 + (i % 7) - (i % 3) for i in range(50)]
dd2 = [0.0] * 50
sig2 = jfv._signal_indices("breakout20", closes2, dd2, 0.0)
naive2 = {i for i in range(50) if i >= 19 and closes2[i] >= max(closes2[i - 19:i + 1])}
check("breakout20 nonmono == naive", sig2 == naive2, str(sorted(sig2))[:60])

# 2. _signal_indices drawdown
sig3 = jfv._signal_indices("drawdown", [1, 2, 3, 4, 5], [0.0, -0.1, -0.31, -0.5, -0.2], -0.30)
check("drawdown threshold", sig3 == {2, 3}, str(sig3))

# 3. _seg_edge math: sig rets .10,.20 (mean .15); base mean .05; edge .10
seg = jfv._seg_edge([(0, 0.10), (1, 0.00), (2, 0.20), (3, -0.10)], {0, 2})
check("seg edge_raw .10", abs(seg["edge_raw"] - 0.10) < 1e-9, str(seg))
check("seg n_signal 2", seg["n_signal"] == 2)
check("seg win 100", seg["win_pct"] == 100.0)
check("seg no-signal None", jfv._seg_edge([(0, 0.1)], set())["edge_raw"] is None)

# 4. _stability math
st = jfv._stability([0.02, -0.01, 0.03])
check("stability pos ratio 66.7", st["positive_ratio_pct"] == 66.7, str(st))
check("stability windows 3", st["windows_with_edge"] == 3)
check("stability empty None", jfv._stability([None, None])["mean_edge_pct"] is None)

dates = ["d%02d" % i for i in range(20)]

# 5. walk_forward windowing (monkeypatch _prepare; sig==all -> per-window edge ~0)
fr = [(i, 0.01 * (1 if i % 2 == 0 else -1)) for i in range(20)]
jfv._prepare = lambda factor, threshold, horizon: (dates, list(range(20)), fr, set(range(20)))
wf = jfv.walk_forward("breakout20", 0.0, 3, n_windows=4)
check("wf 4 windows", wf["n_windows"] == 4, str(wf["n_windows"]))
check("wf windows ordered", [w["window"] for w in wf["windows"]] == [1, 2, 3, 4])
check("wf edges ~0 (sig==all)", all(abs(w["edge_pct"]) < 1e-6 for w in wf["windows"]),
      str([w["edge_pct"] for w in wf["windows"]]))

# 6. purged_kfold purge-gap proof: guard=horizon+embargo=5; fold1 val=pos0..4, train=pos10..19
#    signal at pos7 (inside purge gap) must be EXCLUDED from train; pos15 (in train) included.
fr2 = [(i, 0.0) for i in range(20)]
jfv._prepare = lambda factor, threshold, horizon: (dates, list(range(20)), fr2, {7, 15})
pk = jfv.purged_kfold("breakout20", 0.0, horizon=3, k=4, embargo=2)
check("pk guard 5", pk["purge_guard_days"] == 5, str(pk["purge_guard_days"]))
check("pk 4 folds", len(pk["folds"]) == 4)
check("pk fold1 purges sig7, keeps sig15 (train_n_signal==1)",
      pk["folds"][0]["train_n_signal"] == 1, str(pk["folds"][0]))

print("\n=== " + ("ALL PASS" if not fails else "FAILED %d: %s" % (len(fails), fails)) + " ===")
raise SystemExit(1 if fails else 0)
