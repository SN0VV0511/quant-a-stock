import type { OfficeRole } from "./types";

type Point = readonly [number, number, number];
export type Activity = { kind: "work" | "walk-out" | "coffee" | "chat" | "walk-home"; progress: number };

/** 角色生活的确定性时间表，与交易任务或市场状态无关。 */
export function activityAt(role: OfficeRole, seconds: number): Activity {
  const time = Math.max(0, seconds) % 72;
  if (role !== "system" && role !== "factors") return { kind: "work", progress: 0 };
  const departure = role === "system" ? 0 : 6;
  const arrival = role === "system" ? 8 : 14;
  if (time < departure || time >= 36) return { kind: "work", progress: 0 };
  if (time < arrival) return { kind: "walk-out", progress: (time - departure) / 8 };
  if (time < 14) return { kind: "coffee", progress: (time - arrival) / 6 };
  if (time < 28) return { kind: "chat", progress: (time - 14) / 14 };
  return { kind: "walk-home", progress: (time - 28) / 8 };
}

/** 按路程而非分段数匀速行走，拐角处也保持在指定走廊内。 */
export function pointOnPath(points: readonly Point[], progress: number): { position: Point; yaw: number } {
  if (points.length < 2) throw new Error("行走路径至少需要两个点");
  const lengths = points.slice(1).map((p, i) => Math.hypot(p[0] - points[i][0], p[2] - points[i][2]));
  let distance = lengths.reduce((sum, length) => sum + length, 0) * Math.max(0, Math.min(1, progress));
  for (let index = 0; index < lengths.length; index += 1) {
    const length = lengths[index];
    if (distance <= length || index === lengths.length - 1) {
      const from = points[index];
      const to = points[index + 1];
      const fraction = length > 0 ? distance / length : 0;
      return {
        position: [from[0] + (to[0] - from[0]) * fraction, from[1], from[2] + (to[2] - from[2]) * fraction],
        yaw: Math.atan2(to[0] - from[0], to[2] - from[2])
      };
    }
    distance -= length;
  }
  return { position: points[0], yaw: 0 };
}
