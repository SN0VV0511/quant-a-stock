import { Component, type ErrorInfo, type ReactNode } from "react";

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  error: Error | null;
}

/**
 * 捕获顶层渲染异常，避免接口数据异常时整个页面只剩黑屏。
 */
export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("仪表盘渲染失败", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <main className="auth-shell">
        <section className="auth-panel" role="alert">
          <p className="eyebrow">DASHBOARD ERROR</p>
          <h1>页面加载失败</h1>
          <p className="tone-negative">{this.state.error.message}</p>
          <button className="primary-action" type="button" onClick={() => window.location.reload()}>
            重新加载
          </button>
        </section>
      </main>
    );
  }
}
