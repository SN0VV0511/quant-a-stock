import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./usePolling";

describe("usePolling", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });
  it("保留成功数据并记录更新时间", async () => {
    const loader = vi.fn(async () => ({ ok: true }));
    const { result } = renderHook(() => usePolling(loader, 60_000));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.data).toEqual({ ok: true });
    expect(result.current.error).toBeNull();
    expect(result.current.updatedAt).toEqual(expect.any(Number));
  });

  it("请求失败时暴露错误信息", async () => {
    const loader = vi.fn(async () => {
      throw new Error("网络异常");
    });
    const { result } = renderHook(() => usePolling(loader, 60_000));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.data).toBeNull();
    expect(result.current.error).toBe("网络异常");
  });

  it("慢请求不重叠，后台暂停，回到前台立即更新", async () => {
    vi.useFakeTimers();
    let hidden = false;
    vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
    let finish: (value: number) => void = () => undefined;
    const loader = vi.fn(() => new Promise<number>((resolve) => { finish = resolve; }));
    renderHook(() => usePolling(loader, 1000));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(loader).toHaveBeenCalledTimes(1);
    await act(async () => { finish(1); });
    hidden = true;
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(loader).toHaveBeenCalledTimes(1);
    hidden = false;
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("切换查询后忽略旧请求晚到的结果", async () => {
    let finishOld: (value: string) => void = () => undefined;
    const oldLoader = () => new Promise<string>((resolve) => { finishOld = resolve; });
    const nextLoader = async () => "new";
    const { result, rerender } = renderHook(({ loader }) => usePolling(loader, 60_000), { initialProps: { loader: oldLoader } });
    await act(async () => undefined);
    rerender({ loader: nextLoader });
    await waitFor(() => expect(result.current.data).toBe("new"));
    await act(async () => { finishOld("old"); });
    expect(result.current.data).toBe("new");
  });

  it("停用再启用复用慢请求，卸载后的 refresh 不发请求", async () => {
    let finish: (value: number) => void = () => undefined;
    const loader = vi.fn(() => new Promise<number>((resolve) => { finish = resolve; }));
    const { result, rerender, unmount } = renderHook(({ enabled }) => usePolling(loader, 60_000, enabled), { initialProps: { enabled: true } });
    const refresh = result.current.refresh;
    await act(async () => undefined);
    expect(loader).toHaveBeenCalledTimes(1);
    rerender({ enabled: false });
    await act(async () => { await refresh(); });
    rerender({ enabled: true });
    await act(async () => undefined);
    expect(loader).toHaveBeenCalledTimes(1);
    await act(async () => { finish(42); });
    expect(result.current.data).toBe(42);
    unmount();
    await refresh();
    expect(loader).toHaveBeenCalledTimes(1);
  });
});
