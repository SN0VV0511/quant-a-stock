import { useEffect, useRef } from "react";
import {
  ArcElement,
  Chart,
  DoughnutController,
  Filler,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  CategoryScale,
  Tooltip
} from "chart.js";
import type { BacktestSeries, EquityPoint, Position } from "../types";
import { formatNumber, shortTimeLabel } from "../lib/format";

Chart.register(
  ArcElement,
  CategoryScale,
  DoughnutController,
  Filler,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip
);

const grid = {
  color: "rgba(71, 85, 105, 0.25)",
  drawBorder: false
};

function chartAnimation(reducedMotion: boolean): false | { duration: number; easing: "easeOutQuart" } {
  return reducedMotion ? false : { duration: 280, easing: "easeOutQuart" as const };
}

interface EquityChartsProps {
  points: EquityPoint[];
  reducedMotion: boolean;
}

export function EquityCharts({ points, reducedMotion }: EquityChartsProps) {
  const equityRef = useRef<HTMLCanvasElement | null>(null);
  const ddRef = useRef<HTMLCanvasElement | null>(null);
  const equityChart = useRef<Chart | null>(null);
  const ddChart = useRef<Chart | null>(null);

  useEffect(() => {
    if (!equityRef.current || !ddRef.current || !points.length) {
      return undefined;
    }

    const labels = points.map((point) => shortTimeLabel(point.t));
    const values = points.map((point) => point.value);
    const drawdowns = points.map((point) => -((point.drawdown ?? 0) * 100));

    const sharedOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: chartAnimation(reducedMotion),
      interaction: { intersect: false, mode: "index" as const },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "rgba(2, 6, 23, 0.92)",
          borderColor: "rgba(34, 197, 94, 0.28)",
          borderWidth: 1,
          titleColor: "#f8fafc",
          bodyColor: "#cbd5e1"
        }
      },
      scales: {
        x: { grid, ticks: { color: "#64748b", maxTicksLimit: 8 } },
        y: { grid, ticks: { color: "#94a3b8", maxTicksLimit: 5 } }
      }
    };

    if (!equityChart.current) {
      equityChart.current = new Chart(equityRef.current, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              data: values,
              borderColor: "#38bdf8",
              backgroundColor: "rgba(56, 189, 248, 0.12)",
              borderWidth: 2,
              pointRadius: 0,
              fill: true,
              tension: 0.32
            }
          ]
        },
        options: sharedOptions
      });
    } else {
      equityChart.current.data.labels = labels;
      equityChart.current.data.datasets[0].data = values;
      equityChart.current.update();
    }

    if (!ddChart.current) {
      ddChart.current = new Chart(ddRef.current, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              data: drawdowns,
              borderColor: "#fb7185",
              backgroundColor: "rgba(251, 113, 133, 0.14)",
              borderWidth: 1.6,
              pointRadius: 0,
              fill: true,
              tension: 0.28
            }
          ]
        },
        options: sharedOptions
      });
    } else {
      ddChart.current.data.labels = labels;
      ddChart.current.data.datasets[0].data = drawdowns;
      ddChart.current.update();
    }

    return undefined;
  }, [points, reducedMotion]);

  useEffect(() => {
    return () => {
      equityChart.current?.destroy();
      ddChart.current?.destroy();
      equityChart.current = null;
      ddChart.current = null;
    };
  }, []);

  return (
    <div className="chart-stack">
      <div className="chart-box chart-box--equity">
        <canvas ref={equityRef} aria-label="账户净值曲线" />
      </div>
      <div className="chart-box chart-box--drawdown">
        <canvas ref={ddRef} aria-label="账户回撤曲线" />
      </div>
    </div>
  );
}

interface AllocationChartProps {
  cash: number;
  positions: Position[];
  reducedMotion: boolean;
}

export function AllocationChart({ cash, positions, reducedMotion }: AllocationChartProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<Chart | null>(null);

  useEffect(() => {
    if (!canvasRef.current) {
      return undefined;
    }

    const hasPositions = positions.length > 0;
    const labels = ["现金", ...positions.map((position) => position.name || position.code)];
    const values = [hasPositions ? cash : Math.max(cash, 1), ...positions.map((position) => position.value)];
    const colors = ["#334155", "#22c55e", "#38bdf8", "#f59e0b", "#a78bfa", "#fb7185", "#14b8a6"];

    if (!chartRef.current) {
      chartRef.current = new Chart(canvasRef.current, {
        type: "doughnut",
        data: {
          labels,
          datasets: [
            {
              data: values,
              backgroundColor: values.map((_, index) => colors[index % colors.length]),
              borderColor: "#020617",
              borderWidth: 2
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "66%",
          animation: chartAnimation(reducedMotion),
          plugins: {
            legend: {
              display: hasPositions,
              position: "right",
              labels: { color: "#94a3b8", boxWidth: 8, boxHeight: 8 }
            },
            tooltip: {
              callbacks: {
                label: (ctx) => `${ctx.label}: ¥${formatNumber(Number(ctx.raw), 0)}`
              }
            }
          }
        }
      });
    } else {
      chartRef.current.data.labels = labels;
      chartRef.current.data.datasets[0].data = values;
      chartRef.current.data.datasets[0].backgroundColor = values.map((_, index) => colors[index % colors.length]);
      chartRef.current.options.plugins = {
        ...chartRef.current.options.plugins,
        legend: {
          display: hasPositions,
          position: "right",
          labels: { color: "#94a3b8", boxWidth: 8, boxHeight: 8 }
        }
      };
      chartRef.current.update();
    }

    return undefined;
  }, [cash, positions, reducedMotion]);

  useEffect(() => {
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, []);

  const hasPositions = positions.length > 0;

  return (
    <div className={`chart-box chart-box--allocation ${hasPositions ? "" : "is-empty"}`}>
      <canvas ref={canvasRef} aria-label="持仓分布图" />
      {!hasPositions && <span className="allocation-note">空仓</span>}
    </div>
  );
}

interface EquitySparklineProps {
  points: EquityPoint[];
  reducedMotion: boolean;
}

/**
 * 策略剧场中的紧凑净值曲线，只展示真实账户快照，不补造行情点。
 */
export function EquitySparkline({ points, reducedMotion }: EquitySparklineProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<Chart | null>(null);

  useEffect(() => {
    if (!canvasRef.current || !points.length) {
      return undefined;
    }

    const labels = points.map((point) => shortTimeLabel(point.t));
    const values = points.map((point) => point.value);
    if (!chartRef.current) {
      chartRef.current = new Chart(canvasRef.current, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              data: values,
              borderColor: "#ff4b3e",
              backgroundColor: "rgba(255, 75, 62, 0.08)",
              borderWidth: 1.8,
              pointRadius: points.length === 1 ? 2.5 : 0,
              pointBackgroundColor: "#ff6a5f",
              fill: true,
              tension: 0.34
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: chartAnimation(reducedMotion),
          interaction: { intersect: false, mode: "index" },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "rgba(5, 5, 6, 0.96)",
              borderColor: "rgba(255, 75, 62, 0.45)",
              borderWidth: 1,
              titleColor: "#f5f5f4",
              bodyColor: "#b8b6b2",
              callbacks: {
                label: (context) => `净值：¥${formatNumber(Number(context.raw), 0)}`
              }
            }
          },
          scales: {
            x: { display: false },
            y: { display: false }
          }
        }
      });
    } else {
      chartRef.current.data.labels = labels;
      chartRef.current.data.datasets[0].data = values;
      const dataset = chartRef.current.data.datasets[0] as unknown as { pointRadius: number };
      dataset.pointRadius = points.length === 1 ? 2.5 : 0;
      chartRef.current.update();
    }

    return undefined;
  }, [points, reducedMotion]);

  useEffect(() => {
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, []);

  if (!points.length) {
    return <div className="equity-sparkline is-empty">等待净值快照</div>;
  }
  return (
    <div className="equity-sparkline">
      <canvas ref={canvasRef} aria-label="账户净值迷你曲线" />
    </div>
  );
}

interface RiskGaugeProps {
  score: number;
  reducedMotion: boolean;
}

/**
 * 半圆风险仪表：分数越高代表风险预算越充足。
 */
export function RiskGauge({ score, reducedMotion }: RiskGaugeProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const safeScore = Math.max(0, Math.min(10, score));
  const color = safeScore >= 7 ? "#37c978" : safeScore >= 4 ? "#f2a93b" : "#ff4b3e";

  useEffect(() => {
    if (!canvasRef.current) {
      return undefined;
    }
    const values = [safeScore, 10 - safeScore];
    if (!chartRef.current) {
      chartRef.current = new Chart(canvasRef.current, {
        type: "doughnut",
        data: {
          datasets: [
            {
              data: values,
              backgroundColor: [color, "rgba(255, 255, 255, 0.1)"],
              borderWidth: 0,
              borderRadius: 8
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: chartAnimation(reducedMotion),
          rotation: -90,
          circumference: 180,
          cutout: "76%",
          plugins: {
            legend: { display: false },
            tooltip: { enabled: false }
          }
        }
      });
    } else {
      chartRef.current.data.datasets[0].data = values;
      chartRef.current.data.datasets[0].backgroundColor = [color, "rgba(255, 255, 255, 0.1)"];
      chartRef.current.update();
    }

    return undefined;
  }, [color, reducedMotion, safeScore]);

  useEffect(() => {
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, []);

  return (
    <div className="risk-gauge">
      <canvas ref={canvasRef} aria-label={`风险评分 ${safeScore.toFixed(1)} 分`} />
      <div className="risk-gauge__value" aria-hidden="true">
        <strong>{safeScore.toFixed(1)}</strong>
        <span>/ 10</span>
        <small>风险评分</small>
      </div>
    </div>
  );
}

interface BacktestChartProps {
  series: BacktestSeries[];
  reducedMotion: boolean;
}

export function BacktestChart({ series, reducedMotion }: BacktestChartProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<Chart | null>(null);

  useEffect(() => {
    if (!canvasRef.current || !series.length) {
      return undefined;
    }

    const colors = ["#38bdf8", "#22c55e", "#f59e0b", "#a78bfa"];
    const labels = series[0].equity.map((point) => String(point.date).slice(4));
    const datasets = series.map((item, index) => {
      const base = item.equity[0]?.value || 1;
      return {
        label: item.name,
        data: item.equity.map((point) => (point.value / base - 1) * 100),
        borderColor: colors[index % colors.length],
        backgroundColor: "transparent",
        borderWidth: 1.8,
        pointRadius: 0,
        tension: 0.28
      };
    });

    if (!chartRef.current) {
      chartRef.current = new Chart(canvasRef.current, {
        type: "line",
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: chartAnimation(reducedMotion),
          interaction: { intersect: false, mode: "index" },
          plugins: {
            legend: {
              position: "top",
              labels: { color: "#cbd5e1", boxWidth: 10, boxHeight: 10 }
            },
            tooltip: {
              callbacks: {
                label: (ctx) => `${ctx.dataset.label}: ${Number(ctx.raw).toFixed(1)}%`
              }
            }
          },
          scales: {
            x: { grid, ticks: { color: "#64748b", maxTicksLimit: 8 } },
            y: {
              grid,
              ticks: {
                color: "#94a3b8",
                callback: (value) => `${value}%`
              }
            }
          }
        }
      });
    } else {
      chartRef.current.data.labels = labels;
      chartRef.current.data.datasets = datasets;
      chartRef.current.update();
    }

    return undefined;
  }, [reducedMotion, series]);

  useEffect(() => {
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, []);

  return (
    <div className="chart-box chart-box--backtest">
      <canvas ref={canvasRef} aria-label="回测净值对比曲线" />
    </div>
  );
}
