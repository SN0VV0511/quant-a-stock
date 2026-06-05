const currencyFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
  minimumFractionDigits: 0
});

/**
 * 格式化金额，统一处理接口返回的空值和异常值。
 */
export function formatCurrency(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "--";
  }
  return `¥${currencyFormatter.format(Number(value.toFixed(digits)))}`;
}

/**
 * 格式化普通数字，主要用于价格、股数和指标表格。
 */
export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "--";
  }
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

/**
 * 格式化百分比，保留涨跌方向符号。
 */
export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "--";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(digits)}%`;
}

/**
 * 根据盈亏值返回语义色类名，避免在组件里重复判断。
 */
export function toneByValue(value: number | null | undefined): "positive" | "negative" | "neutral" {
  if (value === null || value === undefined || Number.isNaN(value) || value === 0) {
    return "neutral";
  }
  return value > 0 ? "positive" : "negative";
}

/**
 * 后端历史数据可能是日期或时间戳，统一压缩为图表标签。
 */
export function shortTimeLabel(value: string | null | undefined): string {
  if (!value) {
    return "--";
  }
  return String(value).slice(5, 16);
}
