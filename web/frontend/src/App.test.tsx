import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("./components/Charts", () => ({
  AllocationChart: () => <div data-testid="allocation-chart" />,
  BacktestChart: () => <div data-testid="backtest-chart" />,
  EquityCharts: () => <div data-testid="equity-chart" />
}));

describe("App login", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/login");
  });

  it("登录表单提交成功后进入仪表盘", async () => {
    window.history.replaceState({}, "", "/login");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/login") {
          return new Response(JSON.stringify({ success: true }), { status: 200 });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      })
    );

    render(<App />);
    fireEvent.change(screen.getByLabelText("访问密码"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "进入终端" }));

    await waitFor(() => expect(screen.getByText("总市值")).toBeInTheDocument());
  });

  it("新密码不足时给出内联错误", async () => {
    window.history.replaceState({}, "", "/login");
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    fireEvent.change(await screen.findByLabelText("旧密码"), { target: { value: "old" } });
    fireEvent.change(await screen.findByLabelText("新密码"), { target: { value: "123" } });
    fireEvent.click(await screen.findByRole("button", { name: "更新密码" }));

    expect(await screen.findByText("新密码至少 6 位")).toBeInTheDocument();
  });
});
