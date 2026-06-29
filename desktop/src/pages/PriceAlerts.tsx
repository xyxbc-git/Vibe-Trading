import { useState, useEffect, useCallback } from "react";
import {
  Bell,
  Mail,
  Plus,
  Trash2,
  Save,
  Send,
  RefreshCw,
  X,
  TrendingUp,
  TrendingDown,
  Power,
} from "lucide-react";
import {
  api,
  type AlertConfig,
  type AlertPlan,
  type AlertDirection,
} from "@/api/client";
import { useApi } from "@/hooks/useApi";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"];

function fmtTs(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

/* ─────────────────── SMTP 配置卡片 ─────────────────── */
function SmtpCard({
  config,
  onSaved,
}: {
  config: AlertConfig | null;
  onSaved: () => void;
}) {
  const [host, setHost] = useState("");
  const [port, setPort] = useState(465);
  const [useSsl, setUseSsl] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fromName, setFromName] = useState("贾维斯价位提醒");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!config) return;
    setHost(config.smtp.host);
    setPort(config.smtp.port);
    setUseSsl(config.smtp.use_ssl);
    setUsername(config.smtp.username);
    setFromName(config.smtp.from_name || "贾维斯价位提醒");
  }, [config]);

  const save = async () => {
    setSaving(true);
    setMsg("");
    try {
      const smtp: Record<string, unknown> = {
        host: host.trim(),
        port,
        use_ssl: useSsl,
        username: username.trim(),
        from_name: fromName.trim(),
      };
      if (password.trim()) smtp.password = password.trim();
      const res = await api.updateAlertConfig({ smtp });
      if (res.ok) {
        setMsg("保存成功 ✓");
        setPassword("");
        onSaved();
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

  const test = async () => {
    setTesting(true);
    setMsg("");
    try {
      const res = await api.testAlertEmail();
      setMsg(
        res.ok
          ? `测试邮件已发送 ✓ → ${(res.to ?? []).join(", ") || "(全局收件人)"}`
          : `发送失败: ${res.reason ?? "未知错误"}`,
      );
    } catch (e) {
      setMsg(`发送失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setTesting(false);
      setTimeout(() => setMsg(""), 6000);
    }
  };

  const labelCls = "text-sm text-jarvis-text mb-1.5";
  const inputCls =
    "w-full px-2 py-1.5 text-sm bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue";

  return (
    <div className="card">
      <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
        <Mail size={14} />
        发件邮箱 (SMTP)
      </h3>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        用于发送提醒的邮箱账号。QQ/163 等需使用「授权码」而非登录密码。授权码仅保存在本地，绝不上传。
      </p>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <p className={labelCls}>SMTP 服务器</p>
          <input
            className={inputCls}
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="smtp.qq.com"
          />
        </div>
        <div>
          <p className={labelCls}>端口</p>
          <input
            type="number"
            className={inputCls}
            value={port}
            onChange={(e) => setPort(Number(e.target.value))}
            placeholder="465"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 mt-3">
        <div>
          <p className={labelCls}>发件邮箱账号</p>
          <input
            className={inputCls}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="you@qq.com"
            autoComplete="off"
          />
        </div>
        <div>
          <p className={labelCls}>
            授权码 / 密码
            {config?.smtp.has_password && (
              <span className="text-jarvis-text-secondary ml-1">
                （当前 {config.smtp.password_masked}）
              </span>
            )}
          </p>
          <input
            type="password"
            className={inputCls}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={config?.smtp.has_password ? "留空表示不修改" : "粘贴授权码"}
            autoComplete="new-password"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 mt-3 items-end">
        <div>
          <p className={labelCls}>发件人显示名</p>
          <input
            className={inputCls}
            value={fromName}
            onChange={(e) => setFromName(e.target.value)}
            placeholder="贾维斯价位提醒"
          />
        </div>
        <label className="flex items-center gap-2 py-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={useSsl}
            onChange={(e) => setUseSsl(e.target.checked)}
            className="accent-jarvis-blue w-4 h-4"
          />
          <span className="text-sm text-jarvis-text">使用 SSL（465 端口常用）</span>
        </label>
      </div>

      <div className="flex items-center gap-3 mt-4">
        <button onClick={save} disabled={saving} className="btn-primary flex items-center gap-2">
          <Save size={14} />
          {saving ? "保存中..." : "保存 SMTP"}
        </button>
        <button
          onClick={test}
          disabled={testing}
          className="btn-primary flex items-center gap-2 !bg-jarvis-card border border-jarvis-border"
        >
          <Send size={14} />
          {testing ? "发送中..." : "发测试邮件"}
        </button>
        {msg && (
          <span
            className={`text-sm ${
              msg.includes("成功") || msg.includes("已发送") ? "text-jarvis-green" : "text-jarvis-red"
            }`}
          >
            {msg}
          </span>
        )}
      </div>
    </div>
  );
}

/* ─────────────────── 全局收件人卡片 ─────────────────── */
function RecipientsCard({
  config,
  onSaved,
}: {
  config: AlertConfig | null;
  onSaved: () => void;
}) {
  const [emails, setEmails] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [interval, setIntervalS] = useState(60);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!config) return;
    setEmails(config.recipients);
    setIntervalS(config.poll_interval_s);
  }, [config]);

  const addEmail = () => {
    const e = input.trim();
    if (e && e.includes("@") && !emails.includes(e)) {
      setEmails([...emails, e]);
      setInput("");
    }
  };

  const save = async () => {
    setMsg("");
    try {
      const res = await api.updateAlertConfig({
        recipients: emails,
        poll_interval_s: interval,
      });
      setMsg(res.ok ? "保存成功 ✓" : `保存失败: ${res.reason ?? "未知错误"}`);
      if (res.ok) onSaved();
    } catch (e) {
      setMsg(`保存失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setTimeout(() => setMsg(""), 4000);
    }
  };

  return (
    <div className="card">
      <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
        <Bell size={14} />
        默认收件邮箱
      </h3>
      <p className="text-xs text-jarvis-text-secondary mb-3">
        计划未单独指定收件人时，提醒将发送到这些邮箱。可配置多个。
      </p>

      <div className="flex flex-wrap gap-2 mb-3">
        {emails.length === 0 && (
          <span className="text-xs text-jarvis-text-secondary">暂无收件邮箱</span>
        )}
        {emails.map((e) => (
          <span
            key={e}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-jarvis-bg border border-jarvis-border rounded-full text-jarvis-text"
          >
            {e}
            <button
              onClick={() => setEmails(emails.filter((x) => x !== e))}
              className="text-jarvis-text-secondary hover:text-jarvis-red"
            >
              <X size={12} />
            </button>
          </span>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <input
          className="flex-1 px-2 py-1.5 text-sm bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addEmail()}
          placeholder="输入邮箱后回车添加"
        />
        <button
          onClick={addEmail}
          className="btn-primary flex items-center gap-1 !bg-jarvis-card border border-jarvis-border"
        >
          <Plus size={14} />
          添加
        </button>
      </div>

      <div className="flex items-center justify-between mt-4 pt-3 border-t border-jarvis-border/50">
        <div>
          <p className="text-sm text-jarvis-text">轮询间隔（秒）</p>
          <p className="text-xs text-jarvis-text-secondary">后台多久检查一次价格</p>
        </div>
        <input
          type="number"
          min={10}
          value={interval}
          onChange={(e) => setIntervalS(Number(e.target.value))}
          className="w-24 px-2 py-1 text-sm font-mono text-right bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue"
        />
      </div>

      <div className="flex items-center gap-3 mt-4">
        <button onClick={save} className="btn-primary flex items-center gap-2">
          <Save size={14} />
          保存收件人
        </button>
        {msg && (
          <span className={`text-sm ${msg.includes("成功") ? "text-jarvis-green" : "text-jarvis-red"}`}>
            {msg}
          </span>
        )}
      </div>
    </div>
  );
}

/* ─────────────────── 新增计划表单 ─────────────────── */
const emptyPlan = {
  name: "",
  symbol: "BTCUSDT",
  target_price: 0,
  direction: "above" as AlertDirection,
  repeat: false,
};

function NewPlanForm({ onCreated }: { onCreated: () => void }) {
  const [form, setForm] = useState({ ...emptyPlan });
  const [livePrice, setLivePrice] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const loadPrice = useCallback(async (symbol: string) => {
    setLivePrice(null);
    try {
      const res = await api.alertPrice(symbol);
      setLivePrice(res.price);
    } catch {
      setLivePrice(null);
    }
  }, []);

  useEffect(() => {
    loadPrice(form.symbol);
  }, [form.symbol, loadPrice]);

  const submit = async () => {
    if (!form.name.trim()) {
      setMsg("请填写提醒名称");
      return;
    }
    if (!form.target_price || form.target_price <= 0) {
      setMsg("请填写有效的目标价位");
      return;
    }
    setSaving(true);
    setMsg("");
    try {
      const res = await api.createAlertPlan(form);
      if (res.ok) {
        setForm({ ...emptyPlan });
        onCreated();
      } else {
        setMsg(`创建失败: ${res.reason ?? "未知错误"}`);
      }
    } catch (e) {
      setMsg(`创建失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setSaving(false);
      setTimeout(() => setMsg(""), 4000);
    }
  };

  const inputCls =
    "w-full px-2 py-1.5 text-sm bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue";

  return (
    <div className="card">
      <h3 className="text-sm font-semibold text-jarvis-text mb-3 flex items-center gap-2">
        <Plus size={14} />
        新增提醒计划
      </h3>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">提醒名称</p>
          <input
            className={inputCls}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="如：BTC 涨破 7 万"
          />
        </div>
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">
            币种
            {livePrice != null && (
              <span className="text-jarvis-green ml-1.5">现价 {livePrice}</span>
            )}
          </p>
          <select
            className={inputCls}
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value })}
          >
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 mt-3">
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">目标价位</p>
          <input
            type="number"
            className={inputCls}
            value={form.target_price || ""}
            onChange={(e) => setForm({ ...form, target_price: Number(e.target.value) })}
            placeholder="如 70000"
          />
        </div>
        <div>
          <p className="text-sm text-jarvis-text mb-1.5">触发方向</p>
          <div className="flex gap-2">
            <button
              onClick={() => setForm({ ...form, direction: "above" })}
              className={`flex-1 flex items-center justify-center gap-1 py-1.5 rounded-md text-sm border ${
                form.direction === "above"
                  ? "bg-jarvis-green/15 border-jarvis-green text-jarvis-green"
                  : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary"
              }`}
            >
              <TrendingUp size={14} /> 涨破
            </button>
            <button
              onClick={() => setForm({ ...form, direction: "below" })}
              className={`flex-1 flex items-center justify-center gap-1 py-1.5 rounded-md text-sm border ${
                form.direction === "below"
                  ? "bg-jarvis-red/15 border-jarvis-red text-jarvis-red"
                  : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary"
              }`}
            >
              <TrendingDown size={14} /> 跌破
            </button>
          </div>
        </div>
      </div>

      <label className="flex items-center gap-2 mt-3 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={form.repeat}
          onChange={(e) => setForm({ ...form, repeat: e.target.checked })}
          className="accent-jarvis-blue w-4 h-4"
        />
        <span className="text-sm text-jarvis-text">重复提醒（默认触发一次后自动停用）</span>
      </label>

      <div className="flex items-center gap-3 mt-4">
        <button onClick={submit} disabled={saving} className="btn-primary flex items-center gap-2">
          <Plus size={14} />
          {saving ? "创建中..." : "创建计划"}
        </button>
        {msg && <span className="text-sm text-jarvis-red">{msg}</span>}
      </div>
    </div>
  );
}

/* ─────────────────── 计划列表项 ─────────────────── */
function PlanRow({
  plan,
  onChanged,
}: {
  plan: AlertPlan;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  const toggle = async () => {
    setBusy(true);
    try {
      await api.updateAlertPlan(plan.id, { enabled: !plan.enabled });
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await api.deleteAlertPlan(plan.id);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const dirAbove = plan.direction === "above";

  return (
    <div className="flex items-center justify-between py-3 px-3 border-b border-jarvis-border/50 last:border-0">
      <div className="flex items-center gap-3 min-w-0">
        <span
          className={`flex items-center justify-center w-9 h-9 rounded-lg shrink-0 ${
            dirAbove ? "bg-jarvis-green/15 text-jarvis-green" : "bg-jarvis-red/15 text-jarvis-red"
          }`}
        >
          {dirAbove ? <TrendingUp size={18} /> : <TrendingDown size={18} />}
        </span>
        <div className="min-w-0">
          <p className="text-sm text-jarvis-text font-medium truncate">
            {plan.name}
            {!plan.enabled && (
              <span className="ml-2 text-xs text-jarvis-text-secondary">（已停用）</span>
            )}
            {plan.repeat && (
              <span className="ml-2 text-xs text-jarvis-blue">重复</span>
            )}
          </p>
          <p className="text-xs text-jarvis-text-secondary truncate">
            {plan.symbol} · {dirAbove ? "涨破" : "跌破"}{" "}
            <span className="font-mono text-jarvis-text">{plan.target_price}</span>
            {plan.last_price != null && <> · 最近价 {plan.last_price}</>}
            {plan.triggered_count > 0 && (
              <> · 已触发 {plan.triggered_count} 次 @ {fmtTs(plan.last_triggered_at)}</>
            )}
          </p>
          {plan.recipients.length > 0 && (
            <p className="text-xs text-jarvis-text-secondary truncate">
              收件人：{plan.recipients.join(", ")}
            </p>
          )}
          {plan.last_send_result && plan.last_send_result !== "ok" && (
            <p className="text-xs text-jarvis-red truncate">
              上次发送失败：{plan.last_send_result}
            </p>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={toggle}
          disabled={busy}
          title={plan.enabled ? "停用" : "启用"}
          className={`p-2 rounded-md border transition-colors ${
            plan.enabled
              ? "border-jarvis-green/40 text-jarvis-green hover:bg-jarvis-green/10"
              : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text"
          }`}
        >
          <Power size={15} />
        </button>
        <button
          onClick={remove}
          disabled={busy}
          title="删除"
          className="p-2 rounded-md border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-red hover:border-jarvis-red/40 transition-colors"
        >
          <Trash2 size={15} />
        </button>
      </div>
    </div>
  );
}

/* ─────────────────── 页面 ─────────────────── */
export default function PriceAlertsPage() {
  const { data: config, refetch: refetchConfig } = useApi<AlertConfig>(
    () => api.alertConfig(),
  );
  const { data: plans, refetch: refetchPlans } = useApi<AlertPlan[]>(
    () => api.alertPlans(),
  );
  const [checking, setChecking] = useState(false);
  const [checkMsg, setCheckMsg] = useState("");

  const runCheck = async () => {
    setChecking(true);
    setCheckMsg("");
    try {
      const res = await api.alertCheck(false);
      setCheckMsg(`已检查 ${res.checked} 个 · 触发 ${res.triggered} 个`);
      refetchPlans();
      refetchConfig();
    } catch (e) {
      setCheckMsg(`检查失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setChecking(false);
      setTimeout(() => setCheckMsg(""), 5000);
    }
  };

  const mon = config?.monitor;

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Bell size={22} />
        价位提醒
      </h1>

      <div className="flex items-center gap-3 mb-4">
        <button onClick={runCheck} disabled={checking} className="btn-primary flex items-center gap-2">
          <RefreshCw size={14} className={checking ? "animate-spin" : ""} />
          {checking ? "检查中..." : "立即检查"}
        </button>
        <div className="flex items-center gap-1.5 text-sm">
          <span
            className={`w-2 h-2 rounded-full ${
              mon?.running ? "bg-jarvis-green" : "bg-jarvis-text-secondary"
            }`}
          />
          <span className="text-jarvis-text-secondary">
            后台监控{mon?.running ? "运行中" : "未运行"}
            {mon?.last_run && ` · 上次 ${mon.last_run}`}
          </span>
        </div>
        {checkMsg && <span className="text-sm text-jarvis-green">{checkMsg}</span>}
        {mon?.last_error && (
          <span className="text-sm text-jarvis-red">监控异常：{mon.last_error}</span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <SmtpCard config={config} onSaved={refetchConfig} />
        <RecipientsCard config={config} onSaved={refetchConfig} />
      </div>

      <div className="grid grid-cols-2 gap-4 items-start">
        <NewPlanForm onCreated={refetchPlans} />

        <div className="card">
          <h3 className="text-sm font-semibold text-jarvis-text mb-1 flex items-center gap-2">
            <Bell size={14} />
            提醒计划（{plans?.length ?? 0}）
          </h3>
          {(!plans || plans.length === 0) && (
            <p className="text-sm text-jarvis-text-secondary py-6 text-center">
              还没有提醒计划，左侧创建一个吧。
            </p>
          )}
          <div>
            {plans?.map((p) => (
              <PlanRow key={p.id} plan={p} onChanged={refetchPlans} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
