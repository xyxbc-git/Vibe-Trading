"""离线 smoketest：T-06 数据源单点故障降级（Binance -> OKX 备用源）。不联网。

通过 monkeypatch jcd._get 模拟主源/备用源响应，验证：
  1) inst 代码映射正确
  2) Binance 主源失败时核心字段（标记价/资金费率/OI/现货价）自动切 OKX
  3) Binance 主源正常时不切备用源（_source==binance）
  4) collect 的 _sources 汇总与 to_markdown 降级提示
"""
import jarvis_crypto_data as jcd

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. instId 映射 ──
check("swap inst BTCUSDT", jcd._okx_swap_inst("BTCUSDT") == "BTC-USDT-SWAP", jcd._okx_swap_inst("BTCUSDT"))
check("swap inst ETHUSDT", jcd._okx_swap_inst("ETHUSDT") == "ETH-USDT-SWAP")
check("spot inst BTCUSDT", jcd._okx_spot_inst("BTCUSDT") == "BTC-USDT", jcd._okx_spot_inst("BTCUSDT"))
check("spot inst SOLUSD", jcd._okx_spot_inst("SOLUSD") == "SOL-USD")


# ── 公共 stub 工厂 ──
def make_get(binance_ok: bool):
    """binance_ok=False 时 binance 全失败、OKX 正常；True 时 binance 正常。"""
    def _stub(url, params=None, retries=4):
        is_binance = "binance.com" in url
        if is_binance and not binance_ok:
            return {"_error": "binance down"}
        if "binance.com" in url and "premiumIndex" in url:
            return {"lastFundingRate": "0.0001", "markPrice": "60000", "indexPrice": "59990"}
        if "binance.com" in url and "fundingRate" in url:
            return [{"fundingRate": "0.0001"}] * 21
        if "binance.com" in url and "openInterest" in url and "Hist" not in url:
            return {"openInterest": "12345.6"}
        if "binance.com" in url and "ticker/price" in url:
            return {"price": "59950"}
        # OKX 备用源响应
        if "okx.com" in url and "mark-price" in url:
            return {"code": "0", "data": [{"markPx": "60100"}]}
        if "okx.com" in url and "funding-rate" in url:
            return {"code": "0", "data": [{"fundingRate": "0.00012"}]}
        if "okx.com" in url and "open-interest" in url:
            return {"code": "0", "data": [{"oi": "98765.4"}]}
        if "okx.com" in url and "market/ticker" in url:
            return {"code": "0", "data": [{"last": "60050"}]}
        return {"_error": "unhandled"}
    return _stub


def _text_stub(url, retries=3):
    return None


jcd._get_text = _text_stub  # 避免链上接口真实联网

# ── 2. 主源失败 -> 切 OKX ──
jcd._get = make_get(binance_ok=False)

f = jcd.fetch_funding("BTCUSDT")
check("funding 切 OKX mark_price", f.get("mark_price") == 60100.0, str(f.get("mark_price")))
check("funding 切 OKX _source", f.get("_source") == "okx", str(f.get("_source")))
check("funding 切 OKX 无残留 error", "_funding_error" not in f)
check("funding OKX 费率非空", f.get("last_funding_rate_8h_pct") is not None, str(f.get("last_funding_rate_8h_pct")))

oi = jcd.fetch_oi("BTCUSDT")
check("oi 切 OKX 张数", oi.get("open_interest_contracts") == 98765.4, str(oi.get("open_interest_contracts")))
check("oi 切 OKX _source", oi.get("_source") == "okx")

ba = jcd.fetch_basis("BTCUSDT", mark_price=60100.0)
check("basis 切 OKX 现货价", ba.get("spot_price") == 60050.0, str(ba.get("spot_price")))
check("basis 切 OKX _source", ba.get("_source") == "okx")
check("basis 切 OKX 基差非空", ba.get("perp_spot_basis_pct") is not None, str(ba.get("perp_spot_basis_pct")))

# ── 3. 主源正常 -> 不切备用源 ──
jcd._get = make_get(binance_ok=True)

f2 = jcd.fetch_funding("BTCUSDT")
check("funding 主源 mark_price", f2.get("mark_price") == 60000.0, str(f2.get("mark_price")))
check("funding 主源 _source==binance", f2.get("_source") == "binance", str(f2.get("_source")))

oi2 = jcd.fetch_oi("BTCUSDT")
check("oi 主源 _source==binance", oi2.get("_source") == "binance")
check("oi 主源张数", oi2.get("open_interest_contracts") == 12345.6)

ba2 = jcd.fetch_basis("BTCUSDT", mark_price=60000.0)
check("basis 主源 _source==binance", ba2.get("_source") == "binance")
check("basis 主源现货价", ba2.get("spot_price") == 59950.0)

# ── 4. collect 汇总 + markdown 降级提示 ──
jcd._get = make_get(binance_ok=False)
data = jcd.collect("ETHUSDT")  # 非 BTC 避免 onchain 分支
srcs = data.get("_sources", {})
check("collect _sources funding okx", srcs.get("funding") == "okx", str(srcs))
md = jcd.to_markdown(data)
check("markdown 含降级提示", "数据降级" in md and "OKX" in md)

jcd._get = make_get(binance_ok=True)
data2 = jcd.collect("ETHUSDT")
md2 = jcd.to_markdown(data2)
check("markdown 主源无降级提示", "数据降级" not in md2)
check("collect 主源 funding==binance", data2.get("_sources", {}).get("funding") == "binance")

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
