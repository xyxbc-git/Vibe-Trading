import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { ShieldCheck, Plus, Trash2, Save, RotateCcw } from "lucide-react";
import { mentorApi, DEFAULT_RULES, type MentorRule } from "@/api/mentor";

/**
 * V2「我的军规」管理页：默认 8 条可改参/停用，自定义文本军规可增删。
 * 军规同时用于：① 裁决卡「军规核对」区段逐条打分 ② 下单前自我约束提醒。
 */
export default function RulesTab() {
  const [rules, setRules] = useState<MentorRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [mock, setMock] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [newText, setNewText] = useState("");

  useEffect(() => {
    let alive = true;
    mentorApi.rules().then((res) => {
      if (!alive) return;
      setRules(res.rules);
      setMock(Boolean(res.mock));
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  function patch(id: MentorRule["id"], p: Partial<MentorRule>) {
    setRules((prev) => prev.map((r) => (r.id === id ? { ...r, ...p } : r)));
    setDirty(true);
    setSavedNote(null);
  }

  function addCustom() {
    const text = newText.trim();
    if (!text) return;
    setRules((prev) => [
      ...prev,
      {
        id: `c${Date.now()}`,
        kind: `custom_${Date.now()}`,
        text,
        param: null,
        enabled: true,
        builtin: false,
      },
    ]);
    setNewText("");
    setDirty(true);
    setSavedNote(null);
  }

  function removeCustom(id: MentorRule["id"]) {
    setRules((prev) => prev.filter((r) => r.id !== id));
    setDirty(true);
    setSavedNote(null);
  }

  function resetDefaults() {
    setRules(DEFAULT_RULES.map((r) => ({ ...r })));
    setDirty(true);
    setSavedNote(null);
  }

  async function save() {
    setSaving(true);
    const res = await mentorApi.saveRules(rules);
    setSaving(false);
    setDirty(false);
    setMock(Boolean(res.mock));
    setSavedNote(res.mock ? "已保存到本地（后端军规接口待联调，届时自动同步）" : "已保存");
  }

  return (
    <div className="card">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-base font-semibold text-jarvis-text">
          <ShieldCheck size={18} className="text-jarvis-blue" />
          我的军规 · 你给自己定的下单规矩
        </h2>
        {mock && (
          <span className="rounded-full border border-jarvis-yellow/50 px-2 py-0.5 text-[10px] text-jarvis-yellow">
            本地数据 · 待后端联调
          </span>
        )}
      </div>
      <p className="mb-4 text-xs text-jarvis-text-secondary">
        每次提交计划，导师都会拿这份军规逐条核对（裁决卡「军规核对」区）。数值可改、不想要的可停用——但规矩是你自己定的，破了账也记你头上。
      </p>

      {loading ? (
        <p className="py-10 text-center text-sm text-jarvis-text-secondary">加载中…</p>
      ) : (
        <div className="space-y-2">
          {rules.map((r, i) => (
            <div
              key={String(r.id)}
              className={clsx(
                "flex items-center gap-3 rounded-lg border px-3 py-2.5",
                r.enabled ? "border-jarvis-border bg-jarvis-bg/60" : "border-jarvis-border/40 bg-jarvis-bg/30 opacity-60",
              )}
            >
              <span className="w-5 shrink-0 text-center font-mono text-xs text-jarvis-text-secondary">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-jarvis-text">
                  {r.text}
                  {!r.builtin && <span className="ml-2 rounded border border-jarvis-border px-1 text-[10px] text-jarvis-text-secondary">自定义</span>}
                </p>
              </div>
              {/* 数值参数（内置数值型军规可编辑） */}
              {r.param != null && (
                <div className="flex shrink-0 items-center gap-1">
                  <input
                    type="number"
                    step="any"
                    value={r.param}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      if (Number.isFinite(v)) patch(r.id, { param: v });
                    }}
                    className="w-16 rounded-md border border-jarvis-border bg-jarvis-bg px-2 py-1 text-right font-mono text-xs text-jarvis-text outline-none focus:border-jarvis-blue"
                  />
                  {r.param_label && <span className="text-[10px] text-jarvis-text-secondary">{r.param_label}</span>}
                </div>
              )}
              {/* 启用开关 */}
              <button
                type="button"
                onClick={() => patch(r.id, { enabled: !r.enabled })}
                className={clsx(
                  "relative h-5 w-9 shrink-0 rounded-full transition-colors",
                  r.enabled ? "bg-jarvis-green" : "bg-jarvis-border",
                )}
                aria-label={r.enabled ? "停用" : "启用"}
              >
                <span
                  className={clsx(
                    "absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all",
                    r.enabled ? "left-[18px]" : "left-0.5",
                  )}
                />
              </button>
              {/* 自定义军规可删除；默认军规只可停用 */}
              {!r.builtin ? (
                <button
                  type="button"
                  onClick={() => removeCustom(r.id)}
                  className="shrink-0 text-jarvis-text-secondary transition-colors hover:text-jarvis-red"
                  aria-label="删除"
                >
                  <Trash2 size={14} />
                </button>
              ) : (
                <span className="w-[14px] shrink-0" />
              )}
            </div>
          ))}
        </div>
      )}

      {/* 新增自定义军规（文本型：裁决卡以提醒形式展示） */}
      <div className="mt-3 flex items-center gap-2">
        <input
          value={newText}
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addCustom()}
          placeholder="写一条自己的军规，如：跌破日线 MA20 不做多"
          className="flex-1 rounded-lg border border-jarvis-border bg-jarvis-bg px-3 py-2 text-sm text-jarvis-text outline-none focus:border-jarvis-blue"
        />
        <button
          type="button"
          onClick={addCustom}
          disabled={!newText.trim()}
          className={clsx(
            "flex items-center gap-1 rounded-lg border px-3 py-2 text-sm transition-colors",
            newText.trim()
              ? "border-jarvis-blue/50 text-jarvis-blue hover:bg-jarvis-blue/10"
              : "cursor-not-allowed border-jarvis-border text-jarvis-text-secondary",
          )}
        >
          <Plus size={14} /> 新增
        </button>
      </div>
      <p className="mt-1 text-[11px] text-jarvis-text-secondary">
        自定义军规为文本提醒（裁决时原文展示）；数值判定类军规由系统内置引擎核对。
      </p>

      {/* 保存 / 重置 */}
      <div className="mt-4 flex items-center gap-2">
        <button
          type="button"
          onClick={save}
          disabled={!dirty || saving}
          className={clsx(
            "flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium transition-colors",
            dirty && !saving
              ? "bg-jarvis-blue text-jarvis-accent-fg hover:bg-jarvis-blue/80"
              : "cursor-not-allowed bg-jarvis-border/60 text-jarvis-text-secondary",
          )}
        >
          <Save size={14} /> {saving ? "保存中…" : "保存军规"}
        </button>
        <button
          type="button"
          onClick={resetDefaults}
          className="flex items-center gap-1.5 rounded-lg border border-jarvis-border px-3 py-2 text-sm text-jarvis-text-secondary transition-colors hover:text-jarvis-text"
        >
          <RotateCcw size={13} /> 恢复默认 8 条
        </button>
        {savedNote && <span className="text-xs text-jarvis-green">{savedNote}</span>}
        {dirty && !savedNote && <span className="text-xs text-jarvis-yellow">有未保存的修改</span>}
      </div>
    </div>
  );
}
