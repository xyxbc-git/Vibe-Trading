import {
  Globe,
  TrendingUp,
  TrendingDown,
  AlertTriangle,
  Link2,
  Activity,
  RefreshCw,
  Loader2,
  Clock,
  PlugZap,
} from "lucide-react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/useApi";

/** 数据源暂不可用（后端拉取失败且无缓存）时的卡片占位 */
function SourceDownNote({ error }: { error?: string | null }) {
  return (
    <div className="py-4 text-center">
      <p className="text-xs text-jarvis-text-secondary">数据源暂不可用，等待自动恢复</p>
      {error && (
        <p className="text-[10px] text-jarvis-text-secondary/60 mt-1 break-all">{error}</p>
      )}
    </div>
  );
}

/** 未接入徽标：该数据源需第三方 API key，尚未接入（灰化占位，不展示假数据） */
function NotConnectedBadge() {
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded border font-medium flex-shrink-0
                 bg-jarvis-border/30 text-jarvis-text-secondary border-jarvis-border"
      title="该数据源需第三方 API key，暂未接入，不展示演示数据"
    >
      未接入
    </span>
  );
}

/** 实时徽标：真实数据 + 最近拉取时间 */
function LiveBadge({ ts }: { ts?: number | null }) {
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded border font-medium flex-shrink-0
                 bg-jarvis-green/10 text-jarvis-green border-jarvis-green/40 flex items-center gap-1"
      title={ts ? `最近更新 ${new Date(ts * 1000).toLocaleString()}` : "真实数据"}
    >
      <Clock size={9} />
      {ts ? timeAgo(ts) : "实时"}
    </span>
  );
}

function timeAgo(unixSec: number): string {
  const diff = Math.max(0, Date.now() / 1000 - unixSec);
  if (diff < 90) return `${Math.round(diff)}s前`;
  if (diff < 5400) return `${Math.round(diff / 60)}m前`;
  return `${(diff / 3600).toFixed(1)}h前`;
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

export default function MarketIntel() {
  // 后端各源自带 TTL 缓存（资金费率 2min / OI 与多空比 5min / 恐贪 1h），前端 60s 轮询足够
  const { data, loading, error, refetch } = usePolling(() => api.marketIntel(), 60_000);

  const fng = data?.fng ?? null;
  const fundingRate = data?.funding_rate ?? null;
  const oi = data?.oi ?? null;
  const ls = data?.long_short ?? null;
  const errs = data?.errors ?? null;
  const unavailable = data?.unavailable ?? null;
  const notConnected = unavailable ? Object.keys(unavailable).length : 0;

  const fColor = fngColor(fng?.value ?? 50);

  // 首次加载：后端可能正在冷启动拉取外部源，给加载态而不是占位数字
  if (loading && !data && !error) {
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
            <span>情报接口请求失败：{error}</span>
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

      {!error && notConnected > 0 && (
        <div className="mb-4 flex items-start gap-2 p-3 rounded-lg bg-jarvis-border/20 border border-jarvis-border text-sm text-jarvis-text-secondary">
          <PlugZap size={16} className="flex-shrink-0 mt-0.5" />
          <span>
            爆仓数据与链上指标需第三方 API key（Coinglass / Glassnode），暂未接入、已灰化占位；
            其余卡片均为真实行情数据。
          </span>
        </div>
      )}

      <div className="grid grid-cols-3 gap-4 mb-4">
        {/* 恐慌贪婪指数（alternative.me，真实数据） */}
        <div className="card flex flex-col items-center">
          <div className="flex items-center gap-2 mb-3">
            <p className="stat-label mb-0">恐慌贪婪指数</p>
            {fng ? <LiveBadge ts={fng.ts} /> : null}
          </div>
          {fng ? (
            <div className="relative w-28 h-28 mb-2">
              <svg viewBox="0 0 120 120" className="w-full h-full">
                <circle cx="60" cy="60" r="50" fill="none" stroke="#30363d" strokeWidth="8" />
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
                <text x="60" y="75" textAnchor="middle" fill="#8b949e" fontSize="11">
                  {fngLabel(fng.value)}
                </text>
              </svg>
            </div>
          ) : (
            <SourceDownNote error={errs?.fng} />
          )}
        </div>

        {/* 资金费率（Binance premiumIndex，真实数据） */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <Activity size={14} />
            资金费率
            {fundingRate ? <LiveBadge ts={data?.funding_ts} /> : null}
          </p>
          {fundingRate ? (
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
                      {(rate * 100).toFixed(4)}%
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <SourceDownNote error={errs?.funding} />
          )}
        </div>

        {/* 持仓量 OI（Binance openInterestHist，真实数据） */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <TrendingUp size={14} />
            持仓量 (OI · {oi?.symbol?.replace("USDT", "") ?? "BTC"})
            {oi ? <LiveBadge ts={oi.ts} /> : null}
          </p>
          {oi ? (
            <>
              <p className="stat-value">{formatUsd(oi.value)}</p>
              {oi.change_pct != null ? (
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
              ) : (
                <p className="text-xs text-jarvis-text-secondary mt-2">24h 变化暂无数据</p>
              )}
            </>
          ) : (
            <SourceDownNote error={errs?.oi} />
          )}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* 多空比（Binance globalLongShortAccountRatio，真实数据） */}
        <div className="card">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            多空比 ({ls?.symbol?.replace("USDT", "") ?? "BTC"} 全体账户)
            {ls ? <LiveBadge ts={ls.ts} /> : null}
          </p>
          {ls ? (
            <>
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
            </>
          ) : (
            <SourceDownNote error={errs?.long_short} />
          )}
        </div>

        {/* 爆仓数据：需 Coinglass key，未接入（灰化占位，不展示假数据） */}
        <div className="card opacity-60">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <AlertTriangle size={14} />
            爆仓数据 (24h)
            <NotConnectedBadge />
          </p>
          <div className="py-5 text-center">
            <p className="text-xs text-jarvis-text-secondary">
              {unavailable?.liquidations ?? "需第三方 API key，暂未接入"}
            </p>
            <p className="text-[10px] text-jarvis-text-secondary/60 mt-1.5">
              配置 Coinglass key 后可展示多空爆仓金额
            </p>
          </div>
        </div>

        {/* 链上指标：需 Glassnode key，未接入（灰化占位，不展示假数据） */}
        <div className="card opacity-60">
          <p className="stat-label mb-3 flex items-center gap-1.5">
            <Link2 size={14} />
            链上指标
            <NotConnectedBadge />
          </p>
          <div className="py-5 text-center">
            <p className="text-xs text-jarvis-text-secondary">
              {unavailable?.onchain ?? "需第三方 API key，暂未接入"}
            </p>
            <p className="text-[10px] text-jarvis-text-secondary/60 mt-1.5">
              配置 Glassnode key 后可展示活跃地址 / 交易所净流入 / NVT
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
