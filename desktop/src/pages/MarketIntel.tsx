import { Globe, TrendingUp, TrendingDown, AlertTriangle, Link2, Activity } from "lucide-react";
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

const PLACEHOLDER: SnapshotData = {
  fng: { value: 35, classification: "恐惧" },
  funding_rate: { BTCUSDT: 0.0042, ETHUSDT: 0.0031, SOLUSDT: -0.0015, BNBUSDT: 0.0028 },
  oi: { value: 18_420_000_000, change_pct: 3.2 },
  long_short: { long_pct: 54.3, short_pct: 45.7, ratio: 1.19 },
  liquidations: { long_usd: 42_500_000, short_usd: 28_300_000, total_usd: 70_800_000 },
  onchain: { active_addresses: 982_451, exchange_inflow: 12_340, nvt: 45.2 },
};

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
  const { data: rawSnapshot } = usePolling(() => api.snapshot(), 30_000);
  const snap = (rawSnapshot as unknown as SnapshotData) ?? PLACEHOLDER;

  const fng = snap.fng ?? PLACEHOLDER.fng!;
  const fundingRate = snap.funding_rate ?? PLACEHOLDER.funding_rate!;
  const oi = snap.oi ?? PLACEHOLDER.oi!;
  const ls = snap.long_short ?? PLACEHOLDER.long_short!;
  const liq = snap.liquidations ?? PLACEHOLDER.liquidations!;
  const onchain = snap.onchain ?? PLACEHOLDER.onchain!;

  const fColor = fngColor(fng.value);

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Globe size={22} />
        市场情报
      </h1>

      <div className="grid grid-cols-3 gap-4 mb-4">
        {/* 恐慌贪婪指数 */}
        <div className="card flex flex-col items-center">
          <p className="stat-label mb-3">恐慌贪婪指数</p>
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
          <p className="stat-label mb-3">多空比</p>
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
