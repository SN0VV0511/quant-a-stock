import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  IconActivity as Activity,
  IconBriefcase2 as Briefcase,
  IconEye as Eye,
  IconEyeOff as EyeOff,
  IconFlask as FlaskConical,
  IconLockAccess as UserLock,
  IconRadar2 as Radar,
  IconSearch as Search,
  IconTerminal2 as Terminal,
  IconTrophy as Trophy,
  IconTrendingUp as TrendingUp
} from "@tabler/icons-react";
import { api } from "./lib/api";
import { formatCurrency, formatNumber, formatPercent, toneByValue } from "./lib/format";
import { usePolling } from "./hooks/usePolling";
import { useReducedMotion } from "./hooks/useReducedMotion";
import { AllocationChart, BacktestChart, EquityCharts } from "./components/Charts";
import { HudCard } from "./components/HudCard";
import { StrategyTheater, type WorkspaceSection } from "./components/StrategyTheater";
import type { BacktestSeries, Candidate, CandidatesResponse, ObservationResponse, Position, ProfitRankItem, RpsOrder, RpsSignal, Trade } from "./types";

function toneClass(value: number | null | undefined) {
  return `tone-${toneByValue(value)}`;
}

function useClock() {
  const [clock, setClock] = useState(() => new Date().toLocaleTimeString("zh-CN"));
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date().toLocaleTimeString("zh-CN")), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return clock;
}

function LoginView({ onLogin }: { onLogin: () => void }) {
  const [mode, setMode] = useState<"login" | "change">("login");
  const [password, setPassword] = useState("");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submitLogin = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setMessage(null);
    try {
      const response = await api.login(password);
      if (response.success) {
        onLogin();
        window.history.replaceState({}, "", "/quantify/");
      } else {
        setMessage(response.error ?? "密码错误，请重试");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "登录失败，请重试");
    } finally {
      setSubmitting(false);
    }
  };

  const submitChange = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setMessage(null);
    setSuccess(null);
    if (newPassword.length < 6) {
      setMessage("新密码至少 6 位");
      setSubmitting(false);
      return;
    }
    try {
      const response = await api.changePassword(oldPassword, newPassword);
      if (response.success) {
        setSuccess("密码修改成功，请使用新密码登录");
        setOldPassword("");
        setNewPassword("");
      } else {
        setMessage(response.error ?? "修改失败");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "修改失败，请确认已登录或旧密码正确");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-shell">
      <div className="hud-bg" aria-hidden="true" />
      <motion.section
        className="auth-panel"
        initial={{ opacity: 0, y: 22, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.36, ease: "easeOut" }}
      >
        <div className="auth-panel__beam" aria-hidden="true" />
        <div className="auth-panel__brand">
          <span className="brand-mark">
            <UserLock size={22} />
          </span>
          <div>
            <p className="eyebrow">SECURE TERMINAL</p>
            <h1>量化盯盘系统</h1>
          </div>
        </div>
        <div className="segmented" role="tablist" aria-label="认证模式">
          <button className={mode === "login" ? "active" : ""} onClick={() => setMode("login")} type="button">
            登录
          </button>
          <button className={mode === "change" ? "active" : ""} onClick={() => setMode("change")} type="button">
            修改密码
          </button>
        </div>

        <AnimatePresence mode="wait">
          {mode === "login" ? (
            <motion.form
              key="login"
              className="auth-form"
              onSubmit={submitLogin}
              initial={{ opacity: 0, x: -12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 12 }}
            >
              <label htmlFor="password">访问密码</label>
              <div className="input-wrap">
                <input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoFocus
                  required
                />
                <button
                  type="button"
                  className="icon-button"
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                  onClick={() => setShowPassword((next) => !next)}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
              <button className="primary-action" type="submit" disabled={submitting}>
                {submitting ? "认证中..." : "进入终端"}
              </button>
            </motion.form>
          ) : (
            <motion.form
              key="change"
              className="auth-form"
              onSubmit={submitChange}
              initial={{ opacity: 0, x: 12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -12 }}
            >
              <label htmlFor="old-password">旧密码</label>
              <input
                id="old-password"
                type="password"
                autoComplete="current-password"
                value={oldPassword}
                onChange={(event) => setOldPassword(event.target.value)}
                required
              />
              <label htmlFor="new-password">新密码</label>
              <input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                required
                minLength={6}
              />
              <button className="primary-action" type="submit" disabled={submitting}>
                {submitting ? "提交中..." : "更新密码"}
              </button>
            </motion.form>
          )}
        </AnimatePresence>

        <div className="auth-message" role="status" aria-live="polite">
          {message && <span className="tone-negative">{message}</span>}
          {success && <span className="tone-positive">{success}</span>}
        </div>
        <p className="auth-disclaimer">个人量化研究工具，数据存在延迟与误差，不构成投资建议。</p>
      </motion.section>
    </main>
  );
}

function KpiCard({
  label,
  value,
  sub,
  tone = "neutral",
  meter
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "positive" | "negative" | "neutral" | "accent" | "warn";
  meter?: { value: number; tone: "positive" | "negative" | "accent" | "warn" };
}) {
  return (
    <motion.div className="kpi-card" whileHover={{ y: -3 }} transition={{ duration: 0.18 }}>
      <div className="kpi-card__label">{label}</div>
      <div className={`kpi-card__value tone-${tone}`}>{value}</div>
      {sub && <div className="kpi-card__sub">{sub}</div>}
      {meter && (
        <div className="meter" aria-hidden="true">
          <span className={`meter__fill tone-${meter.tone}`} style={{ width: `${Math.max(0, Math.min(100, meter.value))}%` }} />
        </div>
      )}
    </motion.div>
  );
}

function DashboardView({ onLogout }: { onLogout: () => void }) {
  const reducedMotion = useReducedMotion();
  const clock = useClock();
  const [activeSection, setActiveSection] = useState<WorkspaceSection>("theater");
  const [logLines, setLogLines] = useState(100);
  const [autoScroll, setAutoScroll] = useState(true);
  const [showRejected, setShowRejected] = useState(false);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [scanTriggering, setScanTriggering] = useState(false);
  const [scanMessage, setScanMessage] = useState<string | null>(null);

  const status = usePolling(api.status, 5000);
  const portfolio = usePolling(api.portfolio, 5000);
  const trades = usePolling(useCallback(() => api.trades(selectedDate ?? undefined), [selectedDate]), 5000);
  const candidates = usePolling(api.candidates, 5000);
  const rps = usePolling(api.rps, 5000);
  const profitRanking = usePolling(api.profitRanking, 30000);
  const equity = usePolling(api.equity, 5000);
  const logs = usePolling(useCallback(() => api.logs(logLines), [logLines]), 5000);
  const observation = usePolling(api.observation, 60000);
  const backtest = usePolling(api.backtest, 60000);

  const logout = async () => {
    await api.logout();
    onLogout();
    window.history.replaceState({}, "", "/quantify/login");
  };

  const triggerPreviewScan = async () => {
    setScanTriggering(true);
    setScanMessage(null);
    try {
      const response = await api.triggerScan();
      setScanMessage(response.message);
      await Promise.all([candidates.refresh(), status.refresh()]);
    } catch (error) {
      setScanMessage(error instanceof Error ? error.message : "安全预览扫描启动失败");
    } finally {
      setScanTriggering(false);
    }
  };

  const portfolioData = portfolio.data;
  const positionRatio = portfolioData?.position_ratio ?? 0;
  const currentDrawdown = (equity.data?.points ?? []).at(-1)?.drawdown ?? 0;
  const allTrades = (trades.data?.trades ?? []).slice().reverse();
  const visibleTrades = showRejected ? allTrades : allTrades.filter((trade) => trade.status !== "rejected");
  const rpsOrders = rps.data?.orders ?? [];
  const activeOrders = rpsOrders.filter((order) => order.status !== "risk_rejected");
  const backtestSeries = backtest.data?.series ?? [];

  const workspaceContent = (() => {
    switch (activeSection) {
      case "market":
        return (
          <section className="workspace-grid">
            <div className="workspace-kpis span-12">
              <KpiCard
                label="总市值"
                value={formatCurrency(portfolioData?.total_value, 0)}
                sub={`${formatPercent(portfolioData?.pnl_pct)} (${formatNumber(portfolioData?.pnl, 0)} 元)`}
                tone={toneByValue(portfolioData?.pnl)}
              />
              <KpiCard label="可用现金" value={formatCurrency(portfolioData?.cash, 0)} sub={`现金占比 ${((1 - positionRatio) * 100).toFixed(1)}%`} />
              <KpiCard
                label="持仓仓位"
                value={`${(positionRatio * 100).toFixed(1)}%`}
                sub={`${portfolioData?.position_count ?? 0} 只持仓`}
                tone={positionRatio > 0.6 ? "negative" : positionRatio > 0.45 ? "warn" : "accent"}
                meter={{ value: positionRatio * 100, tone: positionRatio > 0.6 ? "negative" : positionRatio > 0.45 ? "warn" : "accent" }}
              />
              <KpiCard
                label="当前回撤"
                value={`${(currentDrawdown * 100).toFixed(2)}%`}
                sub="目标上限 10%"
                tone={currentDrawdown > 0.06 ? "negative" : currentDrawdown > 0.03 ? "warn" : "neutral"}
                meter={{
                  value: (currentDrawdown / 0.1) * 100,
                  tone: currentDrawdown > 0.06 ? "negative" : currentDrawdown > 0.03 ? "warn" : "accent"
                }}
              />
            </div>
            <HudCard
              className="span-12"
              title="净值曲线 / 回撤"
              icon={<TrendingUp size={18} />}
              meta={equity.data?.points?.length ? `${equity.data.points.length} 个快照` : equity.error ?? "--"}
            >
              {equity.data?.points?.length ? <EquityCharts points={equity.data.points} reducedMotion={reducedMotion} /> : <EmptyState text="暂无净值数据，运行虚拟盘后生成快照。" />}
            </HudCard>
            <HudCard title="候选股雷达" icon={<Search size={18} />} meta={candidates.data?.updated_at || "--"}>
              <CandidatePanel
                data={candidates.data}
                triggering={scanTriggering}
                message={scanMessage}
                onTrigger={triggerPreviewScan}
              />
            </HudCard>
            <HudCard title="运行观察" icon={<Radar size={18} />} meta={healthMeta(observation.data)}>
              <ObservationPanel data={observation.data} error={observation.error} />
            </HudCard>
          </section>
        );
      case "factors":
        return (
          <section className="workspace-grid">
            <HudCard title="主板候选池" icon={<Search size={18} />} meta={candidates.data?.updated_at || "--"}>
              <CandidatePanel
                data={candidates.data}
                triggering={scanTriggering}
                message={scanMessage}
                onTrigger={triggerPreviewScan}
              />
            </HudCard>
            <HudCard title="ETF / RPS 研究基线" icon={<Radar size={18} />} meta={rpsStatus(rps.data)}>
              <RpsPanel signals={rps.data?.etf_signals ?? []} industries={rps.data?.industry_signals ?? []} orders={activeOrders} hiddenCount={rpsOrders.length - activeOrders.length} errors={rps.data?.errors ?? []} />
            </HudCard>
            <HudCard className="span-12" title="策略验收状态" icon={<FlaskConical size={18} />} meta={healthMeta(observation.data)}>
              <ObservationPanel data={observation.data} error={observation.error} />
            </HudCard>
          </section>
        );
      case "portfolio":
        return (
          <section className="workspace-grid">
            <HudCard title="持仓分布" icon={<Briefcase size={18} />} meta={`${portfolioData?.position_count ?? 0} 只`}>
              <AllocationChart cash={portfolioData?.cash ?? 0} positions={portfolioData?.positions ?? []} reducedMotion={reducedMotion} />
              <PositionList items={portfolioData?.positions ?? []} cash={portfolioData?.cash ?? 0} totalValue={portfolioData?.total_value ?? 0} />
            </HudCard>
            <HudCard className="profit-ranking-card" title="持仓战绩榜" icon={<Trophy size={18} />} meta="历史累计收益率">
              <ProfitRanking ranking={profitRanking.data?.ranking ?? []} />
            </HudCard>
          </section>
        );
      case "execution":
        return (
          <section className="workspace-grid">
            <HudCard
              title="操作记录"
              icon={<Activity size={18} />}
              meta={
                <label className="checkline">
                  <input type="checkbox" checked={showRejected} onChange={(event) => setShowRejected(event.target.checked)} />
                  显示异常
                </label>
              }
            >
              <DateFilter dates={trades.data?.dates ?? []} selected={selectedDate} onSelect={setSelectedDate} />
              <TradeList trades={visibleTrades} />
            </HudCard>
            <HudCard
              title="实时日志"
              icon={<Terminal size={18} />}
              meta={
                <div className="inline-actions">
                  <button className={logLines === 100 ? "chip active" : "chip"} onClick={() => setLogLines(100)} type="button">100</button>
                  <button className={logLines === 300 ? "chip active" : "chip"} onClick={() => setLogLines(300)} type="button">300</button>
                  <button className={autoScroll ? "chip active" : "chip"} onClick={() => setAutoScroll((next) => !next)} type="button">自动</button>
                </div>
              }
            >
              <LogPanel lines={logs.data?.logs ?? []} autoScroll={autoScroll} />
            </HudCard>
          </section>
        );
      case "backtest":
        return (
          <section className="workspace-grid">
            <HudCard className="span-12" title="策略回测对比" icon={<FlaskConical size={18} />} meta={backtestMeta(backtest.data)}>
              {backtestSeries.length ? (
                <div className="backtest-layout">
                  <BacktestChart series={backtestSeries} reducedMotion={reducedMotion} />
                  <BacktestTable series={backtestSeries} />
                </div>
              ) : (
                <EmptyState text={backtest.data?.generating ? "策略回测生成中，完成后自动显示。" : backtest.data?.error ? `策略回测生成失败：${backtest.data.error}` : "暂无回测结果，系统会在后台自动生成。"} />
              )}
            </HudCard>
          </section>
        );
      case "system":
        return (
          <section className="workspace-grid">
            <HudCard title="运行状态" icon={<Activity size={18} />} meta={status.error ?? "实时"}>
              <div className="metric-stack">
                <div className="kv-row"><span>策略进程</span><strong className={status.data?.live_runner ? "tone-positive" : "tone-negative"}>{status.data?.live_runner ? "运行中" : "已停止"}</strong></div>
                <div className="kv-row"><span>Web 服务</span><strong className={status.data?.web_server === false ? "tone-negative" : "tone-positive"}>{status.data?.web_server === false ? "异常" : "正常"}</strong></div>
                <div className="kv-row"><span>策略模式</span><strong>周度收盘选股</strong></div>
                <div className="kv-row"><span>预览扫描</span><strong>{status.data?.scan_running ? "运行中" : "空闲"}</strong></div>
                <div className="kv-row"><span>最近扫描</span><strong>{status.data?.latest_scan_at || "尚未扫描"}</strong></div>
                <div className="kv-row"><span>下次扫描</span><strong>{status.data?.next_scan_at || status.data?.scan_schedule || "--"}</strong></div>
                <div className="kv-row"><span>守护心跳</span><strong>{status.data?.daemon_heartbeat_at || "--"}</strong></div>
                <div className="kv-row"><span>最后日志</span><strong>{status.data?.last_log_time || "--"}</strong></div>
              </div>
            </HudCard>
            <HudCard title="验收检查" icon={<FlaskConical size={18} />} meta={healthMeta(observation.data)}>
              <ObservationPanel data={observation.data} error={observation.error} />
            </HudCard>
          </section>
        );
      case "theater":
      default:
        return null;
    }
  })();

  return (
    <StrategyTheater
      activeSection={activeSection}
      onSectionChange={setActiveSection}
      onLogout={logout}
      clock={clock}
      status={status.data}
      portfolio={portfolioData}
      candidates={candidates.data?.candidates ?? []}
      candidateScan={candidates.data}
      rps={rps.data}
      trades={allTrades}
      equity={equity.data?.points ?? []}
      observation={observation.data}
      reducedMotion={reducedMotion}
    >
      {workspaceContent}
    </StrategyTheater>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state">{text}</div>;
}

// v2 cache-bust
function DateFilter({ dates, selected, onSelect }: { dates: string[]; selected: string | null; onSelect: (d: string | null) => void }) {
  if (!dates.length) return null;
  const fmt = (d: string) => `${d.slice(4, 6)}/${d.slice(6, 8)}`;
  return (
    <div className="date-filter">
      <select
        className="date-select"
        value={selected ?? ""}
        onChange={(e) => onSelect(e.target.value || null)}
      >
        <option value="">全部</option>
        {dates.map((d) => (
          <option key={d} value={d}>{fmt(d)}</option>
        ))}
      </select>
    </div>
  );
}

function CandidatePanel({
  data,
  triggering,
  message,
  onTrigger
}: {
  data: CandidatesResponse | null;
  triggering: boolean;
  message: string | null;
  onTrigger: () => Promise<void>;
}) {
  const status = data?.status ?? "never_run";
  const running = Boolean(data?.scan_running || triggering);
  const filters = [
    ...Object.entries(data?.prefilter_counts ?? {}).map(([key, count]) => ({
      key: `prefilter:${key}`,
      label: data?.prefilter_labels?.[key] || key,
      count
    })),
    ...Object.entries(data?.filter_counts ?? {}).map(([key, count]) => ({
      key: `factor:${key}`,
      label: data?.filter_labels?.[key] || key,
      count
    }))
  ]
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count)
    .slice(0, 5);
  const summary = status === "failed"
    ? `扫描失败：${data?.error || "未知错误"}`
    : status === "completed"
      ? `主板 ${data?.universe?.mainboard_count ?? 0} · 粗筛 ${data?.universe?.rough_candidate_count ?? 0} · 完整历史 ${data?.input_count ?? 0} · 合格 ${data?.eligible_count ?? 0}`
      : "尚无结构化扫描记录";

  return (
    <div className="list-stack">
      <div className={`alert-line ${status === "failed" ? "negative" : ""}`}>{summary}</div>
      <div className="inline-actions">
        <button className="chip active" type="button" disabled={running} onClick={() => void onTrigger()}>
          {running ? "扫描中..." : "安全预览扫描"}
        </button>
        <span className="muted-note">{data?.schedule || "每周收盘扫描"}</span>
      </div>
      {message && <div className="muted-note" role="status">{message}</div>}
      {filters.length > 0 && (
        <div className="metric-stack">
          {filters.map((item) => (
            <div className="kv-row" key={item.key}>
              <span>{item.label}</span>
              <strong>{item.count} 只</strong>
            </div>
          ))}
        </div>
      )}
      <CandidateList
        items={data?.candidates ?? []}
        emptyText={status === "completed" ? "本次扫描没有个股通过全部条件" : "等待首次扫描"}
      />
      {data?.next_scheduled_scan_at && <div className="muted-note">下次计划扫描：{data.next_scheduled_scan_at}</div>}
    </div>
  );
}

function CandidateList({ items, emptyText = "等待扫描" }: { items: Candidate[]; emptyText?: string }) {
  if (!items.length) return <EmptyState text={emptyText} />;
  return (
    <div className="list-stack">
      {items.map((item) => (
        <div className="data-row" key={item.code}>
          <span className="rank">#{item.rank}</span>
          <div className="row-main">
            <strong>{item.name}</strong>
            <span>{item.code} · PB {formatNumber(item.pb, 2)} · 得分 {formatNumber(item.score, 4)}</span>
          </div>
          <div className="row-right">
            <strong>{item.current_price ? formatCurrency(item.current_price, 2) : "--"}</strong>
            <span className={toneClass(item.reversal)}>{formatPercent(item.reversal)}</span>
            {item.selected && <span className="badge buy">拟选</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

function RpsPanel({
  signals,
  industries,
  orders,
  hiddenCount,
  errors
}: {
  signals: RpsSignal[];
  industries: RpsSignal[];
  orders: RpsOrder[];
  hiddenCount: number;
  errors: string[];
}) {
  return (
    <div className="list-stack">
      {errors.slice(0, 2).map((error) => (
        <div className="alert-line negative" key={error}>
          {error}
        </div>
      ))}
      <Subhead text="ETF 入选" />
      {signals.length ? signals.slice(0, 4).map((item) => <SignalRow item={item} key={item.code} />) : <EmptyState text="无 ETF 入选" />}
      <Subhead text="行业强弱" />
      {industries.length ? industries.slice(0, 3).map((item) => <SignalRow item={item} key={item.code} compact />) : <EmptyState text="无行业数据" />}
      <Subhead text="RPS 订单" />
      {orders.length ? orders.slice(0, 4).map((order) => <OrderRow order={order} key={`${order.code}-${order.action}-${order.status}`} />) : <EmptyState text="无有效订单" />}
      {hiddenCount > 0 && <div className="muted-note">{hiddenCount} 笔被风控拒绝已隐藏</div>}
    </div>
  );
}

function Subhead({ text }: { text: string }) {
  return <div className="subhead">{text}</div>;
}

function SignalRow({ item, compact = false }: { item: RpsSignal; compact?: boolean }) {
  return (
    <div className="data-row">
      <span className="rank">{item.rank ?? ""}</span>
      <div className="row-main">
        <strong>{item.name || item.code}</strong>
        <span>{compact ? `RPS ${formatNumber(item.rps, 0)} · 观察` : `${item.code} · RPS ${formatNumber(item.rps, 0)} · 量 ${formatNumber(item.avg_volume, 0)}`}</span>
      </div>
      <div className="row-right">
        {!compact && <strong>{formatCurrency(item.price, 3)}</strong>}
        <span className={toneClass(item.momentum)}>{formatPercent(item.momentum)}</span>
      </div>
    </div>
  );
}

function OrderRow({ order }: { order: RpsOrder }) {
  const buy = order.action === "buy";
  return (
    <div className="data-row">
      <span className={buy ? "badge buy" : "badge sell"}>{buy ? "买" : "卖"}</span>
      <div className="row-main">
        <strong>{order.name || order.code}</strong>
        <span>{order.status || "--"} · {order.reason || order.message || ""}</span>
      </div>
      <div className="row-right">
        <strong>{order.shares ?? 0} 份</strong>
        <span>{formatCurrency(order.price, 3)}</span>
      </div>
    </div>
  );
}

function PositionList({ items, cash, totalValue }: { items: Array<{ code: string; name: string; shares: number; avg_cost: number; current_price: number; profit_pct: number; value: number }>; cash: number; totalValue: number }) {
  if (!items.length) return <EmptyState text="暂无持仓" />;
  const totalPositionValue = items.reduce((sum, item) => sum + (item.value || 0), 0);
  const cashRatio = totalValue > 0 ? (cash / totalValue * 100).toFixed(1) : '0';
  const positionRatio = totalValue > 0 ? (totalPositionValue / totalValue * 100).toFixed(1) : '0';
  return (
    <div className="position-detail">
      <div className="position-summary">
        <div className="position-summary__item">
          <span className="position-summary__label">总资产</span>
          <strong>{formatCurrency(totalValue)}</strong>
        </div>
        <div className="position-summary__item">
          <span className="position-summary__label">现金</span>
          <strong>{formatCurrency(cash)}</strong>
          <span className="muted">({cashRatio}%)</span>
        </div>
        <div className="position-summary__item">
          <span className="position-summary__label">持仓</span>
          <strong>{formatCurrency(totalPositionValue)}</strong>
          <span className="muted">({positionRatio}%)</span>
        </div>
        <div className="position-summary__item">
          <span className="position-summary__label">持仓数</span>
          <strong>{items.length} 只</strong>
        </div>
      </div>
      <div className="list-stack compact-list">
        {items.map((item) => {
          const weight = totalValue > 0 ? ((item.value || 0) / totalValue * 100).toFixed(1) : '0';
          return (
            <div className="data-row" key={item.code}>
              <div className="row-main">
                <strong>{item.name || item.code}</strong>
                <span>{item.shares} 股 · 成本 {formatCurrency(item.avg_cost, 3)}</span>
              </div>
              <div className="row-right">
                <strong>{formatCurrency(item.current_price, 3)}</strong>
                <span className={toneClass(item.profit_pct)}>{formatPercent(item.profit_pct)}</span>
                <span className="muted" style={{ fontSize: '10px' }}>{weight}%</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ProfitRanking({ ranking }: { ranking: ProfitRankItem[] }) {
  if (!ranking.length) {
    return <EmptyState text="暂无已平仓记录，完成交易后显示战绩排名。" />;
  }

  const maxAbsRoi = Math.max(...ranking.map((item) => Math.abs(item.roi)), 0.001);
  const LABELS = ["夯爆了", "夯", "人上人", "npc", "拉", "拉爆了"];
  const assignLabel = (index: number, total: number) => {
    if (total <= 1) return LABELS[0];
    const level = Math.round(index * (LABELS.length - 1) / (total - 1));
    return LABELS[level];
  };

  return (
    <div className="profit-ranking" aria-label="历史战绩排名">
      <div className="profit-ranking__legend">
        <span>按累计收益率排序（已平仓）</span>
        <span>盈利红 · 亏损绿</span>
      </div>
      {ranking.map((item, index) => {
        const positive = item.roi >= 0;
        const width = Math.max(6, Math.abs(item.roi) / maxAbsRoi * 100);
        return (
          <div className={`profit-rank-row rank-level-${index}`} key={item.code}>
            <span className="profit-rank-row__label">{assignLabel(index, ranking.length)}</span>
            <div className="profit-rank-row__stock">
              <strong>{item.name || item.code}</strong>
              <span>{item.code} · {item.shares_traded} 股</span>
              <span className="profit-rank-row__bar" aria-hidden="true">
                <i className={positive ? "is-profit" : "is-loss"} style={{ width: `${width}%` }} />
              </span>
            </div>
            <div className={`profit-rank-row__value ${positive ? "is-profit" : "is-loss"}`}>
              <strong>{positive ? "+" : ""}{formatNumber(item.net_profit, 2)}</strong>
              <span>{formatPercent(item.roi)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function TradeList({ trades }: { trades: Trade[] }) {
  if (!trades.length) return <EmptyState text="暂无交易" />;
  return (
    <div className="list-stack">
      {trades.slice(0, 100).map((trade, index) => {
        const rejected = trade.status === "rejected";
        const buy = (trade.action ?? trade.direction) === "buy";
        return (
          <div className={`data-row ${rejected ? "is-muted" : ""}`} key={`${trade.date}-${trade.time}-${trade.code}-${index}`}>
            <span className="row-time">{trade.date} {trade.time}</span>
            <span className={rejected ? "badge reject" : buy ? "badge buy" : "badge sell"}>{rejected ? "拒" : buy ? "买" : "卖"}</span>
            <div className="row-main">
              <strong>{trade.name || trade.code}</strong>
              <span>{trade.shares ?? 0} 股{rejected ? " · " + (trade.reject_reason || trade.reason || "风控拒绝") : ""}</span>
            </div>
            <div className="row-right">
              <strong>{formatCurrency(trade.price ?? trade.actual_price, 3)}</strong>
              <span>{formatCurrency(trade.amount, 0)}</span>
              {!rejected && trade.strategy && <span className="muted" style={{ fontSize: '10px' }}>{trade.strategy}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function LogPanel({ lines, autoScroll }: { lines: string[]; autoScroll: boolean }) {
  const classForLine = (line: string) => {
    if (line.includes("[ERROR]") || line.includes("失败")) return "negative";
    if (line.includes("[WARNING]") || line.includes("警告")) return "warn";
    if (line.includes("买入") || line.includes("[买入]")) return "positive";
    if (line.includes("卖出") || line.includes("[卖出]") || line.includes("止损")) return "negative";
    if (line.includes("盯盘线") || line.includes("扫描线")) return "accent";
    return "neutral";
  };

  return (
    <div className="log-panel" role="log" aria-live="polite" data-autoscroll={autoScroll}>
      {lines.length ? (
        lines.map((line, index) => (
          <div className={`log-line tone-${classForLine(line)}`} key={`${index}-${line.slice(0, 24)}`}>
            {line}
          </div>
        ))
      ) : (
        <EmptyState text="暂无日志" />
      )}
    </div>
  );
}

function ObservationPanel({ data, error }: { data: ObservationResponse | null; error: string | null }) {
  if (error) return <EmptyState text={error} />;
  const obs = data;
  const healthOk = obs?.health?.ok !== false && !(obs?.health?.failures?.length);
  const acceptance = obs?.acceptance;
  const progress = Math.min(100, ((acceptance?.snapshot_days ?? 0) / (acceptance?.required_snapshot_days || 20)) * 100);
  return (
    <div className="metric-stack">
      <div className="kv-row"><span>健康检查</span><strong className={healthOk ? "tone-positive" : "tone-negative"}>{healthOk ? "通过" : "有失败项"}</strong></div>
      <div className="kv-row"><span>QMT 验收</span><strong className={acceptance?.ready_for_qmt_dry_run ? "tone-positive" : "tone-negative"}>{acceptance?.ready_for_qmt_dry_run ? "就绪" : "未就绪"}</strong></div>
      <div className="progress-label">观察期进度 {acceptance?.snapshot_days ?? 0}/{acceptance?.required_snapshot_days ?? 20}</div>
      <div className="meter large"><span className="meter__fill tone-accent" style={{ width: `${progress}%` }} /></div>
      {obs?.review?.total_return !== undefined && (
        <>
          <Subhead text={`近 ${obs.review.days ?? 30} 日复盘`} />
          <div className="kv-row"><span>总收益</span><strong className={toneClass(obs.review.total_return)}>{formatPercent(obs.review.total_return)}</strong></div>
          <div className="kv-row"><span>最大回撤</span><strong>{formatPercent(obs.review.max_drawdown)}</strong></div>
          <div className="kv-row"><span>胜率</span><strong>{((obs.review.win_rate ?? 0) * 100).toFixed(0)}%</strong></div>
          <div className="kv-row"><span>交易笔数</span><strong>{obs.review.trade_count ?? 0}</strong></div>
        </>
      )}
      {obs?.health?.failures?.slice(0, 3).map((failure) => <div className="alert-line negative" key={failure}>{failure}</div>)}
      {obs?.acceptance?.failures?.slice(0, 2).map((failure) => <div className="alert-line warn" key={failure}>{failure}</div>)}
    </div>
  );
}

function BacktestTable({ series }: { series: BacktestSeries[] }) {
  const metrics = [
    ["总收益", "total_return", true],
    ["年化", "annual_return", true],
    ["最大回撤", "max_drawdown", true],
    ["夏普", "sharpe_ratio", false],
    ["胜率", "win_rate", true],
    ["交易数", "total_trades", false]
  ] as const;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>策略</th>
            {metrics.map(([label]) => <th key={label}>{label}</th>)}
          </tr>
        </thead>
        <tbody>
          {series.map((item) => (
            <tr key={item.name}>
              <td>{item.name}</td>
              {metrics.map(([label, key, percent]) => {
                const value = item.metrics?.[key];
                return (
                  <td key={label} className={key === "total_return" || key === "annual_return" || key === "sharpe_ratio" ? toneClass(value) : ""}>
                    {percent ? formatPercent(value) : value == null ? "--" : formatNumber(value, key === "total_trades" ? 0 : 2)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="muted-note">注：仅展示 robust_v2 历史股票池滚动样本外结果；缺少版本化数据时不会用当前股票池回填。</div>
    </div>
  );
}

function healthMeta(data: unknown) {
  const obs = data as { health?: { ok?: boolean; failures?: string[] } } | null;
  const ok = obs?.health?.ok !== false && !(obs?.health?.failures?.length);
  return <span className={ok ? "tone-positive" : "tone-negative"}>{ok ? "健康" : "异常"}</span>;
}

function rpsStatus(data: unknown) {
  const rps = data as { status?: string; completed?: boolean } | null;
  if (rps?.status === "ok" && rps.completed) return <span className="tone-positive">已完成</span>;
  if (rps?.status === "error") return <span className="tone-negative">异常</span>;
  return rps?.status ?? "--";
}

function backtestMeta(data: unknown) {
  const bt = data as { generating?: boolean; status?: string; window?: string; generated_at?: string; stale?: boolean } | null;
  if (bt?.generating) return "生成中";
  if (!bt) return "--";
  return `${bt.window ?? bt.status ?? ""}${bt.generated_at ? ` · ${bt.generated_at}` : ""}${bt.stale ? " · 后台刷新中" : ""}`;
}

export function App() {
  const [authenticated, setAuthenticated] = useState(() => window.location.pathname !== "/quantify/login");
  return authenticated ? <DashboardView onLogout={() => setAuthenticated(false)} /> : <LoginView onLogin={() => setAuthenticated(true)} />;
}
