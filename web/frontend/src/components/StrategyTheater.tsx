import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  IconActivityHeartbeat,
  IconAdjustmentsHorizontal,
  IconArrowRight,
  IconBriefcase2,
  IconChartDots3,
  IconChartLine,
  IconChevronDown,
  IconCircle,
  IconClock,
  IconFingerprint,
  IconFlask,
  IconGauge,
  IconLayoutDashboard,
  IconLogout,
  IconRadar2,
  IconRoute,
  IconScale,
  IconSettings,
  IconShieldCheck,
  IconShieldLock,
  IconTargetArrow,
  IconTimelineEvent,
  IconWaveSine
} from "@tabler/icons-react";
import { formatCurrency, formatNumber, formatPercent, toneByValue } from "../lib/format";
import type {
  Candidate,
  EquityPoint,
  ObservationResponse,
  PortfolioResponse,
  RpsResponse,
  StatusResponse,
  Trade
} from "../types";
import { EquitySparkline, RiskGauge } from "./Charts";

export type WorkspaceSection =
  | "theater"
  | "market"
  | "factors"
  | "portfolio"
  | "execution"
  | "backtest"
  | "system";

interface StrategyTheaterProps {
  activeSection: WorkspaceSection;
  onSectionChange: (section: WorkspaceSection) => void;
  onLogout: () => void;
  clock: string;
  status: StatusResponse | null;
  portfolio: PortfolioResponse | null;
  candidates: Candidate[];
  rps: RpsResponse | null;
  trades: Trade[];
  equity: EquityPoint[];
  observation: ObservationResponse | null;
  reducedMotion: boolean;
  children: ReactNode;
}

interface TimelineEvent {
  id: string;
  time: string;
  title: string;
  detail: string;
  tone: "active" | "success" | "muted";
}

const NAV_ITEMS: ReadonlyArray<{
  key: WorkspaceSection;
  label: string;
  icon: ReactNode;
}> = [
  { key: "theater", label: "策略剧场", icon: <IconLayoutDashboard size={19} /> },
  { key: "market", label: "行情监控", icon: <IconChartLine size={19} /> },
  { key: "factors", label: "因子看板", icon: <IconAdjustmentsHorizontal size={19} /> },
  { key: "portfolio", label: "组合持仓", icon: <IconBriefcase2 size={19} /> },
  { key: "execution", label: "交易执行", icon: <IconRoute size={19} /> },
  { key: "backtest", label: "回测分析", icon: <IconFlask size={19} /> },
  { key: "system", label: "系统状态", icon: <IconSettings size={19} /> }
];

const SECTION_COPY: Record<Exclude<WorkspaceSection, "theater">, { title: string; subtitle: string }> = {
  market: { title: "行情监控", subtitle: "查看账户净值、回撤与候选池变化" },
  factors: { title: "因子看板", subtitle: "检查 ETF 趋势、主板增强与数据准备状态" },
  portfolio: { title: "组合持仓", subtitle: "核对仓位、现金底线与已实现战绩" },
  execution: { title: "交易执行", subtitle: "追踪订单、T+1 约束与运行日志" },
  backtest: { title: "回测分析", subtitle: "比较策略风险收益，不以单次最高收益为目标" },
  system: { title: "系统状态", subtitle: "确认数据、进程与虚拟盘验收条件" }
};

function dateKey(): string {
  return new Date().toISOString().slice(0, 10).replace(/-/g, "");
}

function shortTime(value: string | null | undefined): string {
  if (!value) return "--:--";
  const match = String(value).match(/(\d{2}:\d{2})(?::\d{2})?/);
  return match?.[1] ?? String(value).slice(-5);
}

function formatDate(): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  })
    .format(new Date())
    .replaceAll("/", ".");
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function buildEvents(
  trades: Trade[],
  status: StatusResponse | null,
  portfolio: PortfolioResponse | null,
  candidates: Candidate[]
): TimelineEvent[] {
  const events: TimelineEvent[] = trades.slice(0, 3).map((trade, index) => {
    const action = (trade.action ?? trade.direction) === "buy" ? "买入" : "卖出";
    const rejected = trade.status === "rejected";
    return {
      id: `trade-${trade.date}-${trade.time}-${trade.code}-${index}`,
      time: shortTime(trade.time),
      title: rejected ? "订单被规则拦截" : `${action} ${trade.name || trade.code || "标的"}`,
      detail: rejected
        ? trade.reject_reason || trade.reason || "风险规则拒绝"
        : `${formatNumber(trade.shares, 0)} 股 · ${formatCurrency(trade.price ?? trade.actual_price, 3)}`,
      tone: rejected ? "active" : "success"
    };
  });

  if (candidates.length) {
    events.push({
      id: "candidate-refresh",
      time: shortTime(status?.now),
      title: "因子候选更新",
      detail: `${candidates.length} 个主板候选通过初筛`,
      tone: "active"
    });
  }
  if (portfolio?.updated_at) {
    events.push({
      id: "portfolio-refresh",
      time: shortTime(portfolio.updated_at),
      title: "账户快照更新",
      detail: `净值 ${formatCurrency(portfolio.total_value, 0)} · 仓位 ${(portfolio.position_ratio * 100).toFixed(1)}%`,
      tone: "muted"
    });
  }
  if (status?.last_log_time) {
    events.push({
      id: "runner-heartbeat",
      time: shortTime(status.last_log_time),
      title: "运行状态刷新",
      detail: status.live_runner ? "策略进程保持运行" : "策略进程当前未运行",
      tone: status.live_runner ? "success" : "muted"
    });
  }
  if (!events.length) {
    events.push({
      id: "idle",
      time: shortTime(status?.now),
      title: "等待新事件",
      detail: "系统已就绪，尚无交易与信号记录",
      tone: "muted"
    });
  }
  return events.slice(0, 5);
}

function Sidebar({
  activeSection,
  onSectionChange,
  status
}: Pick<StrategyTheaterProps, "activeSection" | "onSectionChange" | "status">) {
  return (
    <aside className="ops-sidebar" aria-label="量化工作区导航">
      <button className="ops-brand" type="button" onClick={() => onSectionChange("theater")}>
        <span className="ops-brand__mark" aria-hidden="true">
          <IconWaveSine size={28} stroke={2.2} />
        </span>
        <span>
          <strong>信号剧场</strong>
          <small>A股稳健策略 V2</small>
        </span>
        <IconChevronDown size={16} aria-hidden="true" />
      </button>

      <div className="ops-runtime">
        <span>实时状态</span>
        <strong className={status?.live_runner ? "is-running" : "is-stopped"}>
          <i aria-hidden="true" />
          {status?.live_runner ? "运行中" : "观察已停止"}
        </strong>
        <small>robust_v2</small>
      </div>

      <nav className="ops-nav">
        {NAV_ITEMS.map((item) => (
          <button
            className={activeSection === item.key ? "is-active" : ""}
            key={item.key}
            type="button"
            aria-current={activeSection === item.key ? "page" : undefined}
            onClick={() => onSectionChange(item.key)}
          >
            {item.icon}
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="ops-sidebar__footer">
        <span>数据提供 · A-Share OPS</span>
        <strong className={status?.web_server === false ? "is-stopped" : "is-running"}>
          <i aria-hidden="true" />
          数据连接 {status?.web_server === false ? "异常" : "正常"}
        </strong>
      </div>
    </aside>
  );
}

function RiskRail({
  clock,
  status,
  portfolio,
  equity,
  observation,
  reducedMotion,
  onLogout
}: Pick<
  StrategyTheaterProps,
  "clock" | "status" | "portfolio" | "equity" | "observation" | "reducedMotion" | "onLogout"
>) {
  const positionRatio = portfolio?.position_ratio ?? 0;
  const currentDrawdown = equity.at(-1)?.drawdown ?? 0;
  const cashRatio = portfolio?.total_value ? portfolio.cash / portfolio.total_value : 1;
  const healthOk = observation?.health?.ok !== false && !(observation?.health?.failures?.length);
  const riskScore = clamp(
    10 - (currentDrawdown / 0.1) * 4.5 - Math.max(0, positionRatio - 0.8) * 12 - (healthOk ? 0 : 2.5),
    0,
    10
  );
  const riskLabel = riskScore >= 7 ? "稳健" : riskScore >= 4 ? "关注" : "高风险";

  return (
    <aside className="risk-rail" aria-label="账户风险状态">
      <div className="risk-rail__clock">
        <span>{formatDate()}</span>
        <strong>{clock}</strong>
        <small className={status?.live_runner ? "is-running" : "is-stopped"}>
          <i aria-hidden="true" />
          {status?.live_runner ? "交易观察中" : "观察已停止"}
        </small>
      </div>

      <section className="risk-score">
        <span>风险状态</span>
        <strong className={`risk-score__label risk-${riskLabel}`}>{riskLabel}</strong>
        <RiskGauge score={riskScore} reducedMotion={reducedMotion} />
      </section>

      <dl className="risk-metrics">
        <div>
          <dt>总市值</dt>
          <dd>{formatCurrency(portfolio?.total_value, 0)}</dd>
          <small className={`tone-${toneByValue(portfolio?.pnl)}`}>
            日收益 {formatPercent(portfolio?.pnl_pct)} · {formatNumber(portfolio?.pnl, 0)} 元
          </small>
        </div>
        <div>
          <dt>现金</dt>
          <dd>{(cashRatio * 100).toFixed(1)}%</dd>
          <small>{formatCurrency(portfolio?.cash, 0)}</small>
        </div>
        <div>
          <dt>回撤</dt>
          <dd>{(currentDrawdown * 100).toFixed(2)}%</dd>
          <small>目标上限 10.00%</small>
        </div>
        <div>
          <dt>下一次调仓</dt>
          <dd className="risk-metrics__schedule">下一交易日 09:35</dd>
          <small>收盘信号确认后执行</small>
        </div>
      </dl>

      <button className="risk-rail__logout" type="button" onClick={onLogout}>
        <IconLogout size={17} />
        安全退出
      </button>
    </aside>
  );
}

function ProcessStage({
  index,
  label,
  status,
  icon,
  active,
  onClick
}: {
  index: number;
  label: string;
  status: string;
  icon: ReactNode;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={`process-stage ${active ? "is-active" : ""}`}
      type="button"
      aria-label={`阶段 ${index + 1}：${label}`}
      aria-pressed={active}
      onClick={onClick}
    >
      <span className="process-stage__number">{index + 1}</span>
      <span className="process-stage__label">{label}</span>
      <span className="process-stage__status">
        {active ? <IconActivityHeartbeat size={15} /> : <IconCircle size={13} />}
        {status}
      </span>
      <span className="process-stage__node" aria-hidden="true">
        {icon}
      </span>
    </button>
  );
}

function ReadinessRow({ label, value, negative = false }: { label: string; value: number; negative?: boolean }) {
  return (
    <div className="readiness-row">
      <span>{label}</span>
      <progress max={100} value={value} aria-label={`${label} ${value}%`} />
      <strong className={negative ? "tone-negative" : value >= 75 ? "tone-positive" : ""}>{value}%</strong>
    </div>
  );
}

function MarketCard({
  equity,
  portfolio,
  status,
  active,
  reducedMotion,
  onClick
}: Pick<StrategyTheaterProps, "equity" | "portfolio" | "status" | "reducedMotion"> & {
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`stage-card ${active ? "is-active" : ""}`} type="button" aria-label="查看账户行情快照详情" onClick={onClick}>
      <header>
        <span>账户行情快照</span>
        <small>{shortTime(status?.now)}</small>
      </header>
      <div className="market-value">
        <strong>{formatCurrency(portfolio?.total_value, 0)}</strong>
        <span className={`tone-${toneByValue(portfolio?.pnl_pct)}`}>{formatPercent(portfolio?.pnl_pct)}</span>
      </div>
      <EquitySparkline points={equity} reducedMotion={reducedMotion} />
      <dl className="stage-stats">
        <div><dt>现金</dt><dd>{formatCurrency(portfolio?.cash, 0)}</dd></div>
        <div><dt>持仓数</dt><dd>{portfolio?.position_count ?? 0}</dd></div>
        <div><dt>市场状态</dt><dd>{status?.live_runner ? "观察中" : "静默等待"}</dd></div>
      </dl>
    </button>
  );
}

function FactorCard({
  candidates,
  rps,
  observation,
  portfolio,
  status,
  active,
  onClick
}: Pick<StrategyTheaterProps, "candidates" | "rps" | "observation" | "portfolio" | "status"> & {
  active: boolean;
  onClick: () => void;
}) {
  const healthOk = observation?.health?.ok !== false && !(observation?.health?.failures?.length);
  const readiness = [
    { label: "数据健康", value: healthOk ? 100 : 35 },
    { label: "主板候选", value: candidates.length ? clamp(55 + candidates.length * 5, 0, 100) : 0 },
    { label: "ETF 趋势", value: rps?.status === "ok" ? 90 : rps?.available === false ? 45 : 20 },
    { label: "风险预算", value: clamp(100 - Math.max(0, (portfolio?.position_ratio ?? 0) - 0.6) * 150, 0, 100) },
    { label: "T+1 规则", value: 100 }
  ];
  const conclusion = !status?.live_runner
    ? "等待策略启动"
    : candidates.length
      ? "候选池已就绪"
      : "等待收盘信号";

  return (
    <button className={`stage-card factor-card ${active ? "is-active" : ""}`} type="button" aria-label="查看因子判断详情" onClick={onClick}>
      <header>
        <span>因子判断（就绪度）</span>
        <IconFingerprint size={17} />
      </header>
      <div className="readiness-list">
        {readiness.map((item) => <ReadinessRow key={item.label} {...item} />)}
      </div>
      <div className="signal-conclusion">
        <span>信号结论</span>
        <strong>{conclusion}</strong>
        <small>{candidates.length ? `${candidates.length} 个候选等待目标组合确认` : "不在盘中追涨，不强制开仓"}</small>
      </div>
    </button>
  );
}

function TargetCard({
  portfolio,
  active,
  onClick
}: Pick<StrategyTheaterProps, "portfolio"> & { active: boolean; onClick: () => void }) {
  const positions = portfolio?.positions ?? [];
  return (
    <button className={`stage-card target-card ${active ? "is-active" : ""}`} type="button" aria-label="查看目标组合详情" onClick={onClick}>
      <header>
        <span>目标组合（预览）</span>
        <IconTargetArrow size={18} />
      </header>
      {positions.length ? (
        <div className="target-list">
          {positions.slice(0, 3).map((position) => (
            <div key={position.code}>
              <span>{position.name || position.code}</span>
              <strong>{formatPercent(position.profit_pct)}</strong>
              <small>{position.shares} 股 · {formatCurrency(position.value, 0)}</small>
            </div>
          ))}
        </div>
      ) : (
        <div className="target-empty">
          <IconRadar2 size={48} stroke={1.2} />
          <strong>等待因子信号确认</strong>
          <span>收盘后生成次日目标组合</span>
        </div>
      )}
      <div className="allocation-policy">
        <span>ETF ≤ 60%</span>
        <span>主板个股 ≤ 20%</span>
        <span>现金 ≥ 20%</span>
      </div>
    </button>
  );
}

function ExecutionCard({
  trades,
  active,
  onClick
}: Pick<StrategyTheaterProps, "trades"> & { active: boolean; onClick: () => void }) {
  const blocked = trades.filter((trade) => trade.status === "rejected").length;
  const steps = [
    { label: "生成订单", icon: <IconTimelineEvent size={16} />, active: false },
    { label: "合规检查", icon: <IconShieldCheck size={16} />, active: false },
    { label: "T+1 拦截", icon: <IconShieldLock size={16} />, active: true },
    { label: "次日开盘执行", icon: <IconClock size={16} />, active: false }
  ];
  return (
    <button className={`stage-card execution-card ${active ? "is-active" : ""}`} type="button" aria-label="查看交易执行详情" onClick={onClick}>
      <header>
        <span>执行路径</span>
        <IconRoute size={18} />
      </header>
      <div className="execution-steps">
        {steps.map((step, index) => (
          <div className={step.active ? "is-guard" : ""} key={step.label}>
            <span>{step.icon}{step.label}</span>
            {index < steps.length - 1 && <IconArrowRight size={14} aria-hidden="true" />}
          </div>
        ))}
      </div>
      <div className="execution-note">
        <IconShieldLock size={20} />
        <span>
          <strong>A股 T+1 强制执行</strong>
          <small>{blocked ? `${blocked} 笔异常订单已记录` : "当日买入批次不可卖出"}</small>
        </span>
      </div>
      <small className="stage-card__footer">状态：等待收盘信号</small>
    </button>
  );
}

function RecentEvents({ events }: { events: TimelineEvent[] }) {
  return (
    <section className="recent-events" aria-labelledby="recent-events-title">
      <header>
        <span><IconTimelineEvent size={19} /><strong id="recent-events-title">最近事件</strong></span>
        <span className="compliance-ok"><IconShieldCheck size={18} />无违规交易</span>
      </header>
      <div className="recent-events__track">
        {events.map((event) => (
          <article className={`event-item is-${event.tone}`} key={event.id}>
            <span className="event-item__node" aria-hidden="true"><IconCircle size={12} /></span>
            <time>{event.time}</time>
            <strong>{event.title}</strong>
            <small>{event.detail}</small>
          </article>
        ))}
      </div>
    </section>
  );
}

function TheaterOverview(props: Omit<StrategyTheaterProps, "activeSection" | "onSectionChange" | "onLogout" | "clock" | "children">) {
  const { status, portfolio, candidates, rps, trades, equity, observation, reducedMotion } = props;
  const todayTrades = trades.filter((trade) => trade.date === dateKey());
  const derivedStage = todayTrades.length ? 3 : (portfolio?.position_count ?? 0) > 0 ? 2 : candidates.length ? 1 : status?.live_runner ? 0 : 1;
  const [focusedStage, setFocusedStage] = useState(derivedStage);

  useEffect(() => {
    setFocusedStage(derivedStage);
  }, [derivedStage]);

  const events = useMemo(
    () => buildEvents(trades, status, portfolio, candidates),
    [trades, status, portfolio, candidates]
  );
  const stageStatus = [
    status?.now ? `已更新 ${shortTime(status.now)}` : "等待数据",
    candidates.length ? `${candidates.length} 个候选` : status?.live_runner ? "分析中" : "等待启动",
    (portfolio?.position_count ?? 0) > 0 ? "组合已持有" : "待生成",
    todayTrades.length ? `${todayTrades.length} 笔记录` : "等待收盘信号"
  ];
  const stages = [
    { label: "行情快照", icon: <IconChartDots3 size={17} /> },
    { label: "因子判断", icon: <IconScale size={17} /> },
    { label: "目标组合", icon: <IconTargetArrow size={17} /> },
    { label: "T+1 执行", icon: <IconShieldLock size={17} /> }
  ];

  return (
    <motion.div
      className="theater-overview"
      initial={reducedMotion ? false : { opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, ease: "easeOut" }}
    >
      <section className="process-hero" aria-labelledby="theater-title">
        <motion.div
          className="process-hero__energy"
          aria-hidden="true"
          animate={reducedMotion ? undefined : { x: [0, -14, 0], opacity: [0.68, 0.92, 0.68] }}
          transition={{ duration: 12, repeat: Infinity, ease: "easeInOut" }}
        />
        <header className="process-hero__head">
          <div>
            <span className="section-kicker">ROBUST V2 · PAPER ACCOUNT</span>
            <h1 id="theater-title">策略运行剧场</h1>
            <p>让策略决策过程可视、可追溯、可验证</p>
          </div>
          <span className="process-hero__mode">
            <IconGauge size={18} />
            低频稳健模式
          </span>
        </header>

        <div className="process-track" style={{ "--active-stage": focusedStage } as CSSProperties}>
          <motion.span
            className="process-track__pulse"
            aria-hidden="true"
            animate={reducedMotion ? undefined : { scale: [0.86, 1.16, 0.86], opacity: [0.65, 1, 0.65] }}
            transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut" }}
          />
          {stages.map((stage, index) => (
            <ProcessStage
              key={stage.label}
              index={index}
              label={stage.label}
              status={stageStatus[index]}
              icon={stage.icon}
              active={focusedStage === index}
              onClick={() => setFocusedStage(index)}
            />
          ))}
        </div>
      </section>

      <section className="stage-grid" aria-label="策略四阶段详情">
        <MarketCard {...{ equity, portfolio, status, reducedMotion }} active={focusedStage === 0} onClick={() => setFocusedStage(0)} />
        <FactorCard {...{ candidates, rps, observation, portfolio, status }} active={focusedStage === 1} onClick={() => setFocusedStage(1)} />
        <TargetCard portfolio={portfolio} active={focusedStage === 2} onClick={() => setFocusedStage(2)} />
        <ExecutionCard trades={trades} active={focusedStage === 3} onClick={() => setFocusedStage(3)} />
      </section>

      <section className="strategy-explainer">
        <IconRadar2 size={21} />
        <div>
          <strong>策略说明</strong>
          <p>ETF 使用 20/60/120 日风险调整收益与绝对趋势，主板增强使用低 PB、中小市值、短期反转和盈利质量；组合保留至少 20% 现金，盘中只处理灾难止损。</p>
        </div>
      </section>

      <RecentEvents events={events} />
    </motion.div>
  );
}

export function StrategyTheater({
  activeSection,
  onSectionChange,
  onLogout,
  clock,
  status,
  portfolio,
  candidates,
  rps,
  trades,
  equity,
  observation,
  reducedMotion,
  children
}: StrategyTheaterProps) {
  return (
    <main className="theater-shell">
      <Sidebar activeSection={activeSection} onSectionChange={onSectionChange} status={status} />
      <section className="theater-main">
        <AnimatePresence mode="wait">
          {activeSection === "theater" ? (
            <TheaterOverview
              key="theater"
              status={status}
              portfolio={portfolio}
              candidates={candidates}
              rps={rps}
              trades={trades}
              equity={equity}
              observation={observation}
              reducedMotion={reducedMotion}
            />
          ) : (
            <motion.div
              className="workspace-view"
              key={activeSection}
              initial={reducedMotion ? false : { opacity: 0, x: 14 }}
              animate={{ opacity: 1, x: 0 }}
              exit={reducedMotion ? undefined : { opacity: 0, x: -10 }}
              transition={{ duration: 0.24, ease: "easeOut" }}
            >
              <header className="workspace-view__head">
                <div>
                  <span className="section-kicker">ROBUST V2 WORKSPACE</span>
                  <h1>{SECTION_COPY[activeSection].title}</h1>
                  <p>{SECTION_COPY[activeSection].subtitle}</p>
                </div>
                <button type="button" onClick={() => onSectionChange("theater")}>
                  返回策略剧场
                  <IconArrowRight size={17} />
                </button>
              </header>
              {children}
            </motion.div>
          )}
        </AnimatePresence>
      </section>
      <RiskRail
        clock={clock}
        status={status}
        portfolio={portfolio}
        equity={equity}
        observation={observation}
        reducedMotion={reducedMotion}
        onLogout={onLogout}
      />
    </main>
  );
}
