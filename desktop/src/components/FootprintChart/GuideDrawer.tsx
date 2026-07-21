import { X } from "lucide-react";
import { COLORS, IMBALANCE_RATIO } from "./renderer";

function Cell({
  bid,
  ask,
  highlight,
  poc,
}: {
  bid: string;
  ask: string;
  highlight?: "buy" | "sell" | "imb";
  poc?: boolean;
}) {
  const bg =
    highlight === "buy"
      ? "rgba(37,99,235,0.4)"
      : highlight === "sell"
        ? "rgba(239,68,68,0.4)"
        : "rgba(255,255,255,0.03)";
  const border = poc
    ? COLORS.poc
    : highlight === "imb"
      ? COLORS.imbalance
      : COLORS.border;
  return (
    <div
      className="flex items-center justify-center gap-2 rounded-sm border px-2 py-1 font-mono text-[10px]"
      style={{ background: highlight === "imb" ? "rgba(37,99,235,0.35)" : bg, borderColor: border }}
    >
      <span style={{ color: COLORS.downText }}>{bid}</span>
      <span style={{ color: COLORS.dim }}>×</span>
      <span style={{ color: COLORS.upText }}>{ask}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold" style={{ color: COLORS.text }}>
        {title}
      </h3>
      <div className="space-y-1.5 text-[11px] leading-5" style={{ color: COLORS.dim }}>
        {children}
      </div>
    </section>
  );
}

const SIGNAL_CARDS = [
  {
    color: COLORS.imbalance,
    name: "① 失衡堆积",
    desc: `一根柱里出现 3 个以上绿框（一侧吃单量 ≥ 对侧 ${IMBALANCE_RATIO} 倍）。连续失衡买入 = 买方志在必得，回踩常有支撑；连续失衡卖出反之。`,
  },
  {
    color: COLORS.divergence,
    name: "② Delta 背离",
    desc: "价格创新高但 Delta 为负（涨得心虚），或创新低但 Delta 为正（跌有人接）。常预示动能衰竭、可能反转。",
  },
  {
    color: COLORS.poc,
    name: "③ POC 支撑压力",
    desc: "黄框价位是成交最密集的「主战场」。价格在 POC 上方回踩它看支撑，下方反弹碰它看压力；反复争夺不下时等突破。",
  },
];

/** 读图指南：足迹图是什么 / 与 K 线区别 / 三大买卖信号 / 新手风险提醒 */
export default function GuideDrawer({ onClose }: { onClose: () => void }) {
  return (
    <>
      <div
        className="absolute inset-0 z-20"
        style={{ background: "rgba(0,0,0,0.45)" }}
        onClick={onClose}
      />
      <div
        className="absolute inset-y-0 right-0 z-30 flex w-[340px] max-w-full flex-col border-l shadow-2xl"
        style={{ background: "#0a1926", borderColor: COLORS.border }}
      >
        <div
          className="flex shrink-0 items-center border-b px-4 py-3"
          style={{ borderColor: COLORS.border }}
        >
          <h2 className="text-sm font-semibold" style={{ color: COLORS.text }}>
            ◘ 足迹图读图指南
          </h2>
          <button
            onClick={onClose}
            className="ml-auto rounded p-1 transition-colors hover:bg-white/10"
            style={{ color: COLORS.dim }}
            aria-label="关闭指南"
          >
            <X size={15} />
          </button>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4">
          <Section title="这张图是什么？">
            <p>
              普通 K 线只告诉你「价格到过哪」，足迹图把每根 K
              线切开，告诉你<b style={{ color: COLORS.text }}>每个价位上真实成交了多少买单和卖单</b>
              ——相当于从看「结果」升级到看「过程」，看见多空双方在哪儿真金白银交过手。
            </p>
            <div className="space-y-1 rounded-lg border p-2" style={{ borderColor: COLORS.border }}>
              <Cell bid="128" ask="415" highlight="buy" />
              <p className="pt-0.5">
                每格两个数字：<span style={{ color: COLORS.downText }}>左=主动卖出量</span>、
                <span style={{ color: COLORS.upText }}>右=主动买入量</span>。右比左大很多 =
                买方急迫，短期看涨；格子越<span style={{ color: COLORS.upText }}>蓝</span>买越强、越
                <span style={{ color: COLORS.downText }}>红</span>卖越强，颜色越深量越大。
              </p>
            </div>
            <div className="space-y-1 rounded-lg border p-2" style={{ borderColor: COLORS.border }}>
              <Cell bid="42" ask="386" highlight="imb" />
              <p className="pt-0.5">
                <span style={{ color: COLORS.imbalance }}>绿框 = 失衡格</span>：一侧吃单量达到对侧{" "}
                {IMBALANCE_RATIO} 倍以上，说明这个价位被单方面碾压，是强弱的直接证据。
              </p>
            </div>
            <div className="space-y-1 rounded-lg border p-2" style={{ borderColor: COLORS.border }}>
              <Cell bid="305" ask="316" poc />
              <p className="pt-0.5">
                <span style={{ color: COLORS.poc }}>黄框 = POC</span>
                ：这根柱成交量最大的价位，即多空的「主战场」，后续常成为支撑或压力。白色虚线是最新成交价。
              </p>
            </div>
          </Section>

          <Section title="底部四行数字怎么读？">
            <ul className="list-disc space-y-1 pl-4">
              <li>
                <b style={{ color: COLORS.text }}>成交量</b>：这根柱总共成交多少（k=千）。放量的柱更有参考意义。
              </li>
              <li>
                <b style={{ color: COLORS.text }}>Delta</b>：主动买 − 主动卖。
                <span style={{ color: COLORS.upText }}>正=买方主导（蓝）</span>、
                <span style={{ color: COLORS.downText }}>负=卖方主导（红）</span>。
              </li>
              <li>
                <b style={{ color: COLORS.text }}>Delta%</b>：Delta 占总量的比例，剔除「量大但打平」的干扰，±25% 以上算显著。
              </li>
              <li>
                <b style={{ color: COLORS.text }}>累计Δ</b>：把 Delta 逐柱累加的「资金潮水线」——持续上行说明买方一直占优，与价格走势背离时要警惕。
              </li>
            </ul>
          </Section>

          <Section title="三个最常用的买卖信号">
            {SIGNAL_CARDS.map((s) => (
              <div
                key={s.name}
                className="rounded-lg border-l-2 py-1 pl-2.5"
                style={{ borderColor: s.color, background: "rgba(255,255,255,0.02)" }}
              >
                <p className="font-medium" style={{ color: COLORS.text }}>
                  {s.name}
                </p>
                <p>{s.desc}</p>
              </div>
            ))}
            <p className="pt-1">
              图上会自动标出这三类信号的徽标（⚡扫盘 / ≣堆积 / ◈背离），点击徽标可看该信号的具体解释与风险。
            </p>
          </Section>

          <Section title="成交量分布（钟形曲线）怎么看？">
            <p>
              打开工具条的「分布」开关后，图表左侧会出现<b style={{ color: COLORS.text }}>横向的成交量分布图</b>
              ：每个价位一条横条，长度代表这段时间在该价位的累计成交量。大多数时候它长得像一口钟（正态分布）——
              市场在「公允价」附近换手最多，越偏离越少。
            </p>
            <ul className="list-disc space-y-1 pl-4">
              <li>
                <b style={{ color: "#fde047" }}>POC（黄色横线）</b>
                ：成交量最大的价位，市场的「重心」。价格常被它吸回（磁吸效应），也常在此获得支撑/压力。
              </li>
              <li>
                <b style={{ color: "#93c5fd" }}>价值区（蓝色淡带，70% 成交量）</b>
                ：上沿 VAH / 下沿 VAL。价格在区内=多空都认可的公允区间；离开价值区=一方开始占优。
              </li>
              <li>
                <b style={{ color: COLORS.text }}>HVN 高量节点</b>
                ：分布上的局部凸起，历史换手密集，行情到这儿容易停留或反复。
              </li>
              <li>
                <b style={{ color: COLORS.text }}>LVN 低量真空</b>
                ：分布上的凹陷，几乎没人在此成交过，价格常快速穿越，别指望它挡住行情。
              </li>
            </ul>
            <div
              className="space-y-1 rounded-lg border p-2"
              style={{ borderColor: COLORS.border }}
            >
              <p className="font-medium" style={{ color: COLORS.text }}>三个常用打法</p>
              <ul className="list-disc space-y-1 pl-4">
                <li><b style={{ color: COLORS.text }}>价值区回归</b>：价格冲出价值区但无量跟进 → 大概率回到区内，向 POC 靠拢。</li>
                <li><b style={{ color: COLORS.text }}>POC 磁吸</b>：横盘时价格反复被 POC 吸回，远离 POC 追单风险高。</li>
                <li><b style={{ color: COLORS.text }}>LVN 快速穿越</b>：价格进入低量真空区常加速通过，止损别设在真空区里。</li>
              </ul>
            </div>
            <p>
              分布可切「可见范围 / 今日」两种统计口径；大周期的远期柱若只有 K 线无逐笔明细，会自动跳过（解读卡片会注明覆盖范围）。
            </p>
          </Section>

          <Section title="新手风险提醒">
            <div
              className="space-y-1 rounded-lg border p-2.5"
              style={{ borderColor: "rgba(234,179,8,0.4)", background: "rgba(234,179,8,0.06)" }}
            >
              <p style={{ color: "#fde047" }}>信号会骗人，这不是投资建议。</p>
              <ul className="list-disc space-y-1 pl-4">
                <li>任何单一信号的历史胜率都远低于 100%，大资金也会故意制造假失衡「钓鱼」。</li>
                <li>信号只回答「谁在买卖」，不回答「接下来一定涨跌」；永远等价格走势确认再动手。</li>
                <li>新手先用小仓位或模拟盘验证 2-4 周，统计自己的胜率后再谈实盘。</li>
                <li>每笔交易先想清楚止损位——足迹图帮你找位置，止损纪律才保住本金。</li>
              </ul>
            </div>
          </Section>
        </div>
      </div>
    </>
  );
}
