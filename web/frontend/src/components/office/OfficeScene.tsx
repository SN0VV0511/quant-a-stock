import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { buildOffice } from "./models";
import { activityAt, pointOnPath } from "./activity";
import { TEAM, type OfficeRole, type OfficeSceneProps, type WorkspaceSection } from "./types";
import "../../styles/office-scene.css";

function sectionOf(object: THREE.Object3D): WorkspaceSection | null {
  let current: THREE.Object3D | null = object;
  while (current) {
    const section: unknown = current.userData.section;
    if (section === "backtest" || TEAM.some((person) => person.id === section)) return section as WorkspaceSection;
    current = current.parent;
  }
  return null;
}

/** 所有房间和人物均为可旋转、可拾取的几何模型；服务端只提供已有数据。 */
export function OfficeScene(props: OfficeSceneProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const labelsRef = useRef<Partial<Record<OfficeRole, HTMLButtonElement>>>({});
  const bubblesRef = useRef<Partial<Record<OfficeRole, HTMLSpanElement>>>({});
  const latest = useRef(props);
  latest.current = props;
  const controller = useRef<{ sync: () => void } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
    } catch {
      setError("当前浏览器无法启动 3D 场景。仍可通过下方人物入口查看所有信息。");
      return;
    }
    const scene = new THREE.Scene();
    scene.fog = new THREE.Fog("#d9ddd5", 42, 80);
    renderer.setClearColor(0x000000, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.18;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    const canvas = renderer.domElement;
    canvas.setAttribute("aria-label", "三维办公室：拖拽旋转、滚轮缩放，点击人物查看信息");
    canvas.setAttribute("role", "img");
    mount.appendChild(canvas);

    const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 120);
    camera.position.set(18, 16, 21);
    const controls = new OrbitControls(camera, canvas);
    controls.target.set(0, 0.5, 0);
    controls.enablePan = false;
    controls.enableDamping = false;
    controls.minDistance = 7;
    controls.maxDistance = 42;
    controls.minPolarAngle = Math.PI / 7;
    controls.maxPolarAngle = Math.PI / 2.35;
    controls.minAzimuthAngle = -Math.PI / 3;
    controls.maxAzimuthAngle = Math.PI / 2;
    controls.update();

    scene.add(new THREE.HemisphereLight(0xfff4dc, 0x4d6355, 2.4));
    const sunlight = new THREE.DirectionalLight(0xffdfad, 3.6);
    sunlight.position.set(-5, 14, 8);
    sunlight.castShadow = true;
    sunlight.shadow.mapSize.set(1024, 1024);
    Object.assign(sunlight.shadow.camera, { left: -13, right: 13, top: 12, bottom: -12, near: 0.1, far: 40 });
    sunlight.shadow.normalBias = 0.04;
    scene.add(sunlight);
    const fill = new THREE.DirectionalLight(0xbcdedc, 1.25);
    fill.position.set(8, 6, -4);
    scene.add(fill);
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.12 }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.52;
    ground.receiveShadow = true;
    scene.add(ground);
    const office = buildOffice();
    scene.add(office.group);

    const focus = new THREE.Group();
    const focusMaterial = new THREE.MeshBasicMaterial({ color: "#e9aa63", transparent: true, opacity: 0.8, side: THREE.DoubleSide, depthWrite: false });
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.68, 0.74, 48), focusMaterial);
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.045;
    const outerRing = new THREE.Mesh(new THREE.RingGeometry(0.92, 0.95, 48, 1, 0, Math.PI * 1.45), focusMaterial);
    outerRing.rotation.x = -Math.PI / 2;
    outerRing.position.y = 0.04;
    focus.add(ring, outerRing);
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.8, 0.72, 2.8, 24, 1, true), new THREE.MeshBasicMaterial({ color: "#dbaa70", transparent: true, opacity: 0.09, side: THREE.DoubleSide, depthWrite: false }));
    beam.position.y = 1.42;
    focus.add(beam);
    const points = new Float32Array(24 * 3);
    for (let index = 0; index < 24; index += 1) {
      const angle = index / 24 * Math.PI * 2;
      points[index * 3] = Math.cos(angle) * 0.8;
      points[index * 3 + 1] = index / 24 * 2.5;
      points[index * 3 + 2] = Math.sin(angle) * 0.8;
    }
    const particleGeometry = new THREE.BufferGeometry();
    particleGeometry.setAttribute("position", new THREE.BufferAttribute(points, 3));
    const particles = new THREE.Points(particleGeometry, new THREE.PointsMaterial({ color: "#f2ce8c", size: 0.06, transparent: true, opacity: 0.8, depthWrite: false }));
    focus.add(particles);
    focus.visible = false;
    scene.add(focus);

    // 低模办公室固定五人，无物理引擎；路线只经过工位前方的公共走廊。
    const actors = office.actors.map((actor, index) => {
      const destination = office.coffeePoint.clone().add(new THREE.Vector3(actor.id === "factors" ? -0.65 : 0.65, 0, 0));
      const route: Array<readonly [number, number, number]> = [
        [actor.home.x, actor.home.y, actor.home.z],
        [actor.home.x, actor.home.y, 0.15],
        [destination.x, actor.home.y, 0.15],
        [destination.x, actor.home.y, destination.z]
      ];
      const hit = new THREE.Mesh(new THREE.CylinderGeometry(0.52, 0.52, 2.3, 8), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, colorWrite: false }));
      hit.position.y = 1.1;
      hit.userData.section = actor.id;
      actor.group.add(hit);
      office.interactives.push(hit);
      return { ...actor, index, route, seconds: 0, armX: [actor.leftArm.rotation.x, actor.rightArm.rotation.x], legX: [actor.leftLeg.rotation.x, actor.rightLeg.rotation.x], headY: actor.head.rotation.y, activity: "work" };
    });

    let frame = 0;
    let disposed = false;
    let width = 1;
    let height = 1;
    let previousTime = performance.now();
    let lastRender = 0;
    let seconds = 0;
    let oldSelection: WorkspaceSection | undefined;
    let oldView: OfficeSceneProps["view"] | undefined;
    let oldReset = -1;
    let tween: { start: number; from: THREE.Vector3; to: THREE.Vector3; fromTarget: THREE.Vector3; toTarget: THREE.Vector3 } | null = null;
    const projected = new THREE.Vector3();
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let pointerStart: { x: number; y: number; dragged: boolean } | null = null;
    let lastHover = 0;

    const requestFrame = () => {
      if (!disposed && !document.hidden && !frame) frame = requestAnimationFrame(render);
    };

    function moveCamera(position: THREE.Vector3, target: THREE.Vector3) {
      if (latest.current.reducedMotion) {
        camera.position.copy(position);
        controls.target.copy(target);
        controls.update();
        tween = null;
      } else {
        tween = { start: performance.now(), from: camera.position.clone(), to: position, fromTarget: controls.target.clone(), toTarget: target };
      }
      requestFrame();
    }

    function overview() {
      const scale = width < 700 ? 1.25 : 1;
      const view = latest.current.view;
      if (view === "lounge") moveCamera(new THREE.Vector3(15, 12, 15).multiplyScalar(scale), new THREE.Vector3(4, 0.7, 0));
      else if (view === "desks") moveCamera(new THREE.Vector3(9, 12, 15).multiplyScalar(scale), new THREE.Vector3(-2, 0.5, -1));
      else moveCamera(new THREE.Vector3(18, 16, 21).multiplyScalar(scale), new THREE.Vector3(0, 0.5, 0));
    }

    function resize() {
      width = Math.max(mount?.clientWidth ?? 1, 1);
      height = Math.max(mount?.clientHeight ?? 1, 1);
      camera.aspect = width / height;
      camera.zoom = latest.current.selected === "theater" ? Math.min(1, camera.aspect / 0.8) : 1;
      if (latest.current.selected !== "theater" && width > 760) camera.setViewOffset(width, height, width * 0.2, 0, width, height);
      else if (latest.current.selected !== "theater") camera.setViewOffset(width, height, 0, height * 0.3, width, height);
      else camera.clearViewOffset();
      camera.updateProjectionMatrix();
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, latest.current.quality === "low" ? 1 : 1.5));
      renderer.setSize(width, height, false);
      requestFrame();
    }

    function sync() {
      const current = latest.current;
      renderer.shadowMap.enabled = current.quality !== "low";
      resize();
      const selectionChanged = oldSelection !== current.selected;
      const viewChanged = oldView !== current.view || oldReset !== current.resetKey;
      if (selectionChanged || viewChanged) {
        oldSelection = current.selected;
        oldView = current.view;
        oldReset = current.resetKey;
        const actor = actors.find((person) => person.id === current.selected);
        if (actor && !viewChanged) {
          const target = actor.group.position.clone().add(new THREE.Vector3(0, 1, 0));
          moveCamera(target.clone().add(new THREE.Vector3(6, 5.5, 8)), target);
        } else if (current.selected === "backtest" && !viewChanged) {
          const target = office.board.getWorldPosition(new THREE.Vector3());
          moveCamera(target.clone().add(new THREE.Vector3(7, 5, 11)), target);
        } else overview();
      }
      focus.visible = current.selected !== "theater";
      const selectedActor = actors.find((actor) => actor.id === current.selected);
      if (selectedActor) {
        focus.position.copy(selectedActor.group.position);
        const color = TEAM.find((person) => person.id === selectedActor.id)?.color ?? "#e9aa63";
        focusMaterial.color.set(color);
        beam.material.color.set(color);
        particles.material.color.set(color);
      } else focus.visible = false;
      const values = current.metrics.equity.filter((value) => Number.isFinite(value) && value > 0);
      const minimum = Math.min(...values);
      const maximum = Math.max(...values);
      office.boardBars.forEach((bar, index) => {
        const value = values[Math.round(index / Math.max(office.boardBars.length - 1, 1) * (values.length - 1))];
        bar.visible = values.length > 1;
        bar.scale.y = values.length > 1 ? 0.15 + (value - minimum) / (maximum - minimum || 1) * 0.9 : 0.15;
      });
      requestFrame();
    }

    function render(now: number) {
      frame = 0;
      if (disposed || document.hidden) return;
      const current = latest.current;
      const animatePeople = !current.paused && !current.reducedMotion;
      const fps = current.quality === "low" ? 20 : 30;
      if (now - lastRender < 1000 / fps && (animatePeople || tween)) { requestFrame(); return; }
      const delta = Math.min(Math.max(0, (now - previousTime) / 1000), 0.1);
      previousTime = now;
      lastRender = now;
      if (animatePeople) seconds += delta;
      if (tween) {
        const t = Math.min(1, (now - tween.start) / 950);
        const eased = 1 - Math.pow(1 - t, 3);
        camera.position.lerpVectors(tween.from, tween.to, eased);
        controls.target.lerpVectors(tween.fromTarget, tween.toTarget, eased);
        if (t >= 1) tween = null;
      }
      controls.update();
      // DOM 名牌和画布共用本帧相机矩阵，暂停时也能准确跟随最后一次拖动。
      camera.updateMatrixWorld();
      for (const actor of actors) {
        const selected = current.selected === actor.id;
        if (animatePeople && !selected) actor.seconds += delta;
        const activity = activityAt(actor.id, actor.seconds);
        actor.activity = activity.kind;
        if (!selected) {
          if (activity.kind === "work") {
            actor.group.position.copy(actor.home);
            actor.group.rotation.y = actor.homeYaw;
          } else {
            const walk = activity.kind === "walk-out" || activity.kind === "walk-home";
            const progress = activity.kind === "walk-home" ? 1 - activity.progress : activity.kind === "walk-out" ? activity.progress : 1;
            const pose = pointOnPath(actor.route, progress);
            actor.group.position.set(...pose.position);
            actor.group.rotation.y = walk ? pose.yaw + (activity.kind === "walk-home" ? Math.PI : 0) : actor.id === "factors" ? Math.PI / 2 : -Math.PI / 2;
          }
        }
        const walking = !selected && (activity.kind === "walk-out" || activity.kind === "walk-home");
        const phase = actor.seconds * (walking ? 8 : 3.5) + actor.index;
        actor.leftLeg.rotation.x = actor.legX[0] + (walking ? Math.sin(phase) * 0.4 : 0);
        actor.rightLeg.rotation.x = actor.legX[1] - (walking ? Math.sin(phase) * 0.4 : 0);
        actor.leftArm.rotation.x = actor.armX[0] + (walking ? Math.sin(phase) * -0.32 : -0.7 + Math.sin(phase) * 0.045);
        actor.rightArm.rotation.x = actor.armX[1] + (walking ? Math.sin(phase + 1) * 0.32 : -0.7 + Math.sin(phase + 1) * 0.07);
        actor.head.rotation.y = actor.headY + Math.sin(actor.seconds * 0.7 + actor.index) * 0.1;
        if (activity.kind === "coffee" || activity.kind === "chat") actor.rightArm.rotation.x = -0.8 + Math.sin(phase) * 0.2;
        if (selected) {
          focus.position.copy(actor.group.position);
          actor.head.rotation.y = actor.headY + 0.2;
          actor.rightArm.rotation.x = -1.9 + Math.sin(seconds * 4) * 0.18;
          const facing = Math.atan2(camera.position.x - actor.group.position.x, camera.position.z - actor.group.position.z);
          const turn = Math.atan2(Math.sin(facing - actor.group.rotation.y), Math.cos(facing - actor.group.rotation.y));
          actor.group.rotation.y += turn * (current.reducedMotion ? 1 : Math.min(1, delta * 8));
        }
        if (actor.cup) {
          actor.cup.visible = !selected && (activity.kind === "coffee" || activity.kind === "chat");
          actor.cup.quaternion.copy(actor.rightArm.quaternion).invert();
        }
        const label = labelsRef.current[actor.id];
        const bubble = bubblesRef.current[actor.id];
        if (label) {
          projected.copy(actor.group.position).add(new THREE.Vector3(0, 2.55, 0)).project(camera);
          const x = (projected.x + 1) * width / 2;
          const y = (1 - projected.y) * height / 2;
          label.style.transform = `translate3d(${x}px,${y}px,0) translate(-50%,-100%)`;
          label.style.visibility = projected.z < 1 && projected.z > -1 && x > 25 && x < width - 25 && y > 40 && y < height - 40 ? "visible" : "hidden";
          label.dataset.selected = String(selected);
          if (bubble) {
            const chatting = activity.kind === "chat" && actors.some((person) => person.id !== actor.id && person.activity === "chat" && person.group.position.distanceTo(actor.group.position) < 2);
            bubble.textContent = selected ? "来看看我的工作台" : chatting ? actor.id === "factors" ? "咖啡好了，一起歇一会？" : "好呀，等会儿回工位。" : activity.kind === "coffee" ? "先喝一口咖啡 ☕" : activity.kind === "walk-out" ? "去茶水间走走" : activity.kind === "walk-home" ? "回工位继续忙" : "";
          }
        }
      }
      ring.scale.setScalar(1 + Math.sin(seconds * 3) * 0.045);
      outerRing.rotation.z = seconds * 0.15;
      particles.rotation.y = seconds * 0.3;
      renderer.render(scene, camera);
      if (animatePeople || tween) requestFrame();
    }

    function hitAt(event: PointerEvent): WorkspaceSection | null {
      const bounds = canvas.getBoundingClientRect();
      pointer.set((event.clientX - bounds.left) / bounds.width * 2 - 1, -(event.clientY - bounds.top) / bounds.height * 2 + 1);
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObject(office.group, true).find(({ object }) => {
        let current: THREE.Object3D | null = object;
        while (current) {
          if (!current.visible) return false;
          current = current.parent;
        }
        return true;
      });
      return hit ? sectionOf(hit.object) : null;
    }
    const pointerDown = (event: PointerEvent) => {
      pointerStart = event.isPrimary && event.button === 0 ? { x: event.clientX, y: event.clientY, dragged: false } : null;
    };
    const pointerUp = (event: PointerEvent) => {
      if (pointerStart && !pointerStart.dragged && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) < 6) {
        const section = hitAt(event);
        if (section) latest.current.onSelect(section);
      }
      pointerStart = null;
    };
    const pointerCancel = () => { pointerStart = null; };
    const pointerMove = (event: PointerEvent) => {
      if (pointerStart && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) >= 6) pointerStart.dragged = true;
      if (pointerStart?.dragged) return;
      if (performance.now() - lastHover < 80) return;
      lastHover = performance.now();
      canvas.style.cursor = hitAt(event) ? "pointer" : "grab";
    };
    const visibilityChanged = () => {
      if (document.hidden) { cancelAnimationFrame(frame); frame = 0; }
      else { previousTime = performance.now(); requestFrame(); }
    };
    const controlStart = () => { tween = null; };
    const contextLost = (event: Event) => {
      event.preventDefault();
      cancelAnimationFrame(frame);
      frame = 0;
      disposed = true;
      setError("3D 显示已暂停，请刷新页面恢复。人物入口和账户信息仍可使用。");
    };
    controls.addEventListener("change", requestFrame);
    controls.addEventListener("start", controlStart);
    canvas.addEventListener("pointerdown", pointerDown);
    canvas.addEventListener("pointerup", pointerUp);
    canvas.addEventListener("pointermove", pointerMove);
    canvas.addEventListener("pointercancel", pointerCancel);
    canvas.addEventListener("webglcontextlost", contextLost);
    document.addEventListener("visibilitychange", visibilityChanged);
    const observer = new ResizeObserver(resize);
    observer.observe(mount);
    controller.current = { sync };
    sync();

    return () => {
      disposed = true;
      controller.current = null;
      cancelAnimationFrame(frame);
      observer.disconnect();
      document.removeEventListener("visibilitychange", visibilityChanged);
      canvas.removeEventListener("pointerdown", pointerDown);
      canvas.removeEventListener("pointerup", pointerUp);
      canvas.removeEventListener("pointermove", pointerMove);
      canvas.removeEventListener("pointercancel", pointerCancel);
      canvas.removeEventListener("webglcontextlost", contextLost);
      controls.dispose();
      const geometries = new Set<THREE.BufferGeometry>();
      const materials = new Set<THREE.Material>();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.Points || object instanceof THREE.Line) {
          geometries.add(object.geometry);
          (Array.isArray(object.material) ? object.material : [object.material]).forEach((material) => materials.add(material));
        }
      });
      geometries.forEach((geometry) => geometry.dispose());
      materials.forEach((material) => material.dispose());
      sunlight.shadow.dispose();
      renderer.dispose();
      canvas.remove();
    };
  }, []);

  useEffect(() => { controller.current?.sync(); }, [props.selected, props.metrics, props.paused, props.reducedMotion, props.quality, props.view, props.resetKey]);

  return (
    <div className="office-scene" data-testid="office-scene" data-renderer="three-webgl" data-selected={props.selected}>
      <div className="office-canvas-mount" ref={mountRef} />
      {!error && <div className="office-world-labels">
        {TEAM.map((person) => <button key={person.id} ref={(element) => { if (element) labelsRef.current[person.id] = element; }} className="office-person-label" type="button" aria-label={`查看${person.name}的${person.title}工作台`} aria-pressed={props.selected === person.id} onClick={() => props.onSelect(person.id)}>
          <span className="office-person-label__bubble" ref={(element) => { if (element) bubblesRef.current[person.id] = element; }} />
          <i style={{ background: person.color }} /><span>{person.name}</span><small>{person.title}</small>
        </button>)}
      </div>}
      {error && <div className="office-webgl-fallback" role="status"><strong>办公室暂时休息一下</strong><p>{error}</p></div>}
    </div>
  );
}
