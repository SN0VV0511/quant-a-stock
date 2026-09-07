import type { Group, Mesh, Object3D, Vector3 } from "three";

export type OfficeRole = "market" | "factors" | "portfolio" | "execution" | "system";
export type WorkspaceSection = OfficeRole | "theater" | "backtest";

export const TEAM: ReadonlyArray<{ id: OfficeRole; name: string; title: string; color: string; description: string }> = [
  { id: "market", name: "阿行情", title: "行情观察员", color: "#e3a866", description: "看价格，也看每一次波动的来处。" },
  { id: "factors", name: "小因", title: "策略研究员", color: "#aeb7df", description: "在候选池里，寻找经得起验证的信号。" },
  { id: "portfolio", name: "阿仓", title: "组合管理员", color: "#96c7a9", description: "照看持仓，给现金留一点余地。" },
  { id: "execution", name: "小单", title: "交易执行员", color: "#e29a85", description: "每一笔操作，都按规则留下记录。" },
  { id: "system", name: "小盾", title: "系统巡检员", color: "#81becd", description: "确认数据、心跳和运行状态。" }
];

export interface OfficeMetrics {
  totalValue: number | null;
  cashRatio: number | null;
  candidates: number | null;
  trades: number | null;
  healthy: boolean | null;
  equity: number[];
}

export interface OfficeActor {
  id: OfficeRole;
  group: Group;
  head: Group;
  leftArm: Group;
  rightArm: Group;
  leftLeg: Group;
  rightLeg: Group;
  cup: Group | null;
  home: Vector3;
  homeYaw: number;
}

export interface OfficeModel {
  group: Group;
  actors: OfficeActor[];
  interactives: Object3D[];
  board: Group;
  boardBars: Mesh[];
  coffeePoint: Vector3;
  meetingPoint: Vector3;
}

export interface OfficeSceneProps {
  selected: WorkspaceSection;
  onSelect: (section: WorkspaceSection) => void;
  metrics: OfficeMetrics;
  paused: boolean;
  reducedMotion: boolean;
  quality: "balanced" | "low";
  view: "overview" | "desks" | "lounge";
  resetKey: number;
}
