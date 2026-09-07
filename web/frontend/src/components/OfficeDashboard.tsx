import { Component, Suspense, lazy, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import {
  IconArrowUpRight, IconChartBar, IconCoffee, IconCube, IconDeviceDesktop,
  IconFlask, IconLeaf, IconLogout, IconMaximize, IconPlayerPause,
  IconPlayerPlay, IconRotate, IconSun, IconX
} from "@tabler/icons-react";
import { formatCurrency, formatPercent, toneByValue } from "../lib/format";
import { usePageVisible } from "../hooks/usePageVisible";
import type { Candidate, CandidatesResponse, EquityPoint, ObservationResponse, PortfolioResponse, RpsResponse, StatusResponse, Trade } from "../types";
import { TEAM, type OfficeMetrics, type OfficeRole, type OfficeSceneProps, type WorkspaceSection } from "./office/types";

const OfficeScene = lazy(() => import("./office/OfficeScene").then((module) => ({ default: module.OfficeScene })));

interface OfficeDashboardProps {
  activeSection: WorkspaceSection;
  onSectionChange: (section: WorkspaceSection) => void;
  onLogout: () => void;
  clock: string;
  status: StatusResponse | null;
  portfolio: PortfolioResponse | null;
  candidates: Candidate[];
  candidateScan: CandidatesResponse | null;
  rps: RpsResponse | null;
  trades: Trade[];
  equity: EquityPoint[];
  observation: ObservationResponse | null;
  reducedMotion: boolean;
  feedError?: string | null;
  children: ReactNode;
}

const PANELS: Record<Exclude<WorkspaceSection, "theater">, { title: string; label: string; description: string }> = {
  market: { title: "行情观察", label: "行情监控", description: "账户的每一次变化，都在这里留下刻度。" },
  factors: { title: "研究笔记", label: "因子看板", description: "从候选池到 ETF 基线，慢慢验证每一个想法。" },
  portfolio: { title: "资产手账", label: "组合持仓", description: "看看资金住在哪里，还有多少空间留给明天。" },
  execution: { title: "执行记录", label: "交易执行", description: "交易、拒单和日志，让每一步都有迹可循。" },
  system: { title: "巡检日志", label: "系统状态", description: "运行心跳和数据健康，都需要认真照看。" },
  backtest: { title: "回测资料室", label: "回测分析", description: "回到过去检验想法，历史成绩不代表未来表现。" }
};

class SceneBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed
      ? <div className="office-scene-message" role="status"><IconCube size={34} /><strong>场景暂时无法打开</strong><span>点击下方同事卡片，仍可查看全部数据。</span></div>
      : this.props.children;
  }
}

/** 三维办公室的导航与真实数据入口；角色互动仅改变视角和面板。 */
export function OfficeDashboard({ activeSection, onSectionChange, onLogout, clock, status, portfolio, candidates, candidateScan, rps, trades, equity, observation, reducedMotion, feedError, children }: OfficeDashboardProps) {
  const visible = usePageVisible();
  const [paused, setPaused] = useState(false);
  const [quality, setQuality] = useState<OfficeSceneProps["quality"]>("balanced");
  const [view, setView] = useState<OfficeSceneProps["view"]>("overview");
  const [resetKey, setResetKey] = useState(0);
  const drawerTitle = useRef<HTMLHeadingElement>(null);
  const lastTrigger = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  const open = activeSection !== "theater";
  const role = TEAM.find((person) => person.id === activeSection);
  const panel = open ? PANELS[activeSection] : null;
  const date = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", weekday: "long", timeZone: "Asia/Shanghai" }).format(new Date());
  const today = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" }).format(new Date()).replaceAll("-", "");
  const todayTrades = trades.filter((trade) => trade.date === today);
  const rejectedCount = todayTrades.filter((trade) => trade.status === "rejected").length;
  const healthy = observation?.health?.failures?.length || observation?.health?.ok === false ? false : observation?.health?.ok === true ? true : null;
  const healthLabel = healthy === null ? "健康待确认" : healthy ? "健康检查通过" : "健康检查异常";
  const runtimeLabel = status?.live_runner === undefined ? "等待运行状态" : status.live_runner ? "策略运行中" : "策略已停止";
  const lifePaused = paused || reducedMotion || !visible;
  const metrics = useMemo<OfficeMetrics>(() => ({
    totalValue: portfolio && Number.isFinite(portfolio.total_value) ? portfolio.total_value : null,
    cashRatio: portfolio?.total_value && Number.isFinite(portfolio.cash) ? portfolio.cash / portfolio.total_value : null,
    candidates: candidateScan?.status === "completed" ? candidates.length : null,
    trades: todayTrades.length,
    healthy,
    equity: equity.map((point) => point.value).filter(Number.isFinite)
  }), [portfolio, candidateScan?.status, candidates.length, todayTrades.length, healthy, equity]);

  const summaries: Record<OfficeRole, string> = {
    market: portfolio?.updated_at ? `快照 · ${portfolio.updated_at.slice(-8)}` : "等待账户快照",
    factors: candidateScan?.scan_running ? "安全预览扫描中" : candidateScan?.status === "failed" ? "扫描需要关注" : candidateScan?.status === "completed" ? `${candidateScan.eligible_count ?? candidates.length} 只候选 · ${rps?.status === "ok" ? "ETF 已就绪" : "ETF 待确认"}` : "等待首次扫描",
    portfolio: Number.isFinite(portfolio?.position_count) ? `${portfolio?.position_count} 只持仓 · ${portfolio?.quotes_degraded ? "账本估值" : "账户快照"}` : "等待持仓数据",
    execution: `今日 ${todayTrades.length} 笔 · ${rejectedCount} 笔拒单`,
    system: healthLabel
  };

  useEffect(() => {
    if (open) {
      const active = document.activeElement;
      if (active instanceof HTMLElement && active !== document.body && !active.closest(".office-drawer")) lastTrigger.current = active;
      drawerTitle.current?.focus({ preventScroll: true });
    } else if (wasOpen.current && lastTrigger.current?.isConnected) {
      lastTrigger.current.focus({ preventScroll: true });
    }
    wasOpen.current = open;
  }, [activeSection, open]);

  useEffect(() => {
    if (!open) return;
    const closeWithEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onSectionChange("theater");
    };
    document.addEventListener("keydown", closeWithEscape);
    return () => document.removeEventListener("keydown", closeWithEscape);
  }, [open, onSectionChange]);

  const changeView = (next: OfficeSceneProps["view"]) => {
    setView(next);
    onSectionChange("theater");
    setResetKey((value) => value + 1);
  };

  return (
    <main className={`office-shell${open ? " has-drawer" : ""}`} data-motion={lifePaused ? "paused" : "running"}>
      <header className="office-header">
        <button className="office-brand" type="button" onClick={() => changeView("overview")} aria-label="回到办公室全景">
          <span className="office-brand__mark" aria-hidden="true"><IconCube size={25} stroke={1.5} /></span>
          <span><strong>慢慢事务所<span>®</span></strong><small>MARKET OFFICE</small></span>
        </button>
        <div className="office-account" aria-label="账户概况">
          <div><span>总资产 <small>CNY</small></span><strong>{formatCurrency(portfolio?.total_value, 0)}</strong></div>
          <div><span>累计收益</span><strong className={`tone-${toneByValue(portfolio?.pnl_pct)}`}>{formatPercent(portfolio?.pnl_pct)}</strong></div>
          <div className="office-account__cash"><span>现金占比</span><strong>{metrics.cashRatio === null ? "--" : `${(metrics.cashRatio * 100).toFixed(1)}%`}</strong></div>
        </div>
        <div className="office-header__end">
          <div className="office-runtime"><span className={status?.live_runner === undefined ? "is-unknown" : status.live_runner ? "is-running" : "is-stopped"}><i />{runtimeLabel}</span><time>{date} <b>{clock}</b></time></div>
          <button className="office-icon-button office-logout" type="button" onClick={onLogout} aria-label="安全退出" title="安全退出"><IconLogout size={18} /></button>
        </div>
      </header>

      {(feedError || portfolio?.quotes_degraded || healthy === false) && <div className="office-warning" role="status">
        {feedError && <span>部分数据刷新失败：{feedError}</span>}
        {portfolio?.quotes_degraded && <span>部分行情未能获取，当前使用账本价格估值；请核对后再判断收益。</span>}
        {healthy === false && <button type="button" onClick={() => onSectionChange("system")}>健康检查未通过，查看巡检日志 <IconArrowUpRight size={14} /></button>}
      </div>}

      <section className="office-stage" aria-label="三维市场办公室">
        <div className="office-scene-host">
          <SceneBoundary><Suspense fallback={<div className="office-scene-message" role="status"><IconCube size={34} /><strong>正在打开办公室</strong><span>摆好桌椅，等同事们就位。</span></div>}>
            <OfficeScene selected={activeSection} onSelect={onSectionChange} metrics={metrics} paused={lifePaused} reducedMotion={reducedMotion} quality={quality} view={view} resetKey={resetKey} />
          </Suspense></SceneBoundary>
        </div>
        {view === "overview" && <div className="office-stage__intro" aria-hidden={open ? true : undefined}>
          <p className="office-eyebrow"><span /> A LITTLE SPACE TO WATCH THE MARKET</p>
          <h1>市场很快，<br />在这里慢慢看<span>。</span></h1>
          <p>五位同事，一间小小的办公室。<br />点一点他们，看看今天在忙什么。</p>
          <div className="office-intro__rule"><i /><span>观察 · 研究 · 记录</span></div>
        </div>}
        <nav className="office-board-links" aria-label="办公室看板">
          <button type="button" onClick={() => onSectionChange("market")} aria-label="打开行情看板"><IconChartBar size={18} /><span>行情看板</span><IconArrowUpRight size={15} /></button>
          <button type="button" onClick={() => onSectionChange("backtest")} aria-label="回测分析"><IconFlask size={18} /><span>回测资料室</span><IconArrowUpRight size={15} /></button>
        </nav>
        <div className="office-location"><IconSun size={18} /><span>{view === "lounge" ? "02 / 茶水间" : view === "desks" ? "01 / 工作区" : "00 / 办公室全景"}</span><i />{lifePaused ? "生活已暂停" : "同事们各忙各的"}</div>
        <div className="office-hint"><span>拖动旋转</span><i>·</i><span>滚轮缩放</span><i>·</i><span>点选小人</span></div>

        {open && panel && <aside className="office-drawer" role="dialog" aria-modal="false" aria-labelledby="office-drawer-title" key={activeSection}>
          <header className="office-drawer__head">
            <div className="office-drawer__eyebrow"><span style={{ background: role?.color ?? "#e3a866" }} />{role ? `${role.name} / ${role.title}` : "THE ARCHIVE / 历史研究"}</div>
            <button className="office-icon-button" type="button" onClick={() => onSectionChange("theater")} aria-label="关闭信息面板"><IconX size={20} /></button>
            <h2 id="office-drawer-title" tabIndex={-1} ref={drawerTitle}>{panel.title}<span>↗</span></h2>
            <p>{panel.description}</p>
            {role && <div className="office-drawer__status"><i />数据摘要：{summaries[role.id]}</div>}
          </header>
          <div className="office-drawer__content">{children}</div>
          <footer className="office-drawer__footer"><span>MARKET OFFICE / 观察记录</span><span>ESC 关闭</span></footer>
        </aside>}
      </section>

      <footer className="office-bottom">
        <div className="office-dock-row">
          <div className="office-dock-label"><span>MEET THE TEAM</span><strong>今天，找谁聊聊？</strong></div>
          <nav className="office-team" aria-label="同事与数据面板">
            {TEAM.map((person, index) => <button type="button" key={person.id} style={{ "--person-color": person.color } as CSSProperties} aria-label={`${person.name} · ${person.title} · ${PANELS[person.id].label}`} aria-pressed={activeSection === person.id} onClick={() => onSectionChange(person.id)} title={person.description}>
              <span className="office-person-symbol" aria-hidden="true"><span>{String(index + 1).padStart(2, "0")}</span><i /></span>
              <span className="office-person-copy"><strong>{person.name}<IconArrowUpRight size={12} /></strong><small>{person.title}</small></span>
            </button>)}
          </nav>
        </div>
        <div className="office-tools-row">
          <div className="office-views" role="group" aria-label="场景视角">
            <button type="button" aria-pressed={view === "overview" && !open} onClick={() => changeView("overview")}><IconMaximize size={15} />全景</button>
            <button type="button" aria-pressed={view === "desks" && !open} onClick={() => changeView("desks")}><IconDeviceDesktop size={15} />工位</button>
            <button type="button" aria-pressed={view === "lounge" && !open} onClick={() => changeView("lounge")}><IconCoffee size={15} />茶水间</button>
          </div>
          <span className="office-daily-note">角色日常仅为场景演出，信息以数据面板为准。</span>
          <div className="office-tools">
            <button type="button" aria-pressed={paused || reducedMotion} disabled={reducedMotion} onClick={() => setPaused((value) => !value)}>{lifePaused ? <IconPlayerPlay size={15} /> : <IconPlayerPause size={15} />}{reducedMotion ? "系统减弱动态" : paused ? "继续生活" : "暂停生活"}</button>
            <button type="button" aria-pressed={quality === "low"} onClick={() => setQuality((value) => value === "low" ? "balanced" : "low")}><IconLeaf size={15} />低耗模式</button>
            <button type="button" onClick={() => changeView("overview")} aria-label="重置镜头" title="重置镜头"><IconRotate size={15} /></button>
          </div>
        </div>
      </footer>
    </main>
  );
}
