import type { Position } from "../types";

export const PROFIT_RANK_LABELS = ["夯爆了", "夯", "人上人", "npc", "拉", "拉爆了"] as const;

export interface ProfitRankItem extends Position {
  rankLabel: (typeof PROFIT_RANK_LABELS)[number];
  rankLevel: number;
}

/**
 * 按持仓浮动盈亏金额生成六档战绩榜。
 *
 * 持仓超过六只时保留前三名和后三名，确保最赚钱与最亏损标的一定可见；
 * 持仓不足六只时将现有标的均匀映射到六档标签的首尾区间。
 */
export function buildProfitRanking(positions: Position[]): ProfitRankItem[] {
  const sorted = positions
    .filter((position) => Number.isFinite(position.profit))
    .slice()
    .sort((left, right) => right.profit - left.profit);

  if (!sorted.length) {
    return [];
  }

  const selected = sorted.length > PROFIT_RANK_LABELS.length
    ? [...sorted.slice(0, 3), ...sorted.slice(-3)]
    : sorted;

  return selected.map((position, index) => {
    const rankLevel = selected.length === 1
      ? 0
      : Math.round(index * (PROFIT_RANK_LABELS.length - 1) / (selected.length - 1));
    return {
      ...position,
      rankLabel: PROFIT_RANK_LABELS[rankLevel],
      rankLevel
    };
  });
}
