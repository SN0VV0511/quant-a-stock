import { describe, expect, it } from "vitest";
import { buildProfitRanking, PROFIT_RANK_LABELS } from "./profitRanking";
import type { Position } from "../types";

function position(code: string, profit: number): Position {
  return {
    code,
    name: code,
    shares: 100,
    avg_cost: 10,
    current_price: 10 + profit / 100,
    value: 1_000 + profit,
    profit,
    profit_pct: profit / 1_000
  };
}

describe("buildProfitRanking", () => {
  it("超过六只持仓时保留前三和后三，并覆盖全部六档标签", () => {
    const ranked = buildProfitRanking([
      position("A", 900),
      position("B", 700),
      position("C", 500),
      position("D", 300),
      position("E", 100),
      position("F", -100),
      position("G", -300),
      position("H", -800)
    ]);

    expect(ranked.map((item) => item.code)).toEqual(["A", "B", "C", "F", "G", "H"]);
    expect(ranked.map((item) => item.rankLabel)).toEqual(PROFIT_RANK_LABELS);
  });

  it("少量持仓仍将最好与最差映射到首尾标签", () => {
    const ranked = buildProfitRanking([position("WIN", 100), position("LOSS", -50)]);
    expect(ranked[0].rankLabel).toBe("夯爆了");
    expect(ranked[1].rankLabel).toBe("拉爆了");
  });
});
