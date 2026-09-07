import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("./components/office/OfficeScene", () => ({
  OfficeScene: ({ selected, paused, quality, view }: { selected: string; paused: boolean; quality: string; view: string }) =>
    <div data-testid="office-scene" data-selected={selected} data-paused={String(paused)} data-quality={quality} data-view={view} />
}));

vi.mock("./components/Charts", () => ({
  AllocationChart: () => <div data-testid="allocation-chart" />,
  BacktestChart: () => <div data-testid="backtest-chart" />,
  EquityCharts: () => <div data-testid="equity-chart" />,
  EquitySparkline: () => <div data-testid="equity-sparkline" />,
  RiskGauge: () => <div data-testid="risk-gauge" />
}));

describe("App login", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/quantify/login");
  });

  it("登录表单提交成功后进入仪表盘", async () => {
    window.history.replaceState({}, "", "/quantify/login");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/quantify/api/login") {
          return new Response(JSON.stringify({ success: true }), { status: 200 });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      })
    );

    render(<App />);
    fireEvent.change(screen.getByLabelText("访问密码"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "进入办公室" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: /市场很快/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /组合持仓/ }));
    await waitFor(() => expect(screen.getByText("持仓战绩榜")).toBeInTheDocument());
  });

  it("接口返回空对象时仪表盘保持可用", async () => {
    window.history.replaceState({}, "", "/quantify/");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }))
    );

    render(<App />);

    await waitFor(() => expect(screen.getByRole("heading", { name: /市场很快/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "打开行情看板" }));
    expect(screen.getByText("当前回撤").parentElement).toHaveTextContent("--");
    expect(screen.getByText("持仓仓位").parentElement).toHaveTextContent("--");
    expect(screen.queryByText("现金占比 100.0%")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /组合持仓/ }));
    expect(screen.getByText("持仓战绩榜")).toBeInTheDocument();
    expect(screen.getByText("暂无已平仓记录，完成交易后显示战绩排名。")).toBeInTheDocument();
  });

  it("新密码不足时给出内联错误", async () => {
    window.history.replaceState({}, "", "/quantify/login");
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    fireEvent.change(await screen.findByLabelText("旧密码"), { target: { value: "old" } });
    fireEvent.change(await screen.findByLabelText("新密码"), { target: { value: "123" } });
    fireEvent.click(await screen.findByRole("button", { name: "更新密码" }));

    expect(await screen.findByText("新密码至少 6 位")).toBeInTheDocument();
  });

  it("展示结构化扫描统计并能启动安全预览", async () => {
    window.history.replaceState({}, "", "/quantify/");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/quantify/api/candidates") {
        return new Response(JSON.stringify({
          candidates: [],
          updated_at: "2026-07-10 15:06:00",
          trade_date: "20260710",
          status: "completed",
          mode: "scheduled",
          scan_running: false,
          input_count: 500,
          eligible_count: 0,
          universe: { mainboard_count: 3000, rough_candidate_count: 500 },
          prefilter_counts: { candidate_limit: 400 },
          prefilter_labels: { candidate_limit: "流动性排名超出 500 只上限" },
          filter_counts: { below_ma120: 320 },
          filter_labels: { below_ma120: "股价低于年线" },
          selected_codes: [],
          next_scheduled_scan_at: "2026-07-17 15:05:00",
          error: "",
          schedule: "每周最后一个交易日 15:05 后"
        }), { status: 200 });
      }
      if (url === "/quantify/api/scan/trigger" && init?.method === "POST") {
        return new Response(JSON.stringify({
          status: "started",
          message: "安全预览扫描已启动，不会生成交易信号或订单"
        }), { status: 202 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /因子看板/ }));

    expect(await screen.findByText(/主板 3000 · 粗筛 500 · 完整历史 500 · 合格 0/)).toBeInTheDocument();
    expect(screen.getByText("股价低于年线")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "安全预览扫描" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/quantify/api/scan/trigger",
      expect.objectContaining({ method: "POST" })
    ));
    expect(await screen.findByText("安全预览扫描已启动，不会生成交易信号或订单")).toBeInTheDocument();
  });

  it("账本回退估值显式标注，未打开的重面板不轮询", async () => {
    window.history.replaceState({}, "", "/quantify/");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/quantify/api/portfolio") {
        return new Response(JSON.stringify({
          total_value: 50000, cash: 49000, positions_value: 1000,
          position_ratio: 0.02, position_count: 1, pnl: 0, pnl_pct: 0,
          updated_at: "2026-09-05 09:30:00", quotes_degraded: true,
          positions: [{ code: "600000", name: "测试持仓", shares: 100, avg_cost: 10, current_price: 10,
            value: 1000, profit: 0, profit_pct: 0, price_source: "ledger" }]
        }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    expect(await screen.findByText(/部分行情未能获取，当前使用账本价格估值/)).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls).not.toContain("/quantify/api/backtest");
    expect(urls).not.toContain("/quantify/api/profit-ranking");
    expect(urls.some((url) => url.includes("/api/logs"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: /组合持仓/ }));
    expect(await screen.findByText("账本估值 · 行情未更新")).toBeInTheDocument();
  });

  it("关闭自动回测时提示手动生成，不承诺后台自动生成", async () => {
    window.history.replaceState({}, "", "/quantify/");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(
      JSON.stringify(String(input) === "/quantify/api/backtest" ? { auto_generate: false, series: [] } : {}),
      { status: 200 }
    )));
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /回测分析/ }));
    expect(await screen.findByText("暂无回测结果，自动生成已关闭；请手动生成回测后刷新。")).toBeInTheDocument();
    expect(screen.queryByText("暂无回测结果，系统会在后台自动生成。")).not.toBeInTheDocument();
  });

  it("人物仅打开数据，关闭抽屉还焦且场景保持挂载", async () => {
    window.history.replaceState({}, "", "/quantify/");
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({}), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    const scene = await screen.findByTestId("office-scene");
    const colleague = screen.getByRole("button", { name: /小因 · 策略研究员/ });
    colleague.focus();
    fireEvent.click(colleague);
    expect(screen.getByRole("dialog", { name: /研究笔记/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /研究笔记/ })).toHaveFocus();
    expect(screen.getByTestId("office-scene")).toBe(scene);
    expect(scene).toHaveAttribute("data-selected", "factors");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/scan/trigger"))).toBe(false);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(colleague).toHaveFocus();
    expect(scene).toHaveAttribute("data-selected", "theater");
  });

  it("视角、暂停和低耗控制传入同一个场景，空健康状态保持待确认", async () => {
    window.history.replaceState({}, "", "/quantify/");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })));
    render(<App />);
    const scene = await screen.findByTestId("office-scene");
    fireEvent.click(screen.getByRole("button", { name: "茶水间" }));
    expect(scene).toHaveAttribute("data-view", "lounge");
    expect(screen.queryByRole("heading", { name: /市场很快/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全景" }));
    expect(screen.getByRole("heading", { name: /市场很快/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "暂停生活" }));
    expect(scene).toHaveAttribute("data-paused", "true");
    fireEvent.click(screen.getByRole("button", { name: "低耗模式" }));
    expect(scene).toHaveAttribute("data-quality", "low");
    fireEvent.click(screen.getByRole("button", { name: /小盾 · 系统巡检员/ }));
    expect(screen.getByText("数据摘要：健康待确认")).toBeInTheDocument();
    expect(screen.queryByText("健康检查通过")).not.toBeInTheDocument();
  });

});
