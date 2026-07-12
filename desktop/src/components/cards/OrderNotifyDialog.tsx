import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { Bell, Mail, Send, Trash2, X } from "lucide-react";
import { api } from "@/api/client";

interface OrderNotifyDialogProps {
  /** "order-<limit_order_id>"（挂单）或 "pos-<position_id>"（持仓） */
  orderId: string;
  /** 弹窗标题里的订单描述，如 "BTCUSDT 多单 #12" */
  title: string;
  onClose: () => void;
  /** 保存/删除成功后回调（父组件刷新配置索引） */
  onSaved?: () => void;
}

/** 邮箱格式宽校验（与后端 jarvis_order_notify._valid_email 同口径）；供内联邮件配置复用 */
export function isValidEmail(v: string): boolean {
  const e = v.trim();
  return e.length > 3 && e.includes("@") && Boolean(e.split("@")[1]?.includes("."));
}

/**
 * 单笔订单的邮件提醒配置弹窗：收件邮箱 + 止盈/止损通知开关。
 * 打开时拉取现有配置回填；保存走 PUT /order-notify/{orderId}。
 */
export default function OrderNotifyDialog({
  orderId,
  title,
  onClose,
  onSaved,
}: OrderNotifyDialogProps) {
  const [loading, setLoading] = useState(true);
  const [exists, setExists] = useState(false);
  const [email, setEmail] = useState("");
  const [notifyTp, setNotifyTp] = useState(true);
  const [notifySl, setNotifySl] = useState(true);
  const [busy, setBusy] = useState<"save" | "delete" | "test" | null>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .orderNotifyGet(orderId)
      .then((res) => {
        if (!alive) return;
        const c = res.config;
        if (c) {
          setExists(true);
          setEmail(c.email);
          setNotifyTp(c.notify_take_profit);
          setNotifySl(c.notify_stop_loss);
        }
      })
      .catch(() => {
        /* 无配置/后端不可达都按空表单处理 */
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [orderId]);

  const handleSave = async () => {
    if (!isValidEmail(email)) {
      setResult({ ok: false, msg: "请输入有效的邮箱地址" });
      return;
    }
    if (!notifyTp && !notifySl) {
      setResult({ ok: false, msg: "至少勾选一种通知类型（止盈/止损）" });
      return;
    }
    setBusy("save");
    setResult(null);
    try {
      const res = await api.orderNotifySet(orderId, {
        email: email.trim(),
        notify_take_profit: notifyTp,
        notify_stop_loss: notifySl,
      });
      if (res.ok) {
        setExists(true);
        setResult({ ok: true, msg: "已保存，触发止盈/止损时将发送邮件" });
        onSaved?.();
      } else {
        setResult({ ok: false, msg: res.reason ?? "保存失败" });
      }
    } catch (e) {
      setResult({ ok: false, msg: e instanceof Error ? e.message : "后端服务不可达" });
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async () => {
    setBusy("delete");
    setResult(null);
    try {
      await api.orderNotifyDelete(orderId);
      setExists(false);
      setEmail("");
      setNotifyTp(true);
      setNotifySl(true);
      setResult({ ok: true, msg: "已移除该单的邮件提醒" });
      onSaved?.();
    } catch (e) {
      setResult({ ok: false, msg: e instanceof Error ? e.message : "删除失败" });
    } finally {
      setBusy(null);
    }
  };

  const handleTest = async () => {
    setBusy("test");
    setResult(null);
    try {
      const res = await api.orderNotifyTest(orderId);
      setResult(
        res.ok
          ? { ok: true, msg: `测试邮件已发送 → ${email.trim()}` }
          : { ok: false, msg: `发送失败：${res.reason ?? "未知原因"}` },
      );
    } catch (e) {
      setResult({ ok: false, msg: e instanceof Error ? e.message : "后端服务不可达" });
    } finally {
      setBusy(null);
    }
  };

  const checkboxCls =
    "w-4 h-4 rounded border-jarvis-border bg-jarvis-bg accent-jarvis-blue cursor-pointer";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="card w-[420px] max-w-[92vw] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-2">
            <Bell size={16} className="text-jarvis-yellow" />
            <p className="text-jarvis-text font-semibold text-sm">邮件提醒配置</p>
          </div>
          <button
            onClick={onClose}
            className="text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
            aria-label="关闭"
          >
            <X size={16} />
          </button>
        </div>
        <p className="text-xs text-jarvis-text-secondary mb-4">{title}</p>

        {loading ? (
          <p className="text-sm text-jarvis-text-secondary py-6 text-center">加载中...</p>
        ) : (
          <div className="space-y-4">
            <div>
              <label className="text-xs text-jarvis-text-secondary flex items-center gap-1">
                <Mail size={12} />
                接收通知的邮箱
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full mt-1 px-3 py-2 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text font-mono focus:border-jarvis-blue outline-none transition-colors"
              />
              <p className="text-[11px] text-jarvis-text-secondary mt-1">
                发件账号使用「价位提醒」页配置的 SMTP 邮箱。
              </p>
            </div>

            <div>
              <p className="text-xs text-jarvis-text-secondary mb-2">通知类型（可全选）</p>
              <div className="flex gap-5">
                <label className="flex items-center gap-2 text-sm text-jarvis-text cursor-pointer">
                  <input
                    type="checkbox"
                    checked={notifyTp}
                    onChange={(e) => setNotifyTp(e.target.checked)}
                    className={checkboxCls}
                  />
                  <span className="text-jarvis-green">止盈通知</span>
                </label>
                <label className="flex items-center gap-2 text-sm text-jarvis-text cursor-pointer">
                  <input
                    type="checkbox"
                    checked={notifySl}
                    onChange={(e) => setNotifySl(e.target.checked)}
                    className={checkboxCls}
                  />
                  <span className="text-jarvis-red">止损通知</span>
                </label>
              </div>
            </div>

            {result && (
              <div
                role="status"
                className={clsx(
                  "p-2.5 rounded-lg text-xs",
                  result.ok
                    ? "bg-jarvis-green/10 text-jarvis-green"
                    : "bg-jarvis-red/10 text-jarvis-red",
                )}
              >
                {result.msg}
              </div>
            )}

            <div className="flex items-center gap-2 pt-1">
              <button
                onClick={handleSave}
                disabled={!!busy}
                className="flex-1 py-2 rounded-lg bg-jarvis-blue text-white text-sm font-medium hover:bg-jarvis-blue/80 transition-colors disabled:opacity-50"
              >
                {busy === "save" ? "保存中..." : exists ? "更新配置" : "开启提醒"}
              </button>
              {exists && (
                <>
                  <button
                    onClick={handleTest}
                    disabled={!!busy}
                    title="发送测试邮件"
                    className="p-2 rounded-lg border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-blue hover:border-jarvis-blue transition-colors disabled:opacity-50"
                  >
                    <Send size={15} />
                  </button>
                  <button
                    onClick={handleDelete}
                    disabled={!!busy}
                    title="移除该单提醒"
                    className="p-2 rounded-lg border border-jarvis-red/40 text-jarvis-red hover:bg-jarvis-red/10 transition-colors disabled:opacity-50"
                  >
                    <Trash2 size={15} />
                  </button>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
