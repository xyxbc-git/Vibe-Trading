import { useState, useEffect, useCallback } from "react";
import { Settings as SettingsIcon, Save, RefreshCw, Wifi, Key, Palette } from "lucide-react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/useApi";

interface ScalperConfig {
  risk?: {
    daily_loss_limit?: number;
    daily_loss_action?: string;
    max_concurrent_positions?: number;
    single_trade_risk?: number;
    min_balance_to_trade?: number;
  };
  trading?: {
    always_on?: boolean;
    confidence_threshold?: number;
    aggressive_mode?: boolean;
    cool_down_bars?: number;
  };
  timeframe?: string;
  symbol?: string;
  evolve?: {
    max_rounds?: number;
    min_win_rate?: number;
    min_profit_factor?: number;
    max_drawdown_pct?: number;
    graveyard_similarity_threshold?: number;
  };
}

function NumberInput({
  label,
  value,
  onChange,
  step = 1,
  min,
  max,
  hint,
}: {
  label: string;
  value: number | undefined;
  onChange: (v: number) => void;
  step?: number;
  min?: number;
  max?: number;
  hint?: string;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-jarvis-border/50 last:border-0">
      <div>
        <p className="text-sm text-jarvis-text">{label}</p>
        {hint && <p className="text-xs text-jarvis-text-secondary">{hint}</p>}
      </div>
      <input
        type="number"
        value={value ?? ""}
        onChange={(e) => onChange(Number(e.target.value))}
        step={step}
        min={min}
        max={max}
        className="w-24 px-2 py-1 text-sm font-mono text-right bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
      />
    </div>
  );
}

function SelectInput({
  label,
  value,
  options,
  onChange,
  hint,
}: {
  label: string;
  value: string | undefined;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
  hint?: string;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-jarvis-border/50 last:border-0">
      <div>
        <p className="text-sm text-jarvis-text">{label}</p>
        {hint && <p className="text-xs text-jarvis-text-secondary">{hint}</p>}
      </div>
      <select
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="px-2 py-1 text-sm bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function ToggleInput({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: boolean | undefined;
  onChange: (v: boolean) => void;
  hint?: string;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-jarvis-border/50 last:border-0">
      <div>
        <p className="text-sm text-jarvis-text">{label}</p>
        {hint && <p className="text-xs text-jarvis-text-secondary">{hint}</p>}
      </div>
      <button
        onClick={() => onChange(!value)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          value ? "bg-jarvis-green" : "bg-jarvis-border"
        }`}
      >
        <span
          className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${
            value ? "translate-x-5" : ""
          }`}
        />
      </button>
    </div>
  );
}

export default function SettingsPage() {
  const { data: rawConfig, refetch } = useApi<ScalperConfig>(
    () => api.config() as Promise<ScalperConfig>,
  );

  const [config, setConfig] = useState<ScalperConfig>({});
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

  useEffect(() => {
    if (rawConfig) setConfig(rawConfig);
  }, [rawConfig]);

  const updateRisk = useCallback(
    (key: string, value: unknown) => {
      setConfig((c) => ({ ...c, risk: { ...c.risk, [key]: value } }));
    },
    [],
  );

  const updateTrading = useCallback(
    (key: string, value: unknown) => {
      setConfig((c) => ({ ...c, trading: { ...c.trading, [key]: value } }));
    },
    [],
  );

  const updateEvolve = useCallback(
    (key: string, value: unknown) => {
      setConfig((c) => ({ ...c, evolve: { ...c.evolve, [key]: value } }));
    },
    [],
  );

  const handleSave = async () => {
    setSaving(true);
    setSaveMsg("");
    try {
      const res = await api.updateConfig(config as Record<string, unknown>);
      if ((res as { ok?: boolean }).ok) {
        setSaveMsg("保存成功 ✓");
      } else {
        setSaveMsg(`保存失败: ${(res as { reason?: string }).reason ?? "未知错误"}`);
      }
    } catch (e) {
      setSaveMsg(`保存失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setSaving(false);
      setTimeout(() => setSaveMsg(""), 3000);
    }
  };

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <SettingsIcon size={22} />
        设置
      </h1>

      {/* 保存按钮栏 */}
      <div className="flex items-center gap-3 mb-4">
        <button onClick={handleSave} disabled={saving} className="btn-primary flex items-center gap-2">
          <Save size={14} />
          {saving ? "保存中..." : "保存配置"}
        </button>
        <button onClick={refetch} className="btn-primary flex items-center gap-2 !bg-jarvis-card border border-jarvis-border">
          <RefreshCw size={14} />
          重新加载
        </button>
        {saveMsg && (
          <span className={`text-sm ${saveMsg.includes("成功") ? "text-jarvis-green" : "text-jarvis-red"}`}>
            {saveMsg}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        {/* 风控参数 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
            🛡️ 风控参数
          </h3>
          <NumberInput
            label="单日亏损限额"
            value={config.risk?.daily_loss_limit}
            onChange={(v) => updateRisk("daily_loss_limit", v)}
            step={0.01}
            min={-1}
            max={0}
            hint="占总余额比例，如 -0.02 = 亏 2%"
          />
          <SelectInput
            label="触达限额后行为"
            value={config.risk?.daily_loss_action}
            options={[
              { value: "warn", label: "只告警" },
              { value: "pause", label: "暂停 1 小时" },
              { value: "stop", label: "停手" },
            ]}
            onChange={(v) => updateRisk("daily_loss_action", v)}
          />
          <NumberInput
            label="最大同时持仓数"
            value={config.risk?.max_concurrent_positions}
            onChange={(v) => updateRisk("max_concurrent_positions", v)}
            min={1}
            max={10}
          />
          <NumberInput
            label="单笔仓位占比"
            value={config.risk?.single_trade_risk}
            onChange={(v) => updateRisk("single_trade_risk", v)}
            step={0.005}
            min={0.001}
            max={0.1}
            hint="占总余额比例"
          />
          <NumberInput
            label="最低交易余额 (U)"
            value={config.risk?.min_balance_to_trade}
            onChange={(v) => updateRisk("min_balance_to_trade", v)}
            min={1}
            hint="低于此值停止交易"
          />
        </div>

        {/* 交易行为 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
            ⚡ 交易行为
          </h3>
          <ToggleInput
            label="永续交易"
            value={config.trading?.always_on}
            onChange={(v) => updateTrading("always_on", v)}
            hint="有余额就持续交易"
          />
          <NumberInput
            label="信号置信度门槛"
            value={config.trading?.confidence_threshold}
            onChange={(v) => updateTrading("confidence_threshold", v)}
            step={0.05}
            min={0}
            max={1}
            hint="0-1，越高越保守"
          />
          <ToggleInput
            label="激进模式"
            value={config.trading?.aggressive_mode}
            onChange={(v) => updateTrading("aggressive_mode", v)}
            hint="信号达标即果断进场"
          />
          <NumberInput
            label="冷却 K 线数"
            value={config.trading?.cool_down_bars}
            onChange={(v) => updateTrading("cool_down_bars", v)}
            min={0}
            max={20}
            hint="连续亏损后等待几根 K 线"
          />
          <SelectInput
            label="K 线周期"
            value={config.timeframe}
            options={[
              { value: "5m", label: "5 分钟" },
              { value: "15m", label: "15 分钟" },
              { value: "1h", label: "1 小时" },
            ]}
            onChange={(v) => setConfig((c) => ({ ...c, timeframe: v }))}
          />
          <SelectInput
            label="交易对"
            value={config.symbol}
            options={[
              { value: "BTCUSDT", label: "BTC/USDT" },
              { value: "ETHUSDT", label: "ETH/USDT" },
              { value: "SOLUSDT", label: "SOL/USDT" },
              { value: "BNBUSDT", label: "BNB/USDT" },
            ]}
            onChange={(v) => setConfig((c) => ({ ...c, symbol: v }))}
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        {/* 进化引擎参数 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
            🧬 进化引擎参数
          </h3>
          <NumberInput
            label="最大进化轮数"
            value={config.evolve?.max_rounds}
            onChange={(v) => updateEvolve("max_rounds", v)}
            min={1}
            max={100}
          />
          <NumberInput
            label="达标胜率 (%)"
            value={config.evolve?.min_win_rate}
            onChange={(v) => updateEvolve("min_win_rate", v)}
            min={50}
            max={80}
          />
          <NumberInput
            label="达标盈亏比"
            value={config.evolve?.min_profit_factor}
            onChange={(v) => updateEvolve("min_profit_factor", v)}
            step={0.1}
            min={1}
            max={5}
          />
          <NumberInput
            label="最大回撤 (%)"
            value={config.evolve?.max_drawdown_pct}
            onChange={(v) => updateEvolve("max_drawdown_pct", v)}
            min={5}
            max={50}
          />
          <NumberInput
            label="墓地查重阈值"
            value={config.evolve?.graveyard_similarity_threshold}
            onChange={(v) => updateEvolve("graveyard_similarity_threshold", v)}
            step={0.05}
            min={0.5}
            max={1}
            hint="相似度超过此值则重新生成"
          />
        </div>

        {/* 连接状态 + LLM + 主题 */}
        <div className="space-y-4">
          {/* QD 连接状态 */}
          <div className="card">
            <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
              <Wifi size={14} />
              连接状态
            </h3>
            <div className="flex items-center justify-between py-2">
              <span className="text-sm text-jarvis-text-secondary">Dashboard API</span>
              <span className="flex items-center gap-1.5 text-sm text-jarvis-green">
                <span className="w-2 h-2 rounded-full bg-jarvis-green" />
                在线
              </span>
            </div>
            <div className="flex items-center justify-between py-2 border-t border-jarvis-border/50">
              <span className="text-sm text-jarvis-text-secondary">端口</span>
              <span className="text-sm font-mono text-jarvis-text">7899</span>
            </div>
          </div>

          {/* LLM 配置 */}
          <div className="card">
            <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
              <Key size={14} />
              LLM API 配置
            </h3>
            <p className="text-xs text-jarvis-text-secondary mb-2">
              在项目根目录 .env 文件中设置：
            </p>
            <div className="bg-jarvis-bg rounded-md p-3 text-xs font-mono text-jarvis-text-secondary space-y-1">
              <p>DEEPSEEK_API_KEY=sk-xxx</p>
              <p># 或</p>
              <p>JARVIS_LLM_API_KEY=xxx</p>
              <p>JARVIS_LLM_BASE_URL=https://...</p>
            </div>
          </div>

          {/* 主题切换（占位） */}
          <div className="card">
            <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
              <Palette size={14} />
              主题
            </h3>
            <div className="flex gap-3">
              <button className="flex-1 py-2 rounded-lg bg-jarvis-bg border-2 border-jarvis-blue text-sm text-jarvis-text text-center">
                深色
              </button>
              <button className="flex-1 py-2 rounded-lg bg-jarvis-card border border-jarvis-border text-sm text-jarvis-text-secondary text-center opacity-50 cursor-not-allowed">
                浅色（开发中）
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
