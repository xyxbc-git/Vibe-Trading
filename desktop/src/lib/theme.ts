/** 强调色主题管理：深色底不变，仅切换全局强调色（CSS 变量 --jarvis-accent）。
 *
 * - 色值定义唯一来源在 globals.css 的 :root[data-accent=...] 区块，这里只管清单与切换；
 *   preview 色块仅供设置页色板展示。
 * - 涨绿跌红盈亏语义色不属于主题范畴（交易软件铁律，不随主题变）。
 * - index.html 内联脚本会在首帧前读取 localStorage 注入 data-accent，防止刷新闪回默认蓝。
 */

export const ACCENT_STORAGE_KEY = "jarvis-accent";

export interface AccentTheme {
  id: string;
  name: string;
  /** 色板卡片预览色（与 globals.css 中 --jarvis-accent 同值） */
  preview: string;
}

export const ACCENT_THEMES: AccentTheme[] = [
  { id: "tech-blue", name: "科技蓝", preview: "#58a6ff" },
  { id: "emerald", name: "翡翠绿", preview: "#2dd4bf" },
  { id: "violet", name: "紫罗兰", preview: "#a78bfa" },
  { id: "amber", name: "琥珀橙", preview: "#f2994a" },
  { id: "cyber-pink", name: "赛博粉", preview: "#f472b6" },
  { id: "silver", name: "银灰", preview: "#9ca3af" },
];

export const DEFAULT_ACCENT = "tech-blue";

export function getAccent(): string {
  try {
    const saved = localStorage.getItem(ACCENT_STORAGE_KEY);
    if (saved && ACCENT_THEMES.some((t) => t.id === saved)) return saved;
  } catch {
    /* localStorage 不可用时回默认 */
  }
  return DEFAULT_ACCENT;
}

export function applyAccent(id: string): void {
  const valid = ACCENT_THEMES.some((t) => t.id === id) ? id : DEFAULT_ACCENT;
  // 默认主题移除属性，让 :root 基础变量生效
  if (valid === DEFAULT_ACCENT) {
    delete document.documentElement.dataset.accent;
  } else {
    document.documentElement.dataset.accent = valid;
  }
  try {
    localStorage.setItem(ACCENT_STORAGE_KEY, valid);
  } catch {
    /* 持久化失败不影响本次会话生效 */
  }
}
