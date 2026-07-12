import { useEffect, useMemo, useState } from "react";
import { clsx } from "clsx";
import {
  Calculator,
  AlertTriangle,
  ShieldCheck,
  ShieldAlert,
  Skull,
  Save,
  Loader2,
  Undo2,
  Bell,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import {
  api,
  formatPrice,
  type ConsensusScope,
  type PositionCalcConfig,
  type SlSafety,
} from "@/api/client";
import { SideBadge } from "@/components/cards/SignalBoard";
import { buildPlanOrder } from "@/lib/planOrder";
import { isValidEmail } from "@/components/cards/OrderNotifyDialog";

// 内联邮件提醒的邮箱记忆键：上次配置成功的邮箱，下次保存直接复用
const LAST_EMAIL_KEY = "jarvis.orderNotify.lastEmail";

function loadLastEmail(): string {
  try {
    return localStorage.getItem(LAST_EMAIL_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveLastEmail(email: string): void {
  try {
    localStorage.setItem(LAST_EMAIL_KEY, email);
  } catch {
    /* storage 不可用（隐私模式等）——记忆功能静默降级 */
  }
}

const SAFETY_META: Record<
  SlSafety,
  { label: string; cls: string; icon: React.ReactNode }
> = {
  ok: {
    label: "止损在爆仓内侧，边距充足",
    cls: "bg-jarvis-green/15 text-jarvis-green",
    icon: <ShieldCheck size={11} />,
  },
  warning: {
    label: "止损距爆仓过近",
    cls: "bg-jarvis-yellow/15 text-jarvis-yellow",
    icon: <ShieldAlert size={11} />,
  },
  danger: {
    label: "先爆仓后止损",
    cls: "bg-jarvis-red/20 text-jarvis-red",
    icon: <Skull size={11} />,
  },
};

// 各字段合法区间（与后端 jarvis_config BOUNDS 对齐）
const RANGES = {
  capital: [1, 1e9],
  leverage: [1, 125],
  margin: [1, 100],
} as const;

const clampNum = (v: number, lo: number, hi: number) =>
  Math.max(lo, Math.min(hi, v));

const parseNum = (s: string): number | null => {
  const v = parseFloat(s);
  return Number.isFinite(v) ? v : null;
};

/** 输入防抖：停止输入 500ms 后才触发重算，避免逐键刷接口 */
function useDebounced<T>(value: T, ms = 500): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** 自由键盘输入的数值框（隐藏原生上下箭头，失焦时夹紧到合法区间） */
function NumField({
  label,
  value,
  suffix,
  width = "w-16",
  title,
  onChange,
  onBlur,
}: {
  label: string;
  value: string;
  suffix: string;
  width?: string;
  title?: string;
  onChange: (s: string) => void;
  onBlur?: () => void;
}) {
  return (
    <label className="flex flex-col gap-0.5 min-w-0" title={title}>
      <span className="text-[9px] text-jarvis-text-secondary">{label}</span>
      <span className="flex items-center gap-1">
        <input
          type="number"
          inputMode="decimal"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onBlur}
          className={clsx(
            width,
            "px-1.5 py-1 bg-jarvis-bg border border-jarvis-border rounded",
            "text-[11px] text-jarvis-text font-mono outline-none focus:border-jarvis-blue",
            "[appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none",
          )}
        />
        <span className="text-[9px] text-jarvis-text-secondary">{suffix}</span>
      </span>
    </label>
  );
}

interface PositionAdvisorProps {
  symbol: string;
  /** 信号口径，与页面共识联动："auto" = 多周期综合 */
  tf: ConsensusScope;
  /** 紧凑模式（详情页侧栏用）：隐藏依据/备注等次要信息 */
  compact?: boolean;
}

/** 仓位与风控计算器卡：信号计划 × (本金/入场价/杠杆/保证金%) → 可执行下单建议 */
export default function PositionAdvisor({ symbol, tf, compact }: PositionAdvisorProps) {
  // 已保存配置（GET /position-calc/config），也是字段非法时的回退基准
  const [saved, setSaved] = useState<PositionCalcConfig | null>(null);
  // 表单原始字符串（允许自由输入，提交口径见 effCfg）
  const [fs, setFs] = useState({ capital: "", leverage: "", margin: "", entry: "" });
  // 入场价是否被用户手动接管（false = 跟随信号计划价）
  const [entryTouched, setEntryTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  // ── 内联邮件提醒（保存生成自创订单时一并写入 order-notify 配置） ──
  const [mailOn, setMailOn] = useState(false);
  const [mailEmail, setMailEmail] = useState(loadLastEmail);
  const [mailTp, setMailTp] = useState(true);
  const [mailSl, setMailSl] = useState(true);
  // 发件 SMTP 是否已配置（null = 未知/未拉取）；仅作引导提示，不阻断保存
  const [smtpReady, setSmtpReady] = useState<boolean | null>(null);
  useEffect(() => {
    if (!mailOn || smtpReady !== null) return;
    let cancelled = false;
    api
      .alertConfig()
      .then((c) => {
        if (!cancelled) setSmtpReady(Boolean(c?.smtp?.has_password));
      })
      .catch(() => {
        // 配置接口不可达 → 保持未知，不提示不阻断
      });
    return () => {
      cancelled = true;
    };
  }, [mailOn, smtpReady]);

  // 首次拉配置作为编辑初值
  useEffect(() => {
    let cancelled = false;
    const fill = (c: PositionCalcConfig) => {
      setSaved(c);
      setFs((f) => ({
        ...f,
        capital: String(c.poscalc_capital_usdt),
        leverage: String(c.poscalc_leverage),
        margin: String(c.poscalc_margin_pct ?? 100),
      }));
    };
    api
      .positionCalcConfig()
      .then((c) => {
        if (!cancelled && c) fill(c);
      })
      .catch(() => {
        // 配置接口不可用时用内置缺省（与后端 DEFAULTS 一致）
        if (!cancelled)
          fill({
            poscalc_capital_usdt: 130,
            poscalc_leverage: 100,
            poscalc_risk_pct: 1,
            poscalc_margin_pct: 100,
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 提交口径：非法/半输入字段回退到已保存值；单笔风险固定走配置（默认 1%，UI 不再暴露）
  const effCfg: PositionCalcConfig | null = useMemo(() => {
    if (!saved) return null;
    const cap = parseNum(fs.capital);
    const lev = parseNum(fs.leverage);
    const mar = parseNum(fs.margin);
    return {
      poscalc_capital_usdt:
        cap != null && cap > 0
          ? clampNum(cap, RANGES.capital[0], RANGES.capital[1])
          : saved.poscalc_capital_usdt,
      poscalc_leverage:
        lev != null
          ? clampNum(lev, RANGES.leverage[0], RANGES.leverage[1])
          : saved.poscalc_leverage,
      poscalc_risk_pct: saved.poscalc_risk_pct ?? 1,
      poscalc_margin_pct:
        mar != null
          ? clampNum(mar, RANGES.margin[0], RANGES.margin[1])
          : (saved.poscalc_margin_pct ?? 100),
    };
  }, [fs.capital, fs.leverage, fs.margin, saved]);

  // 手动入场价：仅在用户接管且值合法时随请求下发
  const entryOverride = useMemo(() => {
    if (!entryTouched) return null;
    const v = parseNum(fs.entry);
    return v != null && v > 0 ? v : null;
  }, [entryTouched, fs.entry]);

  const debCfg = useDebounced(effCfg);
  const debEntry = useDebounced(entryOverride);

  const { data, loading, error } = usePolling(
    () =>
      debCfg
        ? api.positionCalc(symbol, tf, debCfg, debEntry)
        : Promise.resolve(null),
    90_000,
    [symbol, tf, debCfg, debEntry],
  );

  // 换币种/周期：入场价回到跟随信号
  useEffect(() => {
    setEntryTouched(false);
    setFs((f) => ({ ...f, entry: "" }));
  }, [symbol, tf]);

  // 未接管时，入场价输入框实时跟随信号计划价
  useEffect(() => {
    if (entryTouched) return;
    const e = data?.ok ? data.advice?.entry : null;
    if (e != null)
      setFs((f) => (f.entry === String(e) ? f : { ...f, entry: String(e) }));
  }, [data, entryTouched]);

  // 失焦：夹紧并回写规范化字符串；入场价清空/非法则恢复跟随信号
  const blurField = (key: "capital" | "leverage" | "margin") => {
    setFs((f) => {
      const v = parseNum(f[key]);
      if (v == null) {
        if (!saved) return f;
        const fallback = {
          capital: saved.poscalc_capital_usdt,
          leverage: saved.poscalc_leverage,
          margin: saved.poscalc_margin_pct ?? 100,
        }[key];
        return { ...f, [key]: String(fallback) };
      }
      return { ...f, [key]: String(clampNum(v, RANGES[key][0], RANGES[key][1])) };
    });
  };
  const blurEntry = () => {
    const v = parseNum(fs.entry);
    if (v == null || v <= 0) setEntryTouched(false);
  };

  /** 订单生成成功后串联邮件配置；只返回追加到提示尾部的文案，失败不回滚订单 */
  const configureOrderMail = async (orderId: number): Promise<string> => {
    if (!mailOn) return "";
    const email = mailEmail.trim();
    if (!isValidEmail(email)) {
      return " · 邮箱格式不正确，邮件提醒未配置（可去交易中心挂单行点铃铛补配）";
    }
    if (!mailTp && !mailSl) {
      return " · 未勾选通知类型，邮件提醒未配置（可去挂单行铃铛补配）";
    }
    try {
      const nr = await api.orderNotifySet(`order-${orderId}`, {
        email,
        notify_take_profit: mailTp,
        notify_stop_loss: mailSl,
      });
      if (nr.ok) {
        saveLastEmail(email);
        return smtpReady === false
          ? " · 邮件提醒已配置（发件 SMTP 未设置，请到「价位提醒」页配好后才能实际发信）"
          : " · 邮件提醒已配置";
      }
      return ` · 邮件配置失败：${nr.reason ?? "未知原因"}（订单已生成，可去挂单行铃铛重配）`;
    } catch {
      return " · 邮件配置失败：后端不可达（订单已生成，可去挂单行铃铛重配）";
    }
  };

  const handleSave = async () => {
    if (!effCfg) return;
    setSaving(true);
    setSavedMsg(null);
    try {
      const res = await api.updatePositionCalcConfig(effCfg);
      if (res.ok) {
        if (res.config) setSaved(res.config);
        else setSaved(effCfg);
        // 计划保存成功 → 按当前建议联动生成「自创」订单（无可执行计划则跳过）
        const order = buildPlanOrder(symbol, tf, resp?.ok ? resp.advice : null);
        if (order) {
          try {
            const or = await api.placeOrder(order);
            if (or.ok && or.order_id != null) {
              const mailMsg = await configureOrderMail(or.order_id);
              setSavedMsg(`已保存 · 自创订单 #${or.order_id} 已生成${mailMsg}`);
            } else {
              setSavedMsg(`已保存，订单生成失败：${or.reason ?? "未知原因"}`);
            }
          } catch (e) {
            setSavedMsg(
              `已保存，订单生成失败：${e instanceof Error ? e.message : "接口不可达"}`,
            );
          }
        } else {
          setSavedMsg(
            mailOn
              ? "已保存（当前无交易计划，未生成订单，邮件配置不生效）"
              : "已保存（当前无交易计划，未生成订单）",
          );
        }
      } else {
        setSavedMsg(`保存失败：${res.reason ?? "未知原因"}`);
      }
    } catch (e) {
      setSavedMsg(`保存失败：${e instanceof Error ? e.message : "接口不可达"}`);
    } finally {
      setSaving(false);
      setTimeout(() => setSavedMsg(null), 8000);
    }
  };

  // 防旧数据回写：慢响应的旧币种建议不得展示到新币种
  const stale = data != null && data.symbol != null && data.symbol !== symbol;
  const resp = stale ? null : data;
  const advice = resp?.ok ? resp.advice : null;
  const hasAdvice = Boolean(advice?.ok);
  const failed = Boolean(error) || (resp != null && !resp.ok);

  // 当前建议能否生成自创订单（邮件区块置灰判定，与保存时的跳过口径一致）
  const planOrderAvailable = useMemo(
    () => buildPlanOrder(symbol, tf, resp?.ok ? resp.advice : null) != null,
    [symbol, tf, resp],
  );
  const mailEmailInvalid =
    mailOn && mailEmail.trim() !== "" && !isValidEmail(mailEmail);

  const safety = advice?.sl?.safety ?? "ok";
  const safetyMeta = SAFETY_META[safety];
  const isShort = advice?.side === "short";
  const entryManual = Boolean(advice?.entry_overridden);

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Calculator size={14} />
          仓位与风控建议 · {symbol}
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 text-jarvis-blue font-mono">
            {tf === "auto" ? "综合" : tf}
          </span>
          {advice?.source_tf && tf === "auto" && (
            <span className="text-[9px] text-jarvis-text-secondary">
              计划依据 {advice.source_tf}
            </span>
          )}
        </p>
        {hasAdvice && <SideBadge side={advice?.side ?? null} />}
      </div>

      {saved && (
        <div className="mb-3 pb-3 border-b border-jarvis-border/60">
          <div className="flex items-end gap-2 flex-wrap">
            <NumField
              label="本金"
              value={fs.capital}
              suffix="U"
              onChange={(s) => setFs((f) => ({ ...f, capital: s }))}
              onBlur={() => blurField("capital")}
            />
            <NumField
              label={entryTouched ? "入场价（手动）" : "入场价（跟随信号）"}
              value={fs.entry}
              suffix="U"
              width="w-24"
              title="默认跟随信号计划价；手动修改后止损/止盈/爆仓价随之平移"
              onChange={(s) => {
                setEntryTouched(true);
                setFs((f) => ({ ...f, entry: s }));
              }}
              onBlur={blurEntry}
            />
            {entryTouched && (
              <button
                onClick={() => {
                  setEntryTouched(false);
                  setFs((f) => ({ ...f, entry: "" }));
                }}
                title="放弃手动价，恢复跟随信号计划价"
                className="flex items-center gap-1 px-1.5 py-1.5 rounded border border-jarvis-border text-[10px] text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue transition-colors"
              >
                <Undo2 size={11} />
                跟随信号
              </button>
            )}
            <NumField
              label="杠杆"
              value={fs.leverage}
              suffix="x"
              title="1~125，直接键盘输入"
              onChange={(s) => setFs((f) => ({ ...f, leverage: s }))}
              onBlur={() => blurField("leverage")}
            />
            <NumField
              label="保证金"
              value={fs.margin}
              suffix="%"
              title="保证金 = 本金 × 该百分比；名义仓位 = 保证金 × 杠杆"
              onChange={(s) => setFs((f) => ({ ...f, margin: s }))}
              onBlur={() => blurField("margin")}
            />
            <button
              onClick={handleSave}
              disabled={saving}
              title="保存为默认配置（jarvis_config 持久化），并按当前计划自动生成一笔「自创」订单"
              className="flex items-center gap-1 px-2 py-1.5 rounded border border-jarvis-border text-[10px] text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue transition-colors disabled:opacity-50"
            >
              {saving ? (
                <Loader2 size={11} className="animate-spin" />
              ) : (
                <Save size={11} />
              )}
              保存
            </button>
            {savedMsg && (
              <span
                className={clsx(
                  "text-[10px]",
                  savedMsg.includes("失败") ? "text-jarvis-red" : "text-jarvis-green",
                )}
              >
                {savedMsg}
              </span>
            )}
          </div>

          {/* ── 内联邮件提醒：保存生成自创订单时一并配置（无计划时置灰） ── */}
          <div className="mt-2">
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={() => setMailOn((v) => !v)}
                disabled={!planOrderAvailable}
                title={
                  planOrderAvailable
                    ? "开启后，保存生成的自创订单自动配置止盈/止损邮件提醒，无需再去挂单行点铃铛"
                    : "当前无交易计划，保存不会生成订单，邮件配置无处挂靠"
                }
                className={clsx(
                  "flex items-center gap-1 px-2 py-1 rounded border text-[10px] transition-colors",
                  !planOrderAvailable
                    ? "border-jarvis-border text-jarvis-text-secondary/50 cursor-not-allowed"
                    : mailOn
                      ? "border-jarvis-yellow/50 text-jarvis-yellow bg-jarvis-yellow/10"
                      : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-yellow hover:border-jarvis-yellow/50",
                )}
              >
                <Bell size={11} />
                邮件提醒{mailOn ? "·开" : "·关"}
              </button>
              {!planOrderAvailable && (
                <span className="text-[10px] text-jarvis-text-secondary/70">
                  无计划不生成订单，邮件配置不生效
                </span>
              )}
              {mailOn && planOrderAvailable && smtpReady === false && (
                <span className="text-[10px] text-jarvis-yellow">
                  发件 SMTP 未配置——到「价位提醒」页设置后才能实际发信（提醒登记不受影响）
                </span>
              )}
            </div>
            {mailOn && planOrderAvailable && (
              <div className="flex items-center gap-3 flex-wrap mt-1.5">
                <input
                  type="email"
                  value={mailEmail}
                  onChange={(e) => setMailEmail(e.target.value)}
                  placeholder="you@example.com"
                  title="接收止盈/止损通知的邮箱（记住上次配置成功的地址）"
                  className={clsx(
                    "w-52 px-2 py-1 bg-jarvis-bg border rounded text-[11px] text-jarvis-text font-mono outline-none transition-colors",
                    mailEmailInvalid
                      ? "border-jarvis-red"
                      : "border-jarvis-border focus:border-jarvis-blue",
                  )}
                />
                <label className="flex items-center gap-1 text-[10px] text-jarvis-text cursor-pointer">
                  <input
                    type="checkbox"
                    checked={mailTp}
                    onChange={(e) => setMailTp(e.target.checked)}
                    className="w-3.5 h-3.5 accent-jarvis-blue cursor-pointer"
                  />
                  <span className="text-jarvis-green">止盈通知</span>
                </label>
                <label className="flex items-center gap-1 text-[10px] text-jarvis-text cursor-pointer">
                  <input
                    type="checkbox"
                    checked={mailSl}
                    onChange={(e) => setMailSl(e.target.checked)}
                    className="w-3.5 h-3.5 accent-jarvis-blue cursor-pointer"
                  />
                  <span className="text-jarvis-red">止损通知</span>
                </label>
                {mailEmailInvalid && (
                  <span className="text-[10px] text-jarvis-red">邮箱格式不正确</span>
                )}
                {mailOn && !mailTp && !mailSl && (
                  <span className="text-[10px] text-jarvis-yellow">
                    至少勾选一种通知类型
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {failed ? (
        <div className="py-6 text-center">
          <p className="text-sm text-jarvis-text-secondary">建议计算失败</p>
          <p className="text-xs text-jarvis-text-secondary/70 mt-1">
            {resp?.error ?? error ?? "等待后端 /api/position-calc 就绪后自动恢复"}
          </p>
        </div>
      ) : !resp || (loading && !data) || stale || !saved ? (
        <div className="space-y-2 animate-pulse py-2">
          <div className="h-4 rounded bg-jarvis-border/30 w-2/3" />
          <div className="h-4 rounded bg-jarvis-border/30" />
          <div className="h-4 rounded bg-jarvis-border/30 w-1/2" />
        </div>
      ) : !hasAdvice ? (
        <p className="text-sm text-jarvis-text-secondary py-6 text-center">
          {advice?.error ?? "当前共识不构成交易计划（中性/分歧），暂无仓位建议"}
        </p>
      ) : (
        <div className="space-y-3">
          {/* ── 建议仓位 ── */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] font-mono">
            <div className="bg-jarvis-bg rounded-lg p-2">
              <p className="text-jarvis-text-secondary text-[9px]">
                名义仓位（保证金 × 杠杆）
              </p>
              <p className="text-jarvis-text text-sm">
                {advice!.position!.notional_usdt.toLocaleString()} U
              </p>
              <p className="text-jarvis-text-secondary text-[9px] mt-0.5">
                保证金 {advice!.position!.margin_usdt} U ·{" "}
                {advice!.position!.capital_used_pct}% 本金
              </p>
            </div>
            <div className="bg-jarvis-bg rounded-lg p-2">
              <p className="text-jarvis-text-secondary text-[9px]">数量</p>
              <p className="text-jarvis-text text-sm">
                {advice!.position!.qty_coin} 币
              </p>
              <p className="text-jarvis-text-secondary text-[9px] mt-0.5">
                {advice!.position!.contracts != null
                  ? `≈ ${advice!.position!.contracts} 张（1张=${advice!.position!.contract_size}币）`
                  : "该币种无张数面值表"}
              </p>
            </div>
            <div className="bg-jarvis-bg rounded-lg p-2">
              <p className="text-jarvis-text-secondary text-[9px]">
                止损触发亏损
              </p>
              <p className="text-jarvis-red text-sm">
                -{advice!.risk_usdt} U
              </p>
              <p className="text-jarvis-text-secondary text-[9px] mt-0.5">
                占本金 {advice!.risk_pct}%（本金 {advice!.capital_usdt} U）
              </p>
            </div>
            <div className="bg-jarvis-bg rounded-lg p-2">
              <p className="text-jarvis-text-secondary text-[9px]">
                {entryManual ? "入场价（手动）" : "入场区间"}
              </p>
              {entryManual ? (
                <p className="text-jarvis-blue text-sm">
                  {formatPrice(advice!.entry)}
                </p>
              ) : (
                <p className="text-jarvis-blue text-sm">
                  {formatPrice(advice!.entry_zone?.[0])} ~{" "}
                  {formatPrice(advice!.entry_zone?.[1])}
                </p>
              )}
              <p className="text-jarvis-text-secondary text-[9px] mt-0.5">
                {entryManual
                  ? "止损/止盈已随手动价平移"
                  : `中价 ${formatPrice(advice!.entry)}`}
              </p>
            </div>
          </div>

          {/* ── 止损 & 爆仓：安全边距徽章 ── */}
          <div
            className={clsx(
              "rounded-lg p-2.5 border",
              safety === "danger"
                ? "border-jarvis-red/60 bg-jarvis-red/10"
                : safety === "warning"
                  ? "border-jarvis-yellow/50 bg-jarvis-yellow/5"
                  : "border-jarvis-border bg-jarvis-bg",
            )}
          >
            <div className="flex items-center gap-2 flex-wrap text-[11px] font-mono">
              <span
                className={clsx(
                  "inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium",
                  safetyMeta.cls,
                )}
              >
                {safetyMeta.icon}
                {safetyMeta.label}
              </span>
              <span>
                <span className="text-jarvis-text-secondary">止损 </span>
                <span className="text-jarvis-red">
                  {formatPrice(advice!.sl!.price)}
                </span>
                <span className="text-jarvis-text-secondary">
                  （{isShort ? "涨" : "跌"} {advice!.sl!.dist_pct}%）
                </span>
              </span>
              <span>
                <span className="text-jarvis-text-secondary">爆仓 </span>
                <span className={safety === "ok" ? "text-jarvis-text" : "text-jarvis-red"}>
                  {formatPrice(advice!.liquidation!.price)}
                </span>
                <span className="text-jarvis-text-secondary">
                  （距入场 {advice!.liquidation!.dist_pct}%，爆仓亏{" "}
                  {advice!.liquidation!.loss_usdt} U）
                </span>
              </span>
              <span className="text-jarvis-text-secondary">
                最大安全杠杆{" "}
                <span
                  className={clsx(
                    (advice!.leverage ?? 0) > (advice!.max_safe_leverage ?? 125)
                      ? "text-jarvis-red"
                      : "text-jarvis-green",
                  )}
                >
                  {advice!.max_safe_leverage}x
                </span>
              </span>
            </div>
          </div>

          {/* ── 分档止盈 1:1.5 / 1:2 / 1:3 ── */}
          <div>
            <p className="text-[10px] text-jarvis-text-secondary mb-1">
              分档止盈（按盈亏比）
              {advice!.plan_tp_ref != null && (
                <span className="ml-1.5 opacity-70">
                  信号参考目标 {formatPrice(advice!.plan_tp_ref)}
                </span>
              )}
            </p>
            <div className="grid grid-cols-3 gap-2 text-[11px] font-mono">
              {(advice!.take_profits ?? []).map((tp) => (
                <div key={tp.rr} className="bg-jarvis-bg rounded-lg p-2">
                  <p className="text-jarvis-text-secondary text-[9px]">
                    1:{tp.rr}
                  </p>
                  <p className="text-jarvis-green text-sm">
                    {formatPrice(tp.price)}
                  </p>
                  <p className="text-jarvis-text-secondary text-[9px] mt-0.5">
                    +{tp.profit_usdt} U
                  </p>
                </div>
              ))}
            </div>
          </div>

          {/* ── 风险警告 ── */}
          {(advice!.warnings?.length ?? 0) > 0 && (
            <div className="space-y-1">
              {advice!.warnings!.map((w, i) => (
                <p
                  key={i}
                  className={clsx(
                    "flex items-start gap-1.5 text-[11px] leading-relaxed rounded px-2 py-1",
                    w.startsWith("🚨")
                      ? "bg-jarvis-red/15 text-jarvis-red font-medium"
                      : "bg-jarvis-yellow/10 text-jarvis-yellow",
                  )}
                >
                  <AlertTriangle size={12} className="flex-shrink-0 mt-0.5" />
                  <span>{w.replace(/^([🚨⚠️]\s*)+/u, "")}</span>
                </p>
              ))}
            </div>
          )}

          {!compact && advice!.note && (
            <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">
              {advice!.note}
            </p>
          )}
          {!compact && (advice!.basis?.length ?? 0) > 0 && (
            <p className="text-[9px] text-jarvis-text-secondary/70">
              信号依据：{advice!.basis!.join(" · ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
