import { describe, expect, it } from "vitest";
import { formatCurrency, formatNumber, formatPercent, shortTimeLabel, toneByValue } from "./format";

describe("format helpers", () => {
  it("格式化金额、数字和百分比", () => {
    expect(formatCurrency(12345.67)).toBe("¥12,346");
    expect(formatNumber(12.3456, 3)).toBe("12.346");
    expect(formatPercent(0.1234)).toBe("+12.34%");
    expect(formatPercent(-0.05)).toBe("-5.00%");
  });

  it("处理空值和时间标签", () => {
    expect(formatCurrency(undefined)).toBe("--");
    expect(formatNumber(Number.NaN)).toBe("--");
    expect(shortTimeLabel("2026-06-05 09:31:00")).toBe("06-05 09:31");
  });

  it("返回盈亏语义色", () => {
    expect(toneByValue(1)).toBe("positive");
    expect(toneByValue(-1)).toBe("negative");
    expect(toneByValue(0)).toBe("neutral");
  });
});
