import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { usePolling } from "./usePolling";

describe("usePolling", () => {
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
});
