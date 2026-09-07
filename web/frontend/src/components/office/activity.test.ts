import { describe, expect, it } from "vitest";
import { activityAt, pointOnPath } from "./activity";

describe("办公室人物生活", () => {
  it("两人同时碰面，其他岗位继续工作，循环后回到日常", () => {
    expect(activityAt("factors", 3).kind).toBe("work");
    expect(activityAt("system", 3).kind).toBe("walk-out");
    expect(activityAt("factors", 20).kind).toBe("chat");
    expect(activityAt("system", 20).kind).toBe("chat");
    expect(activityAt("execution", 20).kind).toBe("work");
    expect(activityAt("system", 71).kind).toBe("work");
    expect(activityAt("system", 75)).toEqual(activityAt("system", 3));
  });
  it("按路程经过走廊，不对起点终点直接穿墙插值", () => {
    const route = [[0, 0, 0], [0, 0, 2], [6, 0, 2]] as const;
    expect(pointOnPath(route, 0).position).toEqual([0, 0, 0]);
    expect(pointOnPath(route, 0.25).position).toEqual([0, 0, 2]);
    expect(pointOnPath(route, 0.5).position).toEqual([2, 0, 2]);
    expect(pointOnPath(route, 2).position).toEqual([6, 0, 2]);
    expect(pointOnPath(route, -1).position).toEqual([0, 0, 0]);
  });
});
