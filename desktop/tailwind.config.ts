import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        jarvis: {
          bg: "#0d1117",
          card: "#161b22",
          border: "#30363d",
          text: "#e6edf3",
          "text-secondary": "#8b949e",
          // 盈亏语义色（涨绿跌红），铁律：不随主题变
          green: "#3fb950",
          red: "#f85149",
          // 强调色走 CSS 变量（globals.css 按 data-accent 切换），/xx 透明度语法保持可用
          blue: "rgb(var(--jarvis-accent) / <alpha-value>)",
          "accent-fg": "rgb(var(--jarvis-accent-fg) / <alpha-value>)",
          yellow: "#d29922",
          purple: "#bc8cff",
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "PingFang SC",
          "Helvetica Neue",
          "sans-serif",
        ],
        mono: ["SF Mono", "Menlo", "Monaco", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
