import { Globe, TrendingUp, TrendingDown, AlertTriangle, Link2, Activity, RefreshCw, Loader2 } from "lucide-react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/useApi";

interface SnapshotData {
  fng?: { value: number; classification: string };
  funding_rate?: Record<string, number>;
  oi?: { value: number; change_pct: number };
  long_short?: { long_pct: number; short_pct: number; ratio: number };
  liquidations?: { long_usd: number; short_usd: number; total_usd: number };
  onchain?: { active_addresses: number; exchange_inflow: number; nvt: number };
}

// 仅在后端失败/字段缺失时兜底展示；所属卡片必须带「演示数据」水印（见 DemoBadge），
// 防止用户把占位行情当真实数据做交易决策。
const PLACEHOLDER: SnapshotData = {
  fng: { value: 35, classification: "恐惧" },
  funding_rate: { BTCUSDT: 0.0042, ETHUSDT: 0.0031, SOLUSDT: -0.0015, BNBUSDT: 0.0028 },
  oi: { value: 18_420_000_000, change_pct: 3.2 },
  long_short: { long_pct: 54.3, short_pct: 45.7, ratio: 1.19 },
  liquidations: { long_usd: 42_500_000, short_usd: 28_300_000, total_usd: 70_800_000 },
  onchain: { active_addresses: 982_451, exchange_inflow: 12_340, nvt: 45.2 },
};

/** 演示数据水印：占位数据卡片右上角的醒目标签 */
function DemoBadge() {
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded border font-medium flex-shrink-0
                 bg-jarvis-yellow/15 text-jarvis-yellow border-jarvis-yellow/40"
      title="后端行情数据不可用，当前展示的是内置演示数据，请勿作为交易依据"
    >
      演示数据
    </span>
  );
}

function fngColor(value: number): string {
  if (value <= 25) return "#f85149";
  if (value <= 45) return "#d29922";
  if (value <= 55) return "#8b949e";
  if (value <= 75) return "#58a6ff";
  return "#3fb950";
}

function fngLabel(value: number): string {
  if (value <= 25) return "极度恐惧";
  if (value <= 45) return "恐惧";
  if (value <= 55) return "中性";
  if (value <= 75) return "贪婪";
  return "极度贪婪";
}

function formatUsd(value: number): string {
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(2)}B`;
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

function formatNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toFixed(0);
}

export default function MarketIntel() {
  const {
    data: rawSnapshot,
    loading,
    error,
    refetch,
  } = usePolling(() => api.snapshot(), 30_000);
  const snap = (rawSnapshot as unknown as SnapshotData) ?? null;

  // 逐字段判定真实性：后端成功但个别字段缺失时，只给缺失卡片打演示水印
  const real = {
    fng: snap?.fng != null,
    funding: snap?.funding_rate != null,
    oi: snap?.oi != null,
    ls: snap?.long_short != null,
    liq: snap?.liquidations != null,
    onchain: snap?.onchain != null,
  };

  const fng = snap?.fng ?? PLACEHOLDER.fng!;
  const fundingRate = snap?.funding_rate ?? PLACEHOLDER.funding_rate!;
  const oi = snap?.oi ?? PLACEHOLDER.oi!;
  const ls = snap?.long_short ?? PLACEHOLDER.long_short!;
  const liq = snap?.liquidations ?? PLACEHOLDER.liquidations!;
  const onchain = snap?.onchain ?? PLACEHOLDER.onchain!;

  // 后端响应成功但一个真实字段都没有（当前 /api/snapshot 返回 brief 结构，
  // 顶层无本页字段）：全页演示数据，必须给显式横幅而不只是角标
  const allDemo =
    snap != null &&
    !real.fng && !real.funding && !real.oi && !real.ls && !real.liq && !real.onchain;

  const fColor = fngColor(fng.value);

  // 首次加载且无任何数据：不渲染占位卡片，避免演示数据闪现误导
  if (loading && !snap && !error) {
    return (
      <div>
        <h1 className="page-title flex items-center gap-2">
          <Globe size={22} />
          市场情报
        </h1>
        <div className="card h-[280px] flex items-center justify-center gap-2 text-jarvis-text-secondary text-sm">
          <Loader2 size={16} className="animate-spin" />
          正在获取市场行情...
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Globe size={22} />
        市场情报
      </h1>

      {error && (
        <div className="mb-4 flex items-center justify-between gap-3 p-3 rounded-lg bg-jarvis-red/10 border border-jarvis-red/40">
          <div className="flex items-start gap-2 text-sm text-jarvis-red">
            <AlertTriangle size={16} className="flex-shrink-0 mt-0.5" />
            <span>
              行情接口请求失败：{error}。
              {!snap && "下方全部为内置演示数据，请勿作为交易依据。"}
            </span>
          </div>
          <button
            onClick={refetch}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border border-jarvis-red/40 text-jarvis-red hover:bg-jarvis-red/10 transition-colors flex-shrink-0 disabled:opacity-50"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
            {loading ? "重试中..." : "重试"}
          </button>
        </div>
      )}

      {!error && allDemo && (
        <div className="mb-4 flex items-start gap-2 p-3 rounded-lg bg-jarvis-yellow/10 border border-jarvis-yellow/40 text-sm text-jarvis-yellow">
          <AlertTriangle size={16} className="flex-shrink-0 mt-0.5" />
          <span>
            后端行情快照中暂无本页所需字段，下方全部为内置演示数据，请勿作为交易依据。
          </span>
        </div>
      )}

      <div className="grid grid-cols-3 gap-4 mb-4">
        {/* 恐慌贪婪指数 */}
        <div className="card flex flex-col items-center">
          <div className="flex items-center gap-2 mb-3">
            <p className="stat-label mb-0">恐慌贪婪指数</p>
            {!real.fng && <DemoBadge />}
          </div>
          <div className="relative w-28 h-28 mb-2">
            <svg viewBox="0 0 120 120" className="w-full h-full">
              <circle
                cx="60"
                cy="60"
                r="50"
                fill="none"
                stroke="#30363d"
                strokeWidth="8"
              />
              <circle
                cx="60"
                cy="60"
                r="50"
                fill="none"
                stroke={fColor}
                strokeWidth="8"
                strokeDasharray={`${(fng.value / 100) * 314} 314`}
                strokeLinecap="round"
                transform="rotate(-90 60 60)"
                className="transition-all duration-700"
              />
              <text
                x="60"
                y="55"
                textAnchor="middle"
                fill={fColor}
                fontSize="28"
                fontWeight="bold"
                fontFamily="SF Mono, monospace"
              >
                {fng.value}
              </text>
              <text
                x="60"
                y="75"
                textAnchor="middle"
                fill="#8b949e"
                fontSize="11"
              >
                {fngLabel(fng.value)}
              </text>
            </svg>
          </div>
        </div>

        {/* 资金费率 */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <Activity size={14} />
            资金费率
            {!real.funding && <DemoBadge />}
          </p>
          <div className="space-y-2.5">
            {Object.entries(fundingRate).map(([symbol, rate]) => (
              <div key={symbol} className="flex items-center justify-between">
                <span className="text-xs text-jarvis-text">{symbol.replace("USDT", "")}</span>
                <div className="flex items-center gap-2">
                  <div className="w-20 h-1.5 bg-jarvis-border rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{
                        width: `${Math.min(Math.abs(rate) * 5000, 100)}%`,
                        backgroundColor: rate >= 0 ? "#3fb950" : "#f85149",
                      }}
                    />
                  </div>
                  <span
                    className={`text-xs font-mono w-16 text-right ${
                      rate >= 0 ? "text-jarvis-green" : "text-jarvis-red"
                    }`}
                  >
                    {rate >= 0 ? "+" : ""}
                    {(rate * 100).toFixed(3)}%
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* OI 变化 */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <TrendingUp size={14} />
            持仓量 (OI)
            {!real.oi && <DemoBadge />}
          </p>
          <p className="stat-value">{formatUsd(oi.value)}</p>
          <div className="flex items-center gap-1 mt-2">
            {oi.change_pct >= 0 ? (
              <TrendingUp size={14} className="text-jarvis-green" />
            ) : (
              <TrendingDown size={14} className="text-jarvis-red" />
            )}
            <span
              className={`text-sm font-mono ${
                oi.change_pct >= 0 ? "text-jarvis-green" : "text-jarvis-red"
              }`}
            >
              {oi.change_pct >= 0 ? "+" : ""}
              {oi.change_pct.toFixed(1)}%
            </span>
            <span className="text-xs text-jarvis-text-secondary ml-1">24h</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* 多空比 */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            多空比
            {!real.ls && <DemoBadge />}
          </p>
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-jarvis-green">多 {ls.long_pct.toFixed(1)}%</span>
            <span className="text-sm text-jarvis-text font-mono">{ls.ratio.toFixed(2)}</span>
            <span className="text-xs text-jarvis-red">空 {ls.short_pct.toFixed(1)}%</span>
          </div>
          <div className="w-full h-3 rounded-full overflow-hidden flex">
            <div
              className="h-full bg-jarvis-green transition-all"
              style={{ width: `${ls.long_pct}%` }}
            />
            <div
              className="h-full bg-jarvis-red transition-all"
              style={{ width: `${ls.short_pct}%` }}
            />
          </div>
          <div className="mt-3 text-center">
            <span className="text-xs text-jarvis-text-secondary">
              {ls.long_pct > ls.short_pct ? "📈 多头主导" : "📉 空头主导"}
            </span>
          </div>
        </div>

        {/* 爆仓数据 */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <AlertTriangle size={14} className="text-jarvis-yellow" />
            爆仓数据 (24h)
            {!real.liq && <DemoBadge />}
          </p>
          <p className="stat-value mb-2">{formatUsd(liq.total_usd)}</p>
          <div className="space-y-1.5">
            <div className="flex justify-between text-xs">
              <span className="text-jarvis-green">多头爆仓</span>
              <span className="text-jarvis-green font-mono">{formatUsd(liq.long_usd)}</span>
            </div>
            <div className="flex justify-between text-xs">
              <span className="text-jarvis-red">空头爆仓</span>
              <span className="text-jarvis-red font-mono">{formatUsd(liq.short_usd)}</span>
            </div>
          </div>
        </div>

        {/* 链上指标 */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <Link2 size={14} className="text-jarvis-purple" />
            链上指标
            {!real.onchain && <DemoBadge />}
          </p>
          <div className="space-y-3">
            <div>
              <span className="text-xs text-jarvis-text-secondary">活跃地址</span>
              <p className="text-sm text-jarvis-text font-mono">{formatNumber(onchain.active_addresses)}</p>
            </div>
            <div>
              <span className="text-xs text-jarvis-text-secondary">交易所流入 (BTC)</span>
              <p className="text-sm text-jarvis-text font-mono">{formatNumber(onchain.exchange_inflow)}</p>
            </div>
            <div>
              <span className="text-xs text-jarvis-text-secondary">NVT 比率</span>
              <p className="text-sm text-jarvis-text font-mono">{onchain.nvt.toFixed(1)}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
