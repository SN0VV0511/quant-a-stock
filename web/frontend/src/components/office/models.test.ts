import { Mesh, PerspectiveCamera, Raycaster, Vector2, Vector3 } from "three";
import { expect, it } from "vitest";
import { buildOffice } from "./models";

it("真实人物可命中，不透明窗帘遮挡后方看板", () => {
  const office = buildOffice();
  office.group.updateMatrixWorld(true);
  const visibleMeshes: Mesh[] = [];
  office.group.traverseVisible((object) => {
    if (object instanceof Mesh) visibleMeshes.push(object);
  });
  const camera = new PerspectiveCamera(36, 1.5, 0.1, 120);
  const raycaster = new Raycaster();
  try {
    camera.position.set(18, 16, 21);
    camera.lookAt(0, 0.5, 0);
    camera.updateMatrixWorld();
    for (const actor of office.actors) {
      for (const height of [1.1, 1.7]) {
        const point = actor.group.localToWorld(new Vector3(0, height, 0)).project(camera);
        raycaster.setFromCamera(new Vector2(point.x, point.y), camera);
        const hit = raycaster.intersectObjects(visibleMeshes, false)[0];
        expect(hit?.object.userData.section).toBe(actor.id);
      }
    }

    // 控件允许的左侧低角度：窗帘在前，组合看板在后，不能穿透房间物件拾取。
    const polar = Math.PI / 2.35;
    const azimuth = -Math.PI / 3;
    camera.position.set(
      30 * Math.sin(polar) * Math.sin(azimuth),
      30 * Math.cos(polar) + 0.5,
      30 * Math.sin(polar) * Math.cos(azimuth)
    );
    camera.lookAt(0, 0.5, 0);
    camera.updateMatrixWorld();
    raycaster.setFromCamera(new Vector2(-0.475, 0.125), camera);
    const hits = raycaster.intersectObjects(visibleMeshes, false);
    expect(hits.length).toBeGreaterThan(1);
    expect(hits[0].object.userData.section).toBeUndefined();
    const boardHit = hits.find((hit) => hit.object.userData.section === "portfolio");
    expect(boardHit).toBeDefined();
    expect(boardHit?.distance).toBeGreaterThan(hits[0].distance);
  } finally {
    office.group.traverse((object) => {
      if (!(object instanceof Mesh)) return;
      object.geometry.dispose();
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      materials.forEach((material) => material.dispose());
    });
  }
});
