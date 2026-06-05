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

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });

  if (!response.ok) {
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
    return requestJson<LoginResponse>("/api/login", {
      method: "POST",
      body: JSON.stringify({ password })
    });
  },
  logout(): Promise<LoginResponse> {
    return requestJson<LoginResponse>("/api/logout", { method: "POST", body: "{}" });
  },
  changePassword(oldPassword: string, newPassword: string): Promise<LoginResponse> {
    return requestJson<LoginResponse>("/api/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
    });
  },
  status(): Promise<StatusResponse> {
    return requestJson<StatusResponse>("/api/status");
  },
  portfolio(): Promise<PortfolioResponse> {
    return requestJson<PortfolioResponse>("/api/portfolio");
  },
  trades(): Promise<TradesResponse> {
    return requestJson<TradesResponse>("/api/trades");
  },
  candidates(): Promise<CandidatesResponse> {
    return requestJson<CandidatesResponse>("/api/candidates");
  },
  rps(): Promise<RpsResponse> {
    return requestJson<RpsResponse>("/api/rps");
  },
  equity(): Promise<EquityResponse> {
    return requestJson<EquityResponse>("/api/equity");
  },
  logs(lines: number): Promise<LogsResponse> {
    return requestJson<LogsResponse>(`/api/logs?lines=${lines}&file=live_today`);
  },
  observation(): Promise<ObservationResponse> {
    return requestJson<ObservationResponse>("/api/observation");
  },
  backtest(): Promise<BacktestResponse> {
    return requestJson<BacktestResponse>("/api/backtest");
  }
};
