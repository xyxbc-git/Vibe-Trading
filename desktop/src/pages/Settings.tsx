import { useState, useEffect, useCallback } from "react";
import { Settings as SettingsIcon, Save, RefreshCw, Wifi, Key, Palette, Server, Plug, Download, ShieldAlert, OctagonX } from "lucide-react";
import { api, type QdConfig, type QdConfigTest, type TradingConfig, type CircuitBreakerStatus, type LlmConfig, type LlmTestResult } from "@/api/client";
import { useApi } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";

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

function PaperTradingSafetyCard() {
  const [cfg, setCfg] = useState<TradingConfig>({});
  const [cb, setCb] = useState<CircuitBreakerStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const [tc, cbr] = await Promise.all([
        api.tradingConfig(),
        api.circuitBreaker(),
      ]);
      setCfg(tc);
      setCb(cbr);
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

  const tripped = Boolean(
    cb?.state?.tripped ?? cb?.evaluation?.already_tripped,
  );

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
      </div>

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
