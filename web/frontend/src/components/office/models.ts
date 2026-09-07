import {
  BoxGeometry, CapsuleGeometry, CylinderGeometry, Group, Mesh, MeshStandardMaterial,
  SphereGeometry, TorusGeometry, Vector3,
  type BufferGeometry, type Object3D
} from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { TEAM, type OfficeActor, type OfficeModel, type OfficeRole, type WorkspaceSection } from "./types";

/** 由共享几何与材质搭建办公室；场景中的家具、人物和装饰均可从任意角度观察。 */
export function buildOffice(): OfficeModel {
  const group = new Group();
  group.name = "Quant office";
  const interactives: Object3D[] = [];
  const geometry = {
    box: new BoxGeometry(1, 1, 1),
    soft: new RoundedBoxGeometry(1, 1, 1, 2, 0.09),
    sphere: new SphereGeometry(1, 16, 10),
    cylinder: new CylinderGeometry(1, 1, 1, 16),
    pot: new CylinderGeometry(1, 0.76, 1, 16),
    capsule: new CapsuleGeometry(1, 1, 4, 12),
    ring: new TorusGeometry(1, 0.16, 6, 20),
    hair: new SphereGeometry(1, 16, 8, 0, Math.PI * 2, 0, 1.28),
    bar: new BoxGeometry(1, 1, 1).translate(0, 0.5, 0)
  };
  const material = new Map<string, MeshStandardMaterial>();
  function surface(color: string, emissive = false): MeshStandardMaterial {
    const key = `${color}:${emissive}`;
    let value = material.get(key);
    if (!value) {
      value = new MeshStandardMaterial({ color, roughness: 0.82, metalness: 0.02,
        ...(emissive ? { emissive: color, emissiveIntensity: 0.26 } : {}) });
      material.set(key, value);
    }
    return value;
  }
  function shape(parent: Object3D, geo: BufferGeometry, color: string,
    x: number, y: number, z: number, sx: number, sy: number, sz: number, glow = false): Mesh {
    const mesh = new Mesh(geo, surface(color, glow));
    mesh.position.set(x, y, z);
    mesh.scale.set(sx, sy, sz);
    mesh.castShadow = !glow;
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
  function box(parent: Object3D, color: string, x: number, y: number, z: number,
    w: number, h: number, d: number, soft = false): Mesh {
    return shape(parent, soft ? geometry.soft : geometry.box, color, x, y, z, w, h, d);
  }
  function ball(parent: Object3D, color: string, x: number, y: number, z: number,
    rx: number, ry = rx, rz = rx): Mesh {
    return shape(parent, geometry.sphere, color, x, y, z, rx, ry, rz);
  }
  function cylinder(parent: Object3D, color: string, x: number, y: number, z: number,
    radius: number, height: number): Mesh {
    return shape(parent, geometry.cylinder, color, x, y, z, radius, height, radius);
  }
  function ring(parent: Object3D, color: string, x: number, y: number, z: number, radius: number): Mesh {
    return shape(parent, geometry.ring, color, x, y, z, radius, radius, radius);
  }
  function cluster(parent: Object3D, x: number, y: number, z: number, yaw = 0): Group {
    const result = new Group();
    result.position.set(x, y, z);
    result.rotation.y = yaw;
    parent.add(result);
    return result;
  }
  function interactive(object: Object3D, section: WorkspaceSection): void {
    object.traverse((child) => { child.userData.section = section; });
    interactives.push(object);
  }

  const cream = "#eae3d4";
  const pine = "#23483f";
  const walnut = "#795441";
  const oak = "#ba9069";
  const ink = "#263d3a";
  const brass = "#bb9660";

  // 切面建筑与木地板：保留正面和右侧的观察开口。
  box(group, pine, 0, -0.38, 0, 16.7, 0.64, 10.65, true);
  box(group, "#a67756", 0, -0.085, 0, 16.35, 0.16, 10.3);
  for (let row = 0; row < 14; row += 1) {
    const z = -4.84 + row * 0.744;
    box(group, row % 3 === 0 ? "#caaa85" : "#cfb18d", 0, 0.008, z, 16.06, 0.033, 0.725);
    for (let part = 0; part < 3; part += 1) {
      box(group, "#b4916d", -5.1 + part * 5.1 + (row % 2) * 1.3, 0.027, z, 0.017, 0.008, 0.72);
    }
  }
  box(group, cream, 0, 2.22, -5.1, 16.3, 4.45, 0.23);
  box(group, pine, 0, 0.5, -4.955, 16.05, 1, 0.065);
  box(group, brass, 0, 1.025, -4.905, 16.1, 0.046, 0.06);
  box(group, pine, -8.12, 0.65, 0, 0.22, 1.3, 10.18);
  box(group, oak, -8.12, 1.31, 0, 0.28, 0.1, 10.3, true);
  box(group, walnut, 0, 0.085, -4.9, 16.13, 0.13, 0.15);
  box(group, cream, -8.12, 2.72, -3.87, 0.24, 2.85, 2.3);

  // 左上角窗户有窗洞层次、窗框和窗台，玻璃采用轻微自发光的实体面。
  const window = cluster(group, -6.32, 2.82, -4.92);
  box(window, walnut, 0, 0, 0, 2.45, 2.45, 0.12, true);
  shape(window, geometry.box, "#b8d6cc", 0, 0, 0.075, 2.18, 2.17, 0.035, true);
  for (const x of [-1.08, 0, 1.08]) box(window, cream, x, 0, 0.14, 0.075, 2.2, 0.1);
  for (const y of [-1.08, 0, 1.08]) box(window, cream, 0, y, 0.14, 2.24, 0.075, 0.1);
  box(window, oak, 0, -1.19, 0.15, 2.65, 0.14, 0.5, true);
  box(window, "#ede4ce", -1.36, 0.09, 0.22, 0.23, 2.48, 0.18, true);
  box(window, "#ede4ce", 1.36, 0.09, 0.22, 0.23, 2.48, 0.18, true);

  function plant(parent: Object3D, x: number, y: number, z: number, size = 1): Group {
    const result = cluster(parent, x, y, z);
    result.scale.setScalar(size);
    shape(result, geometry.pot, "#c58866", 0, 0.23, 0, 0.3, 0.46, 0.3);
    cylinder(result, "#594b3b", 0, 0.455, 0, 0.265, 0.02);
    cylinder(result, "#5b7050", 0, 0.92, 0, 0.036, 0.95);
    for (let i = 0; i < 7; i += 1) {
      const angle = i * 2.4;
      const radius = i === 6 ? 0.02 : 0.24;
      const leaf = ball(result, i % 2 ? "#668c58" : "#3f7152",
        Math.sin(angle) * radius, 0.8 + i * 0.095, Math.cos(angle) * radius, 0.14, 0.31, 0.09);
      leaf.rotation.set(Math.cos(angle) * 0.64, angle, -Math.sin(angle) * 0.64);
    }
    return result;
  }
  plant(group, -7.23, 0, 3.89, 1.35);
  plant(group, 7.21, 0, -1.43, 1.3);
  plant(window, 0.65, -1.11, 0.16, 0.46);

  function mug(parent: Object3D, x: number, y: number, z: number, color: string): void {
    cylinder(parent, color, x, y + 0.11, z, 0.09, 0.2);
    cylinder(parent, "#554333", x, y + 0.213, z, 0.069, 0.008);
    ring(parent, color, x + 0.105, y + 0.12, z, 0.059);
  }
  function lamp(parent: Object3D, x: number, y: number, z: number, color: string): void {
    cylinder(parent, color, x, y + 0.035, z, 0.16, 0.07);
    cylinder(parent, brass, x, y + 0.3, z, 0.025, 0.53);
    const arm = cylinder(parent, brass, x + 0.1, y + 0.6, z, 0.026, 0.29);
    arm.rotation.z = -0.82;
    shape(parent, geometry.pot, color, x + 0.21, y + 0.63, z, 0.16, 0.18, 0.16).rotation.z = 0.26;
    shape(parent, geometry.cylinder, "#ffe6ad", x + 0.21, y + 0.548, z, 0.14, 0.016, 0.14, true);
  }
  function stool(parent: Object3D, x: number, z: number): void {
    cylinder(parent, "#475c4b", x, 0.62, z, 0.31, 0.15);
    for (const offset of [-1, 1]) {
      box(parent, walnut, x + offset * 0.21, 0.3, z - 0.16, 0.065, 0.58, 0.065);
      box(parent, walnut, x + offset * 0.21, 0.3, z + 0.16, 0.065, 0.58, 0.065);
    }
  }
  function desk(x: number, z: number, yaw: number, accent: string, role: OfficeRole): void {
    const table = cluster(group, x, 0, z, yaw);
    box(table, oak, 0, 1.095, 0, 2.58, 0.15, 1.23, true);
    box(table, walnut, 0, 1.006, 0, 2.48, 0.065, 1.15);
    for (const side of [-1, 1]) {
      box(table, pine, side * 1.04, 0.51, -0.36, 0.085, 1.02, 0.085);
      box(table, pine, side * 1.04, 0.51, 0.38, 0.085, 1.02, 0.085);
      box(table, pine, side * 1.04, 0.15, 0.01, 0.09, 0.06, 0.8);
    }
    box(table, "#ad815f", -0.73, 0.84, 0.08, 0.62, 0.31, 0.83, true);
    box(table, brass, -0.73, 0.88, 0.505, 0.22, 0.029, 0.035);
    box(table, ink, 0.1, 1.195, -0.27, 0.55, 0.045, 0.29, true);
    box(table, ink, 0.1, 1.42, -0.33, 0.08, 0.45, 0.09);
    box(table, ink, 0.1, 1.65, -0.34, 1.1, 0.69, 0.085, true);
    shape(table, geometry.box, "#375a53", 0.1, 1.66, -0.292, 0.97, 0.55, 0.016, true);
    // 仅绘制软件的界面骨架，收益图由真实数据驱动的大看板承担。
    box(table, accent, -0.31, 1.655, -0.28, 0.065, 0.43, 0.01);
    for (let row = 0; row < 4; row += 1) {
      box(table, row === 0 ? "#adc6b2" : "#64877b", 0.14, 1.83 - row * 0.112, -0.28,
        row === 0 ? 0.68 : 0.55, 0.031, 0.013);
    }
    box(table, cream, 0.08, 1.197, 0.28, 0.72, 0.045, 0.25, true);
    for (let row = 0; row < 3; row += 1) box(table, "#a7aa99", 0.08, 1.224, 0.207 + row * 0.065, 0.62, 0.01, 0.022);
    box(table, "#c9c9b5", 0.65, 1.196, 0.29, 0.15, 0.062, 0.21, true);
    lamp(table, -0.95, 1.17, -0.31, pine);
    mug(table, 0.99, 1.17, 0.2, accent);
    box(table, cream, 0.9, 1.19, -0.36, 0.32, 0.03, 0.28);
    box(table, accent, 0.9, 1.211, -0.32, 0.28, 0.01, 0.032);
    stool(table, 0.4, 0.18);
    interactive(table, role);
  }

  desk(-5.25, -2.88, 0, TEAM[0].color, "market");
  desk(-1.87, -2.88, 0, TEAM[1].color, "factors");
  desk(1.51, -2.88, 0, TEAM[2].color, "portfolio");
  desk(-5.25, 2.82, Math.PI, TEAM[3].color, "execution");
  desk(-1.87, 2.82, Math.PI, TEAM[4].color, "system");

  // 看板柱体几何的原点在底部；渲染层只需更新 scale.y 即可接入实际净值。
  const board = cluster(group, -1.64, 3.09, -4.83);
  board.name = "Portfolio board";
  box(board, pine, 0, 0, 0, 6.06, 2.01, 0.19, true);
  box(board, "#31594c", 0, 0, 0.106, 5.73, 1.7, 0.03);
  box(board, "#aec8b0", -2.13, 0.62, 0.131, 1.14, 0.045, 0.016);
  box(board, "#749c85", -2.36, 0.51, 0.131, 0.68, 0.025, 0.016);
  for (let row = 0; row < 4; row += 1) box(board, "#416856", 0, -0.56 + row * 0.3, 0.132, 5.35, 0.012, 0.012);
  const boardBars: Mesh[] = [];
  for (let i = 0; i < 8; i += 1) {
    const bar = shape(board, geometry.bar, i % 2 ? "#e0b677" : "#a7c4a2", -2.28 + i * 0.65, -0.56, 0.16, 0.31, 1, 0.05);
    bar.name = `Equity sample ${i + 1}`;
    bar.visible = false;
    boardBars.push(bar);
  }
  box(board, walnut, 0, -1.02, 0.2, 6.29, 0.105, 0.43, true);
  interactive(board, "portfolio");

  // 茶水间：有深度的柜门、置物架、咖啡机、水槽与杯子。
  const coffee = cluster(group, 5.87, 0, -3.78);
  coffee.name = "Coffee corner";
  box(coffee, pine, 0, 0.54, 0, 3.05, 1.08, 1.06, true);
  for (const x of [-0.96, 0, 0.96]) {
    box(coffee, "#365b49", x, 0.57, 0.542, 0.9, 0.92, 0.04, true);
    box(coffee, brass, x + 0.29, 0.75, 0.576, 0.038, 0.22, 0.03);
  }
  box(coffee, cream, 0, 1.14, 0, 3.2, 0.15, 1.17, true);
  box(coffee, "#d5cbb5", 0, 1.39, -0.55, 3.07, 0.46, 0.055);
  const machine = cluster(coffee, 0.57, 1.22, -0.04);
  box(machine, "#bb654d", 0, 0.33, 0, 0.75, 0.66, 0.53, true);
  box(machine, ink, 0, 0.3, 0.274, 0.56, 0.37, 0.035);
  box(machine, "#c8c5b8", 0, 0.31, 0.36, 0.07, 0.12, 0.15);
  box(machine, "#b8b8a8", 0, 0.057, 0.17, 0.65, 0.052, 0.4);
  cylinder(machine, walnut, -0.18, 0.68, 0, 0.13, 0.15);
  shape(machine, geometry.sphere, "#a7c5ae", 0.21, 0.51, 0.273, 0.035, 0.035, 0.015, true);
  mug(machine, 0, 0.088, 0.26, cream);
  box(coffee, "#9baca1", -0.87, 1.222, 0.07, 0.7, 0.025, 0.51, true);
  box(coffee, "#657f76", -0.87, 1.24, 0.07, 0.54, 0.024, 0.37, true);
  cylinder(coffee, "#d6c5a2", -0.87, 1.43, -0.2, 0.035, 0.4);
  box(coffee, "#d6c5a2", -0.87, 1.61, -0.11, 0.07, 0.065, 0.23, true);
  mug(coffee, 1.21, 1.22, 0.05, "#dbaa66");
  box(coffee, walnut, 0, 2.6, -0.64, 3.16, 0.11, 0.51, true);
  for (const x of [-1.22, 1.22]) box(coffee, brass, x, 2.47, -0.69, 0.04, 0.22, 0.35);
  plant(coffee, 0.97, 2.66, -0.68, 0.53);
  for (let i = 0; i < 4; i += 1) mug(coffee, -1.07 + i * 0.34, 2.66, -0.6, i % 2 ? "#dbaa66" : cream);
  interactive(coffee, "backtest");

  // 休息区使用厚地毯、独立沙发坐垫和圆桌，中心通道保持空旷。
  const lounge = cluster(group, 5.52, 0.055, 2.66);
  box(lounge, "#d7ae7e", 0, 0, 0, 4.36, 0.05, 4.05, true);
  box(lounge, "#e4c59a", 0, 0.029, 0, 4.02, 0.018, 3.7, true);
  for (let stripe = 0; stripe < 7; stripe += 1) box(lounge, "#cfab80", -1.5 + stripe * 0.5, 0.04, 0, 0.027, 0.009, 3.65);
  const sofa = cluster(lounge, 1.34, 0, -0.05, -Math.PI / 2);
  box(sofa, "#7b9270", 0, 0.53, 0, 2.52, 0.6, 0.98, true);
  box(sofa, "#7b9270", 0, 0.91, -0.43, 2.52, 0.94, 0.27, true);
  for (const x of [-1.16, 1.16]) {
    box(sofa, "#8c9e7d", x, 0.76, 0, 0.24, 0.59, 1.08, true);
    cylinder(sofa, walnut, x * 0.84, 0.11, 0.29, 0.055, 0.22);
    cylinder(sofa, walnut, x * 0.84, 0.11, -0.29, 0.055, 0.22);
  }
  for (const x of [-0.56, 0.56]) {
    box(sofa, "#a5b08c", x, 0.805, 0.045, 1.01, 0.24, 0.75, true);
    box(sofa, "#95a67f", x, 1.02, -0.27, 0.98, 0.6, 0.2, true).rotation.x = -0.12;
  }
  box(sofa, "#deaf72", -0.78, 1.03, -0.035, 0.4, 0.4, 0.18, true).rotation.z = 0.23;
  const table = cluster(lounge, -0.3, 0, -0.03);
  cylinder(table, walnut, 0, 0.37, 0, 0.12, 0.66);
  cylinder(table, walnut, 0, 0.095, 0, 0.46, 0.08);
  cylinder(table, oak, 0, 0.74, 0, 0.79, 0.12);
  box(table, pine, 0.07, 0.822, -0.18, 0.46, 0.047, 0.33, true).rotation.y = -0.2;
  box(table, cream, 0.07, 0.846, -0.18, 0.39, 0.012, 0.27).rotation.y = -0.2;
  mug(table, -0.35, 0.8, 0.13, cream);
  mug(table, 0.32, 0.8, 0.24, "#bc6e53");
  cylinder(lounge, "#b86f51", -1.13, 0.39, 1.13, 0.45, 0.71);
  cylinder(lounge, "#cc8964", -1.13, 0.78, 1.13, 0.47, 0.14);
  interactive(lounge, "backtest");
  plant(group, 7.44, 0, 4.07, 0.91);

  // 后墙时钟与窄柜补充建筑尺度，所有细节均为实体几何。
  const clock = cluster(group, 3.22, 3.62, -4.79);
  cylinder(clock, oak, 0, 0, 0, 0.35, 0.09).rotation.x = Math.PI / 2;
  cylinder(clock, cream, 0, 0, 0.05, 0.3, 0.018).rotation.x = Math.PI / 2;
  box(clock, ink, 0, 0.09, 0.073, 0.023, 0.19, 0.014).rotation.z = -0.5;
  box(clock, ink, 0.08, 0, 0.076, 0.16, 0.024, 0.018).rotation.z = -0.27;
  ball(clock, brass, 0, 0, 0.081, 0.035);
  const cabinet = cluster(group, -7.43, 0, 0.15);
  box(cabinet, walnut, 0, 0.64, 0, 0.77, 1.26, 1.92, true);
  for (const z of [-0.56, 0, 0.56]) {
    box(cabinet, "#91694e", 0.4, 0.65, z, 0.031, 1.1, 0.48);
    box(cabinet, brass, 0.423, 0.91, z, 0.025, 0.05, 0.18);
  }
  plant(cabinet, 0, 1.28, -0.62, 0.46);
  for (let book = 0; book < 4; book += 1) box(cabinet, [cream, pine, "#c08e63", "#8c9eae"][book], 0, 1.315 + book * 0.063, 0.49, 0.56, 0.055, 0.34);

  // 静态家具按材质及点击目标合并；可动人物和数据柱保留独立几何。
  const batches = new Map<string, Mesh<BufferGeometry, MeshStandardMaterial>[]>();
  const dynamicBars = new Set<Mesh>(boardBars);
  group.updateMatrixWorld(true);
  group.traverse((object) => {
    if (!(object instanceof Mesh) || dynamicBars.has(object)) return;
    const mesh = object as Mesh<BufferGeometry, MeshStandardMaterial>;
    const key = `${mesh.material.uuid}:${String(mesh.userData.section ?? "room")}`;
    const batch = batches.get(key);
    if (batch) batch.push(mesh);
    else batches.set(key, [mesh]);
  });
  for (const batch of batches.values()) {
    if (batch.length < 2) continue;
    const copies = batch.map((mesh) => {
      const copy = mesh.geometry.index ? mesh.geometry.toNonIndexed() : mesh.geometry.clone();
      return copy.applyMatrix4(mesh.matrixWorld);
    });
    const merged = mergeGeometries(copies);
    copies.forEach((copy) => copy.dispose());
    if (!merged) continue;
    const mesh = new Mesh(merged, batch[0].material);
    mesh.castShadow = batch[0].castShadow;
    mesh.receiveShadow = true;
    mesh.userData.section = batch[0].userData.section;
    for (const original of batch) original.removeFromParent();
    group.add(mesh);
    if (mesh.userData.section) interactives.push(mesh);
  }

  function actor(id: OfficeRole, index: number, x: number, z: number, yaw: number): OfficeActor {
    const person = cluster(group, x, 0.035, z, yaw);
    person.name = TEAM[index].name;
    const outfit = TEAM[index].color;
    const skin = ["#ddb18e", "#edc5a5", "#b58262", "#edbb96", "#cc9877"][index];
    const hairColor = ["#533b2d", "#293d3a", "#493326", "#80503b", "#373c35"][index];
    box(person, "#364a43", 0, 0.82, 0, 0.4, 0.2, 0.3, true);
    shape(person, geometry.capsule, outfit, 0, 1.15, 0, 0.25, 0.205, 0.177);
    cylinder(person, skin, 0, 1.48, 0, 0.087, 0.19);
    box(person, cream, 0, 1.401, 0.142, 0.09, 0.13, 0.035, true);
    if (id === "portfolio") box(person, pine, 0, 1.25, 0.176, 0.06, 0.29, 0.025, true);
    if (id === "execution") {
      const hood = ring(person, "#bd7668", 0, 1.39, -0.031, 0.15);
      hood.rotation.x = Math.PI / 2;
      box(person, "#bf7969", 0, 1.075, 0.178, 0.23, 0.1, 0.02, true);
    }
    // 工牌有体积，颜色直接对应可点击的角色入口。
    box(person, "#e8e2cc", -0.104, 1.25, 0.177, 0.085, 0.11, 0.025, true);
    box(person, pine, -0.104, 1.253, 0.193, 0.053, 0.03, 0.008);
    const head = cluster(person, 0, 1.7, 0);
    ball(head, skin, 0, 0, 0, 0.25, 0.276, 0.234);
    for (const side of [-1, 1]) {
      ball(head, skin, side * 0.248, -0.012, -0.013, 0.046, 0.06, 0.043);
      ball(head, "#30382e", side * 0.082, 0.04, 0.222, 0.018, 0.026, 0.012);
      box(head, hairColor, side * 0.082, 0.099, 0.224, 0.058, 0.017, 0.016, true);
    }
    ball(head, skin, 0, -0.018, 0.24, 0.045, 0.044, 0.049);
    box(head, "#a16750", 0, -0.113, 0.215, 0.055, 0.016, 0.019, true);
    shape(head, geometry.hair, hairColor, 0, 0.005, -0.009, 0.265, 0.293, 0.252);
    if (index === 0) {
      for (let curl = 0; curl < 4; curl += 1) ball(head, hairColor, -0.17 + curl * 0.11, 0.227 + (curl % 2) * 0.031, 0.025, 0.103, 0.082, 0.16);
    } else if (index === 1) {
      for (const side of [-1, 1]) ball(head, hairColor, side * 0.212, -0.02, -0.087, 0.074, 0.225, 0.172);
      box(head, hairColor, -0.041, 0.158, 0.206, 0.33, 0.06, 0.07, true).rotation.z = -0.12;
    } else if (index === 2) {
      ball(head, hairColor, -0.07, 0.208, 0.051, 0.234, 0.1, 0.206).rotation.z = -0.22;
    } else if (index === 3) {
      ball(head, hairColor, 0, 0.193, -0.233, 0.138);
      ball(head, "#dca96f", 0, 0.22, -0.197, 0.092, 0.037, 0.096);
    } else {
      ball(head, hairColor, 0.111, 0.178, 0.156, 0.12, 0.087, 0.104);
    }
    if (index === 1 || index === 4) {
      for (const side of [-1, 1]) ring(head, ink, side * 0.09, 0.04, 0.238, 0.065);
      box(head, ink, 0, 0.044, 0.241, 0.055, 0.014, 0.017);
    }
    const arms = [-1, 1].map((side) => {
      const joint = cluster(person, side * 0.28, 1.36, 0);
      shape(joint, geometry.capsule, outfit, 0, -0.105, 0, 0.088, 0.093, 0.094);
      shape(joint, geometry.capsule, skin, 0, -0.344, 0, 0.066, 0.102, 0.068);
      ball(joint, skin, 0, -0.493, 0.016, 0.077, 0.085, 0.075);
      joint.rotation.z = side * 0.065;
      return joint;
    });
    const legs = [-1, 1].map((side) => {
      const joint = cluster(person, side * 0.12, 0.82, 0);
      shape(joint, geometry.capsule, "#354b44", 0, -0.28, 0, 0.084, 0.194, 0.087);
      cylinder(joint, "#e0d8c3", 0, -0.603, 0, 0.063, 0.14);
      box(joint, "#36413a", 0, -0.722, 0.046, 0.18, 0.14, 0.29, true);
      box(joint, cream, 0, -0.782, 0.055, 0.184, 0.027, 0.293, true);
      return joint;
    });
    let cup: Group | null = null;
    if (id === "factors" || id === "system") {
      // 杯子的旋转中心固定在右手，渲染层可抵消手臂转角以保持杯口朝上。
      cup = cluster(arms[1], 0, -0.493, 0.016);
      cup.name = `${id} coffee mug`;
      const handle = cluster(cup, 0, 0, 0, Math.PI);
      mug(handle, -0.155, -0.11, 0, cream);
      cup.visible = false;
    }
    interactive(person, id);
    return { id, group: person, head, leftArm: arms[0], rightArm: arms[1],
      leftLeg: legs[0], rightLeg: legs[1], cup, home: person.position.clone(), homeYaw: yaw };
  }

  const actors = [
    actor("market", 0, -5.25, -1.56, Math.PI),
    actor("factors", 1, -1.87, -1.56, Math.PI),
    actor("portfolio", 2, 1.51, -1.56, Math.PI),
    actor("execution", 3, -5.25, 1.5, 0),
    actor("system", 4, -1.87, 1.5, 0)
  ];
  return { group, actors, interactives, board, boardBars,
    coffeePoint: new Vector3(5.2, 0.035, -1.64), meetingPoint: new Vector3(2.8, 0.035, 0.48) };
}
