import { useState, useEffect, useCallback, useMemo } from "react";
import { Settings as SettingsIcon, Save, RefreshCw, Wifi, Key, Palette, Server, Plug, Download, ShieldAlert, OctagonX, Coins, ChevronDown, Loader2, SlidersHorizontal } from "lucide-react";
import { api, type QdConfig, type QdConfigTest, type TradingConfig, type CircuitBreakerStatus, type CooldownResponse, type LlmConfig, type LlmTestResult, type LlmUsageResponse, type LlmUsageRecent, type LlmUsageDetail, type ConfigCenterResponse, type ConfigGroupName, type ConfigFieldMeta } from "@/api/client";
import { useApi } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { ACCENT_THEMES, applyAccent, getAccent } from "@/lib/theme";

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

function QdGatewayCard() {
  const { data: qd, refetch } = useApi<QdConfig>(() => api.qdConfig());
  const [gatewayBase, setGatewayBase] = useState("");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<QdConfigTest | null>(null);
  const [showIssue, setShowIssue] = useState(false);
  const [issueUser, setIssueUser] = useState("quantdinger");
  const [issuePass, setIssuePass] = useState("");
  const [issuing, setIssuing] = useState(false);
  const [issueMsg, setIssueMsg] = useState("");

  useEffect(() => {
    if (qd) setGatewayBase(qd.gateway_base ?? "");
  }, [qd]);

  const handleIssue = async () => {
    if (!issuePass.trim()) {
      setIssueMsg("请填写密码");
      return;
    }
    setIssuing(true);
    setIssueMsg("");
    try {
      const res = await api.issueQdToken({
        username: issueUser.trim() || "quantdinger",
        password: issuePass.trim(),
        scopes: "R,B",
        gateway_base: gatewayBase.trim() || undefined,
      });
      if (res.ok) {
        setIssueMsg(`签发成功 ✓ ${res.agent_token_masked ?? ""}`);
        setIssuePass("");
        setShowIssue(false);
        refetch();
      } else {
        setIssueMsg(`签发失败: ${res.reason ?? "未知错误"}`);
      }
    } catch (e) {
      setIssueMsg(`签发失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setIssuing(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.testQdConfig();
      setTestResult(res);
    } catch (e) {
      setTestResult({
        ok: false,
        reason: e instanceof Error ? e.message : "网络错误",
      });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const payload: { gateway_base?: string; agent_token?: string } = {
        gateway_base: gatewayBase.trim(),
      };
      if (token.trim()) payload.agent_token = token.trim();
      const res = await api.updateQdConfig(payload);
      if (res.ok) {
        setMsg("保存成功 ✓");
        setToken("");
        refetch();
      } else {
        setMsg(`保存失败: ${res.reason ?? "未知错误"}`);
      }
    } catch (e) {
      setMsg(`保存失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setSaving(false);
      setTimeout(() => setMsg(""), 3000);
    }
  };

  return (
    <div className="card">
      <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
        <Server size={14} />
        QD 网关 + Agent Token
      </h3>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        回测子进程每次运行时重读此配置，保存后无需重启 Dashboard。
      </p>

      <div className="py-2 border-b border-jarvis-border/50">
        <p className="text-sm text-jarvis-text mb-1.5">网关地址</p>
        <input
          type="text"
          value={gatewayBase}
          onChange={(e) => setGatewayBase(e.target.value)}
          placeholder="http://localhost:8888"
          className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
        />
        {qd?.env_base_active && (
          <p className="text-xs text-jarvis-yellow mt-1">
            ⚠ 已设置 QUANTDINGER_GATEWAY_BASE 环境变量，将覆盖此处配置
          </p>
        )}
      </div>

      <div className="py-2">
        <div className="flex items-center justify-between mb-1.5">
          <p className="text-sm text-jarvis-text">Agent Token</p>
          <div className="flex items-center gap-2">
            {qd?.has_token && (
              <span className="text-xs font-mono text-jarvis-text-secondary">
                当前：{qd.agent_token_masked}
              </span>
            )}
            <button
              onClick={() => setShowIssue((v) => !v)}
              className="flex items-center gap-1 text-xs text-jarvis-blue hover:underline"
            >
              <Download size={12} />
              自动获取
            </button>
          </div>
        </div>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={qd?.has_token ? "留空表示不修改" : "粘贴 QD Agent Token（scope 需含 B）"}
          autoComplete="off"
          className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
        />
        {qd?.env_token_active && (
          <p className="text-xs text-jarvis-yellow mt-1">
            ⚠ 已设置 QUANTDINGER_AGENT_TOKEN 环境变量，将覆盖此处 Token
          </p>
        )}

        {showIssue && (
          <div className="mt-2 bg-jarvis-bg rounded-md p-3 space-y-2 border border-jarvis-border">
            <p className="text-xs text-jarvis-text-secondary">
              用 QD 账号密码登录并自动签发 token（scope R,B · paper-only），成功后写入配置。
            </p>
            <input
              type="text"
              value={issueUser}
              onChange={(e) => setIssueUser(e.target.value)}
              placeholder="QD 账号（默认 quantdinger）"
              autoComplete="off"
              className="w-full px-2 py-1.5 text-sm bg-jarvis-card border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
            />
            <input
              type="password"
              value={issuePass}
              onChange={(e) => setIssuePass(e.target.value)}
              placeholder="QD 密码"
              autoComplete="off"
              className="w-full px-2 py-1.5 text-sm bg-jarvis-card border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
            />
            <div className="flex items-center gap-3">
              <button
                onClick={handleIssue}
                disabled={issuing}
                className="btn-primary flex items-center gap-2"
              >
                <Download size={14} />
                {issuing ? "签发中..." : "登录并签发"}
              </button>
              {issueMsg && (
                <span className={`text-xs ${issueMsg.includes("成功") ? "text-jarvis-green" : "text-jarvis-red"}`}>
                  {issueMsg}
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="flex items-center gap-3 mt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn-primary flex items-center gap-2"
        >
          <Save size={14} />
          {saving ? "保存中..." : "保存网关配置"}
        </button>
        <button
          onClick={handleTest}
          disabled={testing}
          className="btn-primary flex items-center gap-2 !bg-jarvis-card border border-jarvis-border"
        >
          <Plug size={14} />
          {testing ? "测试中..." : "连接测试"}
        </button>
        {msg && (
          <span className={`text-sm ${msg.includes("成功") ? "text-jarvis-green" : "text-jarvis-red"}`}>
            {msg}
          </span>
        )}
      </div>

      {testResult && (
        <div className="mt-3 bg-jarvis-bg rounded-md p-3 text-xs space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-jarvis-text-secondary">网关健康</span>
            <span className={testResult.healthy ? "text-jarvis-green" : "text-jarvis-red"}>
              {testResult.healthy ? "✓ 可达" : "✗ 不可达"}
              {testResult.health_error ? ` (${testResult.health_error})` : ""}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-jarvis-text-secondary">Token 有效性</span>
            <span className={testResult.token_valid ? "text-jarvis-green" : "text-jarvis-red"}>
              {testResult.token_valid ? "✓ 有效" : "✗ 无效"}
              {testResult.token_error ? ` (${testResult.token_error})` : ""}
            </span>
          </div>
          {testResult.reason && (
            <p className="text-jarvis-red">错误：{testResult.reason}</p>
          )}
          {testResult.whoami && (
            <pre className="text-jarvis-text-secondary whitespace-pre-wrap break-all pt-1 border-t border-jarvis-border/50">
              {JSON.stringify(testResult.whoami, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

const LLM_PROVIDERS = [
  {
    id: "deepseek",
    label: "DeepSeek（推荐）",
    base: "https://api.deepseek.com",
    model: "deepseek-chat",
    keyHint: "sk- 开头，platform.deepseek.com 获取",
  },
  {
    id: "openai",
    label: "OpenAI",
    base: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    keyHint: "sk- 开头，platform.openai.com 获取",
  },
  {
    id: "qwen",
    label: "通义千问",
    base: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    keyHint: "sk- 开头，bailian.console.aliyun.com 获取",
  },
  {
    id: "moonshot",
    label: "Kimi",
    base: "https://api.moonshot.cn/v1",
    model: "moonshot-v1-8k",
    keyHint: "sk- 开头，platform.moonshot.cn 获取",
  },
  {
    id: "custom",
    label: "自定义 / 兼容中转",
    base: "",
    model: "",
    keyHint: "任意 OpenAI 兼容服务的 Key（含 Ollama）",
  },
] as const;

function LlmConfigCard() {
  const { data: cfg, refetch } = useApi<LlmConfig>(() => api.llmConfig());
  const [provider, setProvider] = useState("deepseek");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [temperature, setTemperature] = useState(0.5);
  const [maxTokens, setMaxTokens] = useState(900);
  const [promptExtra, setPromptExtra] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null);

  useEffect(() => {
    if (!cfg) return;
    setProvider(cfg.provider || "deepseek");
    setBaseUrl(cfg.base_url ?? "");
    setModel(cfg.model ?? "");
    if (typeof cfg.temperature === "number") setTemperature(cfg.temperature);
    if (typeof cfg.max_tokens === "number") setMaxTokens(cfg.max_tokens);
    setPromptExtra(cfg.system_prompt_extra ?? "");
  }, [cfg]);

  const preset = LLM_PROVIDERS.find((p) => p.id === provider) ?? LLM_PROVIDERS[0];

  const handleProviderChange = (id: string) => {
    setProvider(id);
    const p = LLM_PROVIDERS.find((x) => x.id === id);
    if (p && p.id !== "custom") {
      setBaseUrl(p.base);
      setModel(p.model);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const payload: {
        provider: string;
        base_url: string;
        model: string;
        api_key?: string;
        temperature: number;
        max_tokens: number;
        system_prompt_extra: string;
      } = {
        provider,
        base_url: baseUrl.trim(),
        model: model.trim(),
        temperature,
        max_tokens: maxTokens,
        system_prompt_extra: promptExtra.trim(),
      };
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      const res = await api.updateLlmConfig(payload);
      if (res.ok) {
        setMsg("保存成功 ✓ 立即生效，无需重启");
        setApiKey("");
        refetch();
      } else {
        setMsg(`保存失败: ${res.reason ?? "未知错误"}`);
      }
    } catch (e) {
      setMsg(`保存失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setSaving(false);
      setTimeout(() => setMsg(""), 4000);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.testLlmConfig();
      setTestResult(res);
    } catch (e) {
      setTestResult({
        ok: false,
        error: e instanceof Error ? e.message : "网络错误",
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="card">
      <h3 className="text-sm font-semibold text-jarvis-text mb-1 flex items-center gap-2">
        <Key size={14} />
        大模型 (LLM)
        {cfg?.configured ? (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-green/15 text-jarvis-green font-normal">
            已配置{cfg.source === "env" ? "（来自环境变量）" : ""}
          </span>
        ) : (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-yellow/15 text-jarvis-yellow font-normal">
            未配置
          </span>
        )}
      </h3>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        AI 策略工坊、策略进化、AI 问答共用此配置。填入 API Key 即可，保存立即生效。
      </p>

      <div className="py-2 border-b border-jarvis-border/50">
        <p className="text-sm text-jarvis-text mb-1.5">服务商</p>
        <div className="flex gap-2">
          {LLM_PROVIDERS.map((p) => (
            <button
              key={p.id}
              onClick={() => handleProviderChange(p.id)}
              className={`flex-1 px-2 py-1.5 text-xs rounded-md border transition-colors ${
                provider === p.id
                  ? "border-jarvis-blue text-jarvis-blue bg-jarvis-blue/10"
                  : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="py-2 border-b border-jarvis-border/50">
        <div className="flex items-center justify-between mb-1.5">
          <p className="text-sm text-jarvis-text">API Key</p>
          {cfg?.has_key && (
            <span className="text-xs font-mono text-jarvis-text-secondary">
              当前：{cfg.api_key_masked}
            </span>
          )}
        </div>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={cfg?.has_key ? "留空表示不修改" : preset.keyHint}
          autoComplete="off"
          className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
        />
        {cfg?.env_fallback_available && !cfg.has_key && (
          <p className="text-xs text-jarvis-text-secondary mt-1">
            当前正使用 .env / 环境变量里的 Key；在此保存后将优先使用这里的配置。
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 py-2">
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">Base URL</p>
          <input
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={preset.base || "https://api.xxx.com/v1"}
            className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
          />
        </div>
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">模型名</p>
          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={preset.model || "如 deepseek-chat"}
            className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 py-2 border-t border-jarvis-border/50">
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">
            回答温度
            <span className="text-xs text-jarvis-text-secondary ml-1.5">
              0=严谨 · 2=发散
            </span>
          </p>
          <input
            type="number"
            value={temperature}
            onChange={(e) => setTemperature(Number(e.target.value))}
            step={0.1}
            min={0}
            max={2}
            className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
          />
        </div>
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">
            回答长度上限
            <span className="text-xs text-jarvis-text-secondary ml-1.5">tokens</span>
          </p>
          <input
            type="number"
            value={maxTokens}
            onChange={(e) => setMaxTokens(Number(e.target.value))}
            step={100}
            min={100}
            max={8000}
            className="w-full px-2 py-1.5 text-sm font-mono bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
          />
        </div>
      </div>

      <div className="py-2 border-t border-jarvis-border/50">
        <p className="text-sm text-jarvis-text mb-1.5">
          助手人格补充
          <span className="text-xs text-jarvis-text-secondary ml-1.5">
            追加到内置提示词后，如「回答尽量简短」「多用表格」
          </span>
        </p>
        <textarea
          value={promptExtra}
          onChange={(e) => setPromptExtra(e.target.value)}
          rows={2}
          maxLength={1000}
          placeholder="留空使用默认人格"
          className="w-full px-2 py-1.5 text-sm bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue resize-y"
        />
      </div>

      <div className="flex items-center gap-3 mt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn-primary flex items-center gap-2"
        >
          <Save size={14} />
          {saving ? "保存中..." : "保存 LLM 配置"}
        </button>
        <button
          onClick={handleTest}
          disabled={testing}
          className="btn-primary flex items-center gap-2 !bg-jarvis-card border border-jarvis-border"
        >
          <Plug size={14} />
          {testing ? "测试中..." : "连接测试"}
        </button>
        {msg && (
          <span className={`text-sm ${msg.includes("成功") ? "text-jarvis-green" : "text-jarvis-red"}`}>
            {msg}
          </span>
        )}
      </div>

      {testResult && (
        <div className="mt-3 bg-jarvis-bg rounded-md p-3 text-xs space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-jarvis-text-secondary">模型连通性</span>
            <span className={testResult.ok ? "text-jarvis-green" : "text-jarvis-red"}>
              {testResult.ok
                ? `✓ 可用（${testResult.model} · ${testResult.latency_ms}ms）`
                : `✗ 不可用`}
            </span>
          </div>
          {testResult.ok && testResult.reply && (
            <div className="flex items-center justify-between">
              <span className="text-jarvis-text-secondary">模型回复</span>
              <span className="text-jarvis-text">{testResult.reply}</span>
            </div>
          )}
          {!testResult.ok && testResult.error && (
            <p className="text-jarvis-red break-all">错误：{testResult.error}</p>
          )}
        </div>
      )}
    </div>
  );
}

function ThemeCard() {
  const [accent, setAccent] = useState(getAccent());

  const handlePick = (id: string) => {
    applyAccent(id);
    setAccent(id);
  };

  return (
    <div className="card">
      <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
        <Palette size={14} />
        主题
      </h3>

      <p className="text-xs text-jarvis-text-secondary mb-2">强调色（即时生效，自动保存）</p>
      <div className="grid grid-cols-3 gap-2 mb-3">
        {ACCENT_THEMES.map((t) => {
          const active = accent === t.id;
          return (
            <button
              key={t.id}
              onClick={() => handlePick(t.id)}
              aria-pressed={active}
              className={`flex items-center gap-2 px-2.5 py-2 rounded-lg border text-xs transition-colors ${
                active
                  ? "border-jarvis-blue bg-jarvis-blue/10 text-jarvis-text"
                  : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-text-secondary"
              }`}
            >
              <span
                className="w-4 h-4 rounded-full shrink-0 border border-black/30"
                style={{ backgroundColor: t.preview }}
              />
              {t.name}
              {active && <span className="ml-auto text-jarvis-blue">✓</span>}
            </button>
          );
        })}
      </div>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        涨绿跌红等盈亏色不随主题变化；图表内部线色暂不联动。
      </p>

      <p className="text-xs text-jarvis-text-secondary mb-2">底色模式</p>
      <div className="flex gap-3">
        <button className="flex-1 py-2 rounded-lg bg-jarvis-bg border-2 border-jarvis-blue text-sm text-jarvis-text text-center">
          深色
        </button>
        <button
          disabled
          className="flex-1 py-2 rounded-lg bg-jarvis-card border border-jarvis-border text-sm text-jarvis-text-secondary text-center opacity-50 cursor-not-allowed"
        >
          浅色（开发中）
        </button>
      </div>
    </div>
  );
}

const MODULE_LABELS: Record<string, string> = {
  ask: "AI 问答",
  reason: "深度推理",
  review: "交易复盘",
  strategy_gen: "策略工坊",
  strategy_evolve: "策略进化",
  scalper_evolve: "短线进化",
  test: "连接测试",
  unknown: "其它",
};

function fmtTokens(n: number | undefined | null): string {
  const v = Number(n ?? 0);
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return String(v);
}

function fmtCost(n: number | undefined | null): string {
  const v = Number(n ?? 0);
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

/** prompt_text（messages JSON 字符串）→ 结构化消息数组；解析失败返回 null 走原文展示 */
function parsePromptMessages(
  text: string | null | undefined,
): { role: string; content: string }[] | null {
  if (!text) return null;
  try {
    const arr: unknown = JSON.parse(text);
    if (Array.isArray(arr)) {
      return arr.filter(
        (m): m is { role: string; content: string } =>
          !!m && typeof (m as { content?: unknown }).content === "string",
      );
    }
  } catch {
    /* 非 JSON（异常数据）走原文展示 */
  }
  return null;
}

function LlmLogDetailView({ detail }: { detail: LlmUsageDetail }) {
  const messages = parsePromptMessages(detail.prompt_text);
  const roleLabel: Record<string, string> = {
    system: "系统提示词",
    user: "发送内容",
    assistant: "历史回复",
    note: "省略说明",
  };
  return (
    <div className="mt-1.5 space-y-2 border-t border-jarvis-border/50 pt-2">
      <p className="text-[11px] text-jarvis-text-secondary font-mono">
        {detail.model ?? "—"} · 输入 {fmtTokens(detail.prompt_tokens)} / 输出{" "}
        {fmtTokens(detail.completion_tokens)} tok{detail.estimated ? "（估算）" : ""} ·{" "}
        {detail.latency_ms != null ? `${detail.latency_ms}ms` : "—"} · {fmtCost(detail.cost_usd)}
        {detail.error ? ` · 错误：${detail.error}` : ""}
      </p>

      <div>
        <p className="text-[11px] text-jarvis-text-secondary mb-1">发送 ↑</p>
        {messages ? (
          <div className="space-y-1">
            {messages.map((m, i) =>
              m.role === "system" ? (
                <details key={i} className="bg-jarvis-card rounded border border-jarvis-border/60">
                  <summary className="px-2 py-1 text-[11px] text-jarvis-text-secondary cursor-pointer select-none">
                    {roleLabel.system}（点击展开 · {m.content.length} 字）
                  </summary>
                  <pre className="px-2 pb-1.5 text-[11px] leading-relaxed text-jarvis-text-secondary whitespace-pre-wrap break-all max-h-40 overflow-y-auto select-text">
                    {m.content}
                  </pre>
                </details>
              ) : (
                <div key={i} className="bg-jarvis-card rounded border border-jarvis-border/60 px-2 py-1.5">
                  <p className="text-[10px] text-jarvis-text-secondary mb-0.5">
                    {roleLabel[m.role] ?? m.role}
                  </p>
                  <pre className="text-[11px] leading-relaxed text-jarvis-text whitespace-pre-wrap break-all max-h-48 overflow-y-auto select-text">
                    {m.content}
                  </pre>
                </div>
              ),
            )}
          </div>
        ) : (
          <pre className="bg-jarvis-card rounded border border-jarvis-border/60 px-2 py-1.5 text-[11px] leading-relaxed text-jarvis-text whitespace-pre-wrap break-all max-h-48 overflow-y-auto select-text">
            {detail.prompt_text ?? "（内容已过保留期或未记录）"}
          </pre>
        )}
        {(detail.prompt_chars ?? 0) > (detail.prompt_text?.length ?? 0) && (
          <p className="text-[10px] text-jarvis-yellow mt-0.5">
            原文 {detail.prompt_chars} 字，已截断保存
          </p>
        )}
      </div>

      <div>
        <p className="text-[11px] text-jarvis-text-secondary mb-1">返回 ↓</p>
        <pre className="bg-jarvis-card rounded border border-jarvis-border/60 px-2 py-1.5 text-[11px] leading-relaxed text-jarvis-text whitespace-pre-wrap break-all max-h-56 overflow-y-auto select-text">
          {detail.response_text ?? "（无返回内容：调用失败或内容已过保留期）"}
        </pre>
        {(detail.response_chars ?? 0) > (detail.response_text?.length ?? 0) && (
          <p className="text-[10px] text-jarvis-yellow mt-0.5">
            原文 {detail.response_chars} 字，已截断保存
          </p>
        )}
      </div>
    </div>
  );
}

function LlmLogRow({ r }: { r: LlmUsageRecent }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<LlmUsageDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const expandable = r.id != null && r.has_content;

  const toggle = async () => {
    if (!expandable) return;
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (detail || r.id == null) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await api.llmUsageDetail(r.id);
      if (res.ok && res.record) setDetail(res.record);
      else setErr(res.error ?? "加载失败");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "网络错误");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-jarvis-bg rounded px-2 py-1">
      <button
        onClick={toggle}
        disabled={!expandable}
        className={`w-full flex items-center gap-2 text-xs text-left ${expandable ? "cursor-pointer" : "cursor-default"}`}
        title={r.error ?? (expandable ? "点击查看发送/返回内容" : "该记录无内容日志")}
      >
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${r.ok ? "bg-jarvis-green" : "bg-jarvis-red"}`} />
        <span className="w-14 shrink-0 font-mono text-jarvis-text-secondary">
          {new Date(r.ts * 1000).toLocaleTimeString("en-GB", { hour12: false })}
        </span>
        <span className="w-16 shrink-0 text-jarvis-text">
          {MODULE_LABELS[r.module] ?? r.module}
        </span>
        <span className="flex-1 truncate text-jarvis-text-secondary font-mono">
          {r.model ?? "—"}
        </span>
        <span className="shrink-0 font-mono text-jarvis-text">
          {fmtTokens(r.total_tokens)}
          {r.estimated ? "≈" : ""} · {fmtCost(r.cost_usd)}
        </span>
        {expandable && (
          <ChevronDown
            size={12}
            className={`shrink-0 text-jarvis-text-secondary transition-transform ${open ? "rotate-180" : ""}`}
          />
        )}
      </button>
      {open && (
        busy ? (
          <p className="flex items-center gap-1.5 text-[11px] text-jarvis-text-secondary py-1.5">
            <Loader2 size={11} className="animate-spin" />
            加载内容...
          </p>
        ) : err ? (
          <p className="text-[11px] text-jarvis-red py-1.5">{err}</p>
        ) : detail ? (
          <LlmLogDetailView detail={detail} />
        ) : null
      )}
    </div>
  );
}

const LLM_LOG_PAGE = 10;

function LlmUsageCard() {
  const [data, setData] = useState<LlmUsageResponse | null>(null);
  const [records, setRecords] = useState<LlmUsageRecent[]>([]);
  const [moduleFilter, setModuleFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (mod: string, offset: number, append: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.llmUsage(30, LLM_LOG_PAGE, mod || undefined, offset);
        if (!res.ok) {
          setError(res.error ?? "加载失败");
          return;
        }
        setData(res);
        setRecords((prev) => (append ? [...prev, ...(res.recent ?? [])] : res.recent ?? []));
      } catch (e) {
        setError(e instanceof Error ? e.message : "网络错误");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    load(moduleFilter, 0, false);
  }, [moduleFilter, load]);

  const usage = data;
  const maxModuleCost = Math.max(
    0.000001,
    ...(usage?.by_module ?? []).map((b) => b.cost_usd),
  );
  const moduleChips = (usage?.by_module ?? []).map((b) => b.module ?? "").filter(Boolean);
  const hasMore = records.length < (usage?.recent_total ?? 0);

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-1">
        <h3 className="text-sm font-semibold text-jarvis-text flex items-center gap-2">
          <Coins size={14} />
          LLM 用量与日志
        </h3>
        <button
          onClick={() => load(moduleFilter, 0, false)}
          className="flex items-center gap-1 text-xs text-jarvis-blue hover:underline"
          title="重新拉取用量统计与日志"
        >
          <RefreshCw size={12} />
          刷新
        </button>
      </div>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        每次 AI 调用自动记账并保留发送/返回内容（默认 {usage?.content_retention_days ?? 30} 天）。成本为按公开牌价的估算，非账单口径。
      </p>

      {!usage ? (
        <p className="text-xs text-jarvis-text-secondary py-2">
          {loading ? "加载中..." : error ? `加载失败：${error}` : "暂无记账数据，触发一次 AI 问答/推理后再来看。"}
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 mb-3">
            <div className="bg-jarvis-bg rounded-md p-2.5">
              <p className="text-xs text-jarvis-text-secondary mb-1">今日</p>
              <p className="text-sm font-mono text-jarvis-text">
                {fmtCost(usage.today?.cost_usd)}
                <span className="text-xs text-jarvis-text-secondary ml-1.5">
                  {fmtTokens(usage.today?.total_tokens)} tok · {usage.today?.calls ?? 0} 次
                </span>
              </p>
            </div>
            <div className="bg-jarvis-bg rounded-md p-2.5">
              <p className="text-xs text-jarvis-text-secondary mb-1">本月</p>
              <p className="text-sm font-mono text-jarvis-text">
                {fmtCost(usage.month?.cost_usd)}
                <span className="text-xs text-jarvis-text-secondary ml-1.5">
                  {fmtTokens(usage.month?.total_tokens)} tok · {usage.month?.calls ?? 0} 次
                </span>
              </p>
            </div>
          </div>

          {(usage.by_module ?? []).length > 0 && (
            <div className="mb-3">
              <p className="text-xs text-jarvis-text-secondary mb-1.5">
                按功能分布（近 {usage.days ?? 30} 天）
              </p>
              <div className="space-y-1.5">
                {(usage.by_module ?? []).slice(0, 6).map((b) => (
                  <div key={b.module} className="flex items-center gap-2 text-xs">
                    <span className="w-16 shrink-0 text-jarvis-text-secondary">
                      {MODULE_LABELS[b.module ?? ""] ?? b.module}
                    </span>
                    <div className="flex-1 h-1.5 bg-jarvis-bg rounded overflow-hidden">
                      <div
                        className="h-full bg-jarvis-blue/70"
                        style={{ width: `${Math.max(2, (b.cost_usd / maxModuleCost) * 100)}%` }}
                      />
                    </div>
                    <span className="w-24 shrink-0 text-right font-mono text-jarvis-text">
                      {fmtCost(b.cost_usd)} · {fmtTokens(b.total_tokens)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <div className="flex items-center justify-between mb-1.5">
              <p className="text-xs text-jarvis-text-secondary">调用日志（点击展开内容）</p>
            </div>
            {moduleChips.length > 1 && (
              <div className="flex flex-wrap gap-1 mb-1.5">
                {["", ...moduleChips].map((m) => (
                  <button
                    key={m || "__all"}
                    onClick={() => setModuleFilter(m)}
                    className={`px-1.5 py-0.5 text-[11px] rounded border transition-colors ${
                      moduleFilter === m
                        ? "border-jarvis-blue text-jarvis-blue bg-jarvis-blue/10"
                        : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text"
                    }`}
                  >
                    {m ? (MODULE_LABELS[m] ?? m) : "全部"}
                  </button>
                ))}
              </div>
            )}
            {records.length > 0 ? (
              <div className="space-y-1 max-h-80 overflow-y-auto">
                {records.map((r, i) => (
                  <LlmLogRow key={r.id ?? `jsonl-${r.ts}-${i}`} r={r} />
                ))}
                {hasMore && (
                  <button
                    onClick={() => load(moduleFilter, records.length, true)}
                    disabled={loading}
                    className="w-full py-1 text-[11px] text-jarvis-blue hover:underline disabled:opacity-50"
                  >
                    {loading ? "加载中..." : `加载更多（还有 ${(usage.recent_total ?? 0) - records.length} 条）`}
                  </button>
                )}
              </div>
            ) : (
              <p className="text-xs text-jarvis-text-secondary py-1">
                {loading ? "加载中..." : "该筛选下暂无调用记录。"}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function PaperTradingSafetyCard() {
  const [cfg, setCfg] = useState<TradingConfig>({});
  const [cb, setCb] = useState<CircuitBreakerStatus | null>(null);
  const [cooldown, setCooldown] = useState<CooldownResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const [tc, cbr, cd] = await Promise.all([
        api.tradingConfig(),
        api.circuitBreaker(),
        api.cbCooldown().catch(() => null),
      ]);
      setCfg(tc);
      setCb(cbr);
      setCooldown(cd);
    } catch {
      /* 静默 */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const flash = (text: string, ok = true) => {
    setMsg(text);
    setTimeout(() => setMsg(""), 4000);
    if (ok) load();
  };

  const saveLimits = async () => {
    setSaving(true);
    try {
      const res = await api.updateTradingConfig(cfg);
      flash(res.ok ? "限额已保存 ✓" : `保存失败: ${res.reason ?? ""}`, res.ok);
    } catch (e) {
      flash(e instanceof Error ? e.message : "保存失败", false);
    } finally {
      setSaving(false);
    }
  };

  const handleKillSwitch = async () => {
    if (!window.confirm("确认急停？将撤销 QD 模拟挂单并取消本地 pending 订单。")) return;
    setBusy("kill");
    try {
      const res = await api.killSwitch();
      flash(res.ok ? "急停已执行 ✓" : `急停失败: ${res.error ?? ""}`, res.ok);
    } catch (e) {
      flash(e instanceof Error ? e.message : "急停失败", false);
    } finally {
      setBusy("");
    }
  };

  const handleResetCb = async () => {
    if (!window.confirm("确认解除熔断？仅在人工复核风险后操作。")) return;
    setBusy("cb");
    try {
      const res = await api.resetCircuitBreaker();
      flash(res.ok ? "熔断已复位 ✓" : `复位失败: ${res.error ?? ""}`, res.ok);
    } catch (e) {
      flash(e instanceof Error ? e.message : "复位失败", false);
    } finally {
      setBusy("");
    }
  };

  // ── [Sprint1 T1.5] 冷静期：已阅归因 / 提前解锁（二次确认）──
  const handleCooldownAck = async () => {
    setBusy("ack");
    try {
      const res = await api.cbCooldownAck();
      flash(res.ok ? "归因摘要已确认阅读 ✓" : `操作失败: ${res.error ?? ""}`, res.ok);
    } catch (e) {
      flash(e instanceof Error ? e.message : "操作失败", false);
    } finally {
      setBusy("");
    }
  };

  const handleCooldownUnlock = async () => {
    const cd = cooldown?.cooldown;
    const mins = cd ? Math.ceil(cd.remaining_s / 60) : 0;
    if (
      !window.confirm(
        `⚠️ 提前解锁冷静期？\n\n冷静期还剩约 ${mins} 分钟。连续亏损后立即重新开仓，往往是情绪化交易的开始。\n\n确认放弃剩余冷静时间、立即恢复开仓能力？`,
      )
    )
      return;
    setBusy("unlock");
    try {
      const res = await api.cbCooldownUnlock();
      flash(res.ok ? "冷静期已提前解锁" : `解锁失败: ${res.reason ?? res.error ?? ""}`, res.ok);
    } catch (e) {
      flash(e instanceof Error ? e.message : "解锁失败", false);
    } finally {
      setBusy("");
    }
  };

  const tripped = Boolean(
    cb?.state?.tripped ?? cb?.evaluation?.already_tripped,
  );
  const cd = cooldown?.cooldown;
  const attribution = cooldown?.attribution;
  const cooldownActive = Boolean(cd?.active);

  return (
    <div className="card mb-4">
      <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
        <ShieldAlert size={14} />
        Paper 模拟盘安全
      </h3>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        写入 jarvis_config.json，影响 brief/executor/paper 跟盘护栏。
      </p>

      <div className="grid grid-cols-2 gap-3 mb-3">
        <NumberInput
          label="单笔仓位上限 (%)"
          value={cfg.max_position_pct}
          onChange={(v) => setCfg((c) => ({ ...c, max_position_pct: v }))}
          min={1}
          max={100}
        />
        <NumberInput
          label="组合风险红线 (%)"
          value={cfg.max_portfolio_risk_pct}
          onChange={(v) => setCfg((c) => ({ ...c, max_portfolio_risk_pct: v }))}
          step={0.1}
          min={0.1}
          max={10}
        />
        <NumberInput
          label="账户权益 (USDT)"
          value={cfg.account_equity_usdt}
          onChange={(v) => setCfg((c) => ({ ...c, account_equity_usdt: v }))}
          min={100}
        />
        <NumberInput
          label="4h 最大持仓数"
          value={cfg.intraday_max_open_positions}
          onChange={(v) => setCfg((c) => ({ ...c, intraday_max_open_positions: v }))}
          min={1}
          max={10}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <button
          onClick={saveLimits}
          disabled={saving}
          className="btn-primary text-xs py-1.5 px-3 flex items-center gap-1"
        >
          <Save size={12} />
          {saving ? "保存中…" : "保存限额"}
        </button>
        <button
          onClick={handleKillSwitch}
          disabled={busy === "kill"}
          className="text-xs py-1.5 px-3 rounded-md border border-jarvis-red/50 text-jarvis-red hover:bg-jarvis-red/10 flex items-center gap-1"
        >
          <OctagonX size={12} />
          {busy === "kill" ? "执行中…" : "一键急停"}
        </button>
        {tripped && (
          <button
            onClick={handleResetCb}
            disabled={busy === "cb"}
            className="text-xs py-1.5 px-3 rounded-md border border-jarvis-yellow/50 text-jarvis-yellow hover:bg-jarvis-yellow/10"
          >
            {busy === "cb" ? "复位中…" : "解除熔断"}
          </button>
        )}
      </div>

      <div className="text-xs space-y-1 bg-jarvis-bg rounded-md p-2">
        <div className="flex justify-between">
          <span className="text-jarvis-text-secondary">熔断状态</span>
          <span className={tripped ? "text-jarvis-red" : "text-jarvis-green"}>
            {tripped ? `已熔断 · ${cb?.state?.reason ?? "—"}` : "正常"}
          </span>
        </div>
        {cb?.evaluation?.drawdown_pct != null && (
          <div className="flex justify-between">
            <span className="text-jarvis-text-secondary">组合回撤</span>
            <span className="font-mono text-jarvis-text">
              {String(cb.evaluation.drawdown_pct)}%
            </span>
          </div>
        )}
        <div className="flex justify-between">
          <span className="text-jarvis-text-secondary">冷静期</span>
          <span className={cooldownActive ? "text-jarvis-yellow" : "text-jarvis-green"}>
            {cooldownActive
              ? cd?.expired
                ? "已到期 · 待确认归因"
                : `锁单中 · 剩余 ${Math.ceil((cd?.remaining_s ?? 0) / 60)} 分钟`
              : "未激活"}
          </span>
        </div>
      </div>

      {/* [Sprint1 T1.5] 冷静期面板：当日亏损归因摘要 + 已阅 / 提前解锁 */}
      {cooldownActive && (
        <div className="mt-2 text-xs bg-jarvis-yellow/5 border border-jarvis-yellow/30 rounded-md p-2 space-y-2">
          <p className="text-jarvis-yellow font-medium">
            熔断冷静期锁单中（开仓已拦截，平仓不受限）· 触发原因：{cd?.reason ?? "—"}
          </p>
          {attribution && (
            <div className="space-y-1">
              <p className="text-jarvis-text">
                当日复盘（{attribution.date}）：平仓 {attribution.closed_trades} 笔，
                {attribution.wins} 赢 / {attribution.losses} 亏，
                合计 <span className={attribution.total_pnl_usdt < 0 ? "text-jarvis-red" : "text-jarvis-green"}>
                  {attribution.total_pnl_usdt} USDT
                </span>
              </p>
              {attribution.by_reason.length > 0 && (
                <p className="text-jarvis-text-secondary">
                  按平仓原因：
                  {attribution.by_reason
                    .map((r) => `${r.reason} ×${r.count}（${r.pnl_usdt} U）`)
                    .join("、")}
                </p>
              )}
              {attribution.worst_trades.length > 0 && (
                <p className="text-jarvis-text-secondary">
                  最大亏损：
                  {attribution.worst_trades
                    .slice(0, 3)
                    .map((t) => `${t.symbol} ${t.side} ${t.pnl_usdt} U（${t.reason ?? "—"}）`)
                    .join("、")}
                </p>
              )}
              {attribution.closed_trades === 0 && (
                <p className="text-jarvis-text-secondary">当日暂无平仓记录（触发可能来自浮亏/闪崩）。</p>
              )}
            </div>
          )}
          <div className="flex items-center gap-2">
            {!cd?.acknowledged && (
              <button
                onClick={handleCooldownAck}
                disabled={busy === "ack"}
                className="text-xs py-1 px-3 rounded-md border border-jarvis-green/50 text-jarvis-green hover:bg-jarvis-green/10"
              >
                {busy === "ack" ? "确认中…" : "已阅归因摘要"}
              </button>
            )}
            {cd?.acknowledged && !cd?.expired && (
              <span className="text-jarvis-text-secondary">已阅 ✓ · 到期后自动恢复开仓</span>
            )}
            {!cd?.expired && (
              <button
                onClick={handleCooldownUnlock}
                disabled={busy === "unlock"}
                className="text-xs py-1 px-3 rounded-md border border-jarvis-red/50 text-jarvis-red hover:bg-jarvis-red/10"
              >
                {busy === "unlock" ? "解锁中…" : "提前解锁（不推荐）"}
              </button>
            )}
          </div>
        </div>
      )}

      {msg && (
        <p
          className={`text-xs mt-2 ${msg.includes("失败") ? "text-jarvis-red" : "text-jarvis-green"}`}
        >
          {msg}
        </p>
      )}
    </div>
  );
}

const CONFIG_GROUP_ORDER: ConfigGroupName[] = ["trading", "risk", "signal", "data", "notify", "system"];

const CONFIG_GROUP_LABELS: Record<ConfigGroupName, string> = {
  trading: "交易执行",
  risk: "风控红线",
  signal: "信号决策",
  data: "数据回测",
  notify: "通知",
  system: "系统",
};

/** 键名 → 中文标签（缺省回退键名本身，保证新增键零维护也能编辑） */
const CONFIG_KEY_LABELS: Record<string, string> = {
  watchlist: "币种池",
  min_conviction: "信心阈值",
  max_position_pct: "单笔仓位上限 (%)",
  account_equity_usdt: "账户权益 (USDT)",
  entry_band_below_pct: "入场带下沿 (%)",
  entry_band_above_pct: "入场带上沿 (%)",
  sizing_method: "仓位算法",
  kelly_fraction: "凯利系数",
  poscalc_capital_usdt: "合约本金 (USDT)",
  poscalc_leverage: "目标杠杆 (x)",
  poscalc_risk_pct: "单笔风险 (%)",
  poscalc_margin_pct: "保证金占比 (%)",
  intraday_enabled: "4h 引擎开关",
  intraday_max_open_positions: "4h 最大持仓数",
  intraday_cooldown_bars: "4h 冷却根数",
  max_portfolio_risk_pct: "组合风险红线 (%)",
  max_effective_pct: "有效敞口上限 (%)",
  stop_loss_drop_pct: "硬止损幅度 (%)",
  take_profit_pct: "参考止盈 (%)",
  time_stop_days: "时间止损 (天)",
  intraday_risk_pct_per_trade: "4h 单笔风险 (%)",
  intraday_stop_atr_mult: "4h 止损 ATR 倍数",
  intraday_take_atr_mult: "4h 止盈 ATR 倍数",
  intraday_time_stop_bars: "4h 时间止损 (根)",
  intraday_max_consecutive_losses: "4h 连亏熔断 (笔)",
  cb_drawdown_halt_pct: "组合回撤熔断 (%)",
  cb_position_loss_halt_pct: "单仓亏损熔断 (%)",
  cb_flash_crash_24h_pct: "24h 闪崩熔断 (%)",
  cb_depeg_deviation_pct: "稳定币脱锚熔断 (%)",
  plan_min_rr: "计划最低盈亏比 (RR)",
  intraday_min_prob: "4h 开仓概率门槛",
  debate_enabled: "多空辩论层",
  debate_mode: "辩论模式",
  debate_timeout_sec: "辩论超时 (秒)",
  backtest_cost_bps: "回测滑点成本 (bps)",
  notify_timeout_s: "通知超时 (秒)",
  daemon_interval_hours: "守护进程周期 (小时)",
  dashboard_host: "后端监听地址",
  dashboard_port: "后端监听端口",
};

function ConfigCenterField({
  k,
  meta,
  value,
  onChange,
}: {
  k: string;
  meta: ConfigFieldMeta;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = CONFIG_KEY_LABELS[k] ?? k;
  const hint = meta.min != null && meta.max != null ? `范围 ${meta.min}~${meta.max}` : undefined;
  if (meta.type === "bool") {
    return <ToggleInput label={label} value={Boolean(value)} onChange={(v) => onChange(v)} hint={hint} />;
  }
  if (meta.enum) {
    return (
      <SelectInput
        label={label}
        value={String(value ?? "")}
        options={meta.enum.map((o) => ({ value: o, label: o }))}
        onChange={(v) => onChange(v)}
        hint={hint}
      />
    );
  }
  if (meta.type === "int" || meta.type === "float") {
    return (
      <NumberInput
        label={label}
        value={value == null ? undefined : Number(value)}
        onChange={(v) => onChange(v)}
        step={meta.type === "int" ? 1 : 0.1}
        min={meta.min}
        max={meta.max}
        hint={hint}
      />
    );
  }
  // list / str：文本框（list 用逗号分隔）
  const text = Array.isArray(value) ? (value as unknown[]).join(",") : String(value ?? "");
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-jarvis-border/50 last:border-0">
      <div>
        <p className="text-sm text-jarvis-text">{label}</p>
        {meta.type === "list" && <p className="text-xs text-jarvis-text-secondary">逗号分隔</p>}
      </div>
      <input
        type="text"
        value={text}
        onChange={(e) =>
          onChange(meta.type === "list"
            ? e.target.value.split(",").map((s) => s.trim()).filter(Boolean)
            : e.target.value)
        }
        className="w-56 px-2 py-1 text-sm font-mono text-right bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
      />
    </div>
  );
}

function ConfigCenterCard() {
  const [data, setData] = useState<ConfigCenterResponse | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [openGroup, setOpenGroup] = useState<ConfigGroupName | null>("trading");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await api.configCenter();
      setData(res);
      const flat: Record<string, unknown> = {};
      for (const g of CONFIG_GROUP_ORDER) {
        Object.assign(flat, res.groups[g] ?? {});
      }
      setValues(flat);
      setDirty({});
    } catch {
      /* 后端未就绪时静默，卡片显示加载态 */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const groupKeys = useMemo(() => {
    const byGroup: Partial<Record<ConfigGroupName, string[]>> = {};
    if (data) {
      for (const [k, m] of Object.entries(data.fields)) {
        const g = m.group as ConfigGroupName;
        (byGroup[g] ??= []).push(k);
      }
    }
    return byGroup;
  }, [data]);

  const handleChange = (k: string, v: unknown) => {
    setValues((prev) => ({ ...prev, [k]: v }));
    setDirty((prev) => ({ ...prev, [k]: v }));
  };

  const handleSave = async () => {
    if (!Object.keys(dirty).length) {
      setMsg("没有修改项");
      setTimeout(() => setMsg(""), 2500);
      return;
    }
    setSaving(true);
    try {
      const res = await api.updateConfigCenter(dirty);
      if (res.ok) {
        setMsg(`已保存 ${Object.keys(dirty).length} 项，即时生效 ✓（v${res.version ?? "?"}）`);
        await load();
      } else {
        setMsg(`保存失败: ${res.reason ?? "未知错误"}`);
      }
    } catch (e) {
      setMsg(`保存失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setSaving(false);
      setTimeout(() => setMsg(""), 4000);
    }
  };

  const dirtyCount = Object.keys(dirty).length;

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-1">
        <h3 className="text-sm font-semibold text-jarvis-text flex items-center gap-2">
          <SlidersHorizontal size={14} />
          统一配置中心
        </h3>
        <div className="flex items-center gap-2">
          {msg && (
            <span className={`text-xs ${msg.includes("失败") ? "text-jarvis-red" : "text-jarvis-green"}`}>
              {msg}
            </span>
          )}
          <button
            onClick={load}
            className="text-xs py-1 px-2 rounded-md border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text"
          >
            重新加载
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !dirtyCount}
            className="btn-primary text-xs py-1 px-3 flex items-center gap-1 disabled:opacity-50"
          >
            <Save size={12} />
            {saving ? "保存中…" : dirtyCount ? `保存 ${dirtyCount} 项修改` : "保存"}
          </button>
        </div>
      </div>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        写入 ~/.vibe-trading/config.yaml（分组 trading/risk/signal/data/notify/system），保存即热生效无需重启；
        非法值后端自动夹到安全区间。
        {data?.meta?.updated_at && ` 最近更新：${data.meta.updated_at}（v${data.meta.version}）`}
      </p>

      {!data ? (
        <p className="text-xs text-jarvis-text-secondary py-4 text-center">配置加载中…</p>
      ) : (
        CONFIG_GROUP_ORDER.map((g) => {
          const keys = groupKeys[g] ?? [];
          if (!keys.length) return null;
          const open = openGroup === g;
          return (
            <div key={g} className="border border-jarvis-border/60 rounded-lg mb-2 overflow-hidden">
              <button
                onClick={() => setOpenGroup(open ? null : g)}
                className="w-full flex items-center justify-between px-3 py-2 bg-jarvis-bg/60 hover:bg-jarvis-bg text-left"
              >
                <span className="text-sm font-medium text-jarvis-text">
                  {CONFIG_GROUP_LABELS[g]}
                  <span className="ml-2 text-xs text-jarvis-text-secondary">
                    {data.group_comments[g] ?? ""}
                  </span>
                </span>
                <ChevronDown
                  size={14}
                  className={`text-jarvis-text-secondary transition-transform ${open ? "rotate-180" : ""}`}
                />
              </button>
              {open && (
                <div className="px-3 pb-1">
                  {keys.map((k) => (
                    <ConfigCenterField
                      key={k}
                      k={k}
                      meta={data.fields[k]}
                      value={values[k]}
                      onChange={(v) => handleChange(k, v)}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}

export default function SettingsPage() {
  const { supported } = useSymbol();
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

  // 已保存的交易对可能不在当前币种列表（如自定义币种被删除），补进选项避免下拉显示错位
  const scalperSymbolOptions = (() => {
    const opts = supported.map((s) => ({ value: s.value, label: s.label }));
    if (config.symbol && !supported.some((s) => s.value === config.symbol)) {
      opts.push({ value: config.symbol, label: config.symbol });
    }
    return opts;
  })();

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

      <ConfigCenterCard />

      <PaperTradingSafetyCard />

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
            options={scalperSymbolOptions}
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

          {/* QD 网关 + Agent Token 配置 */}
          <QdGatewayCard />

          {/* 大模型 (LLM) 配置 */}
          <LlmConfigCard />

          {/* LLM 用量与成本记账 */}
          <LlmUsageCard />

          {/* 主题切换 */}
          <ThemeCard />
        </div>
      </div>
    </div>
  );
}
