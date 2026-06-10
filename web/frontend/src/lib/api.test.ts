import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("发送登录请求并解析结果", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ success: true }), { status: 200 }))
    );

    await expect(api.login("secret")).resolves.toEqual({ success: true });
    expect(fetch).toHaveBeenCalledWith(
      "/quantify/api/login",
      expect.objectContaining({
        credentials: "same-origin",
        method: "POST",
        body: JSON.stringify({ password: "secret" })
      })
    );
  });

  it("把服务端错误转换为 ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ error: "密码错误" }), { status: 401 }))
    );

    await expect(api.login("bad")).rejects.toMatchObject({
      name: "ApiError",
      message: "密码错误",
      status: 401
    });
  });
});
