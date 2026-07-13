import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { Tag, X } from "lucide-react";
import { api } from "@/api/client";

/**
 * T1.4 平仓复盘打标弹窗。
 * 场景：① 手动平仓成功后立即弹出；② 交易记录页对历史平仓补标/改标。
 * 标签集来自配置中心 trading.journal_tags（可扩展）；读取失败回退内置默认，
 * 保证打标流程不被配置接口故障阻断。
 */

const FALLBACK_TAGS = ["按计划止盈", "按计划止损", "恐慌割肉", "追高被套", "贪婪不止盈", "其它"];

/** 标签 → 高亮色（盈利行为绿、亏损行为红、中性黄/灰） */
function tagCls(tag: string, active: boolean): string {
  const good = tag.includes("止盈") && !tag.includes("不止盈");
  const plan = tag.includes("按计划");
  const bad = ["恐慌", "追高", "贪婪"].some((w) => tag.includes(w));
  if (active) {
    if (plan || good) return "bg-jarvis-green/20 text-jarvis-green border-jarvis-green/60";
    if (bad) return "bg-jarvis-red/20 text-jarvis-red border-jarvis-red/60";
    return "bg-jarvis-blue/20 text-jarvis-blue border-jarvis-blue/60";
  }
  return "bg-jarvis-bg text-jarvis-text-secondary border-jarvis-border hover:text-jarvis-text hover:border-jarvis-blue/50";
}

export default function BehaviorTagDialog({
  positionId,
  title,
  currentTag,
  onClose,
  onSaved,
}: {
  positionId: number;
  /** 弹窗标题上下文，如 "BTCUSDT 多单 #12 已平仓 -2.1%" */
  title: string;
  /** 已有标签（补标/改标场景回显） */
  currentTag?: string | null;
  onClose: () => void;
  onSaved?: (tag: string | null) => void;
}) {
  const [tags, setTags] = useState<string[]>(FALLBACK_TAGS);
  const [selected, setSelected] = useState<string | null>(currentTag ?? null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 标签集走配置中心（trading.journal_tags）；失败回退内置默认
  useEffect(() => {
    let cancelled = false;
    api
      .configCenter()
      .then((cfg) => {
        const raw = cfg.groups?.trading?.journal_tags;
        if (!cancelled && Array.isArray(raw) && raw.length > 0) {
          setTags(raw.map(String));
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const save = async (tag: string | null) => {
    setSaving(true);
    setError(null);
    try {
      const res = await api.setBehaviorTag(positionId, tag);
      if (res.ok) {
        onSaved?.(tag);
        onClose();
      } else {
        setError(res.reason ?? "保存失败");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "后端服务不可达");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="w-[400px] rounded-xl border border-jarvis-border bg-jarvis-card p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-1">
          <p className="flex items-center gap-2 text-sm font-medium text-jarvis-text">
            <Tag size={15} className="text-jarvis-yellow" />
            平仓复盘打标
          </p>
          <button
            onClick={onClose}
            className="text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
            aria-label="关闭"
          >
            <X size={16} />
          </button>
        </div>
        <p className="text-xs text-jarvis-text-secondary mb-4">{title}</p>

        <p className="text-xs text-jarvis-text-secondary mb-2">
          这笔交易的真实原因是什么？诚实打标才能在成长页看到自己的行为模式
        </p>
        <div className="flex flex-wrap gap-2 mb-4">
          {tags.map((t) => (
            <button
              key={t}
              onClick={() => setSelected(selected === t ? null : t)}
              disabled={saving}
              className={clsx(
                "px-3 py-1.5 text-xs rounded-full border transition-colors disabled:opacity-50",
                tagCls(t, selected === t),
              )}
            >
              {t}
            </button>
          ))}
        </div>

        {error && (
          <p className="text-xs text-jarvis-red mb-3" role="alert">
            {error}
          </p>
        )}

        {/* 首次打标必须选标签；补标（已有标签）时取消选中 = 清除标签（后端 tag=null） */}
        <div className="flex gap-2">
          <button
            onClick={() => save(selected)}
            disabled={saving || (selected == null && currentTag == null)}
            className={clsx(
              "flex-1 py-2 text-sm rounded-lg font-medium text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
              selected == null && currentTag != null
                ? "bg-jarvis-red hover:bg-jarvis-red/80"
                : "bg-jarvis-blue hover:bg-jarvis-blue/80",
            )}
          >
            {saving
              ? "保存中..."
              : selected == null && currentTag != null
                ? "清除标签"
                : "保存标签"}
          </button>
          <button
            onClick={onClose}
            disabled={saving}
            className="px-4 py-2 text-sm rounded-lg border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text transition-colors disabled:opacity-50"
          >
            稍后再标
          </button>
        </div>
      </div>
    </div>
  );
}
