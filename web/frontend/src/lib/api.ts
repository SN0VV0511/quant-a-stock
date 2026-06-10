import type {
  BacktestResponse,
  CandidatesResponse,
  EquityResponse,
  LoginResponse,
  LogsResponse,
  ObservationResponse,
  PortfolioResponse,
  RpsResponse,
  StatusResponse,
  TradesResponse
} from "../types";

const BASE = "/quantify";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function requestJson<T>(
  path: string,
  init?: RequestInit,
  redirectOnUnauthorized = true
): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });

  if (!response.ok) {
    if (response.status === 401 && redirectOnUnauthorized) {
      // cookie 过期，跳转到登录页
      window.location.href = "/quantify/login";
      throw new ApiError("未登录，正在跳转...", 401);
    }
    let message = `请求失败: HTTP ${response.status}`;
    try {
      const data = (await response.json()) as { error?: string };
      message = data.error ?? message;
    } catch {
      // 服务端可能返回 HTML 登录页，此处保留 HTTP 状态信息。
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  login(password: string): Promise<LoginResponse> {
    return requestJson<LoginResponse>(`${BASE}/api/login`, {
      method: "POST",
      body: JSON.stringify({ password })
    }, false);
  },
  logout(): Promise<LoginResponse> {
    return requestJson<LoginResponse>(`${BASE}/api/logout`, { method: "POST", body: "{}" });
  },
  changePassword(oldPassword: string, newPassword: string): Promise<LoginResponse> {
    return requestJson<LoginResponse>(`${BASE}/api/change-password`, {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
    }, false);
  },
  status(): Promise<StatusResponse> {
    return requestJson<StatusResponse>(`${BASE}/api/status`);
  },
  portfolio(): Promise<PortfolioResponse> {
    return requestJson<PortfolioResponse>(`${BASE}/api/portfolio`);
  },
  trades(date?: string): Promise<TradesResponse> {
    const qs = date ? `?date=${date}` : "";
    return requestJson<TradesResponse>(`${BASE}/api/trades${qs}`);
  },
  candidates(): Promise<CandidatesResponse> {
    return requestJson<CandidatesResponse>(`${BASE}/api/candidates`);
  },
  rps(): Promise<RpsResponse> {
    return requestJson<RpsResponse>(`${BASE}/api/rps`);
  },
  equity(): Promise<EquityResponse> {
    return requestJson<EquityResponse>(`${BASE}/api/equity`);
  },
  logs(lines: number): Promise<LogsResponse> {
    return requestJson<LogsResponse>(`${BASE}/api/logs?lines=${lines}&file=live_today`);
  },
  observation(): Promise<ObservationResponse> {
    return requestJson<ObservationResponse>(`${BASE}/api/observation`);
  },
  backtest(): Promise<BacktestResponse> {
    return requestJson<BacktestResponse>(`${BASE}/api/backtest`);
  }
};
