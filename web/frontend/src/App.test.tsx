import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

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
    fireEvent.click(screen.getByRole("button", { name: "进入终端" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "策略运行剧场" })).toBeInTheDocument());
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

    await waitFor(() => expect(screen.getByRole("heading", { name: "策略运行剧场" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "阶段 1：行情快照" })).toBeInTheDocument();
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
});
