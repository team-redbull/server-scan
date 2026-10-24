import { Environment, Lightformer, PointerLockControls } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { ReactNode, RefObject } from "react";
import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";

import { RACK_U, U } from "@/features/datacenter/layout";
import type { SceneData } from "@/features/datacenter/model";
import { cableOwnerKey, chassisKey, fiKey, MODELS, resolveKey } from "@/features/datacenter/model";
import {
  aisleTexture,
  floorTexture,
  frontTexture,
  labelTexture,
  rearTexture,
} from "@/features/datacenter/textures";
import type { GoTo, Pose } from "@/features/datacenter/world";
import {
  aislesOf,
  frontSign,
  halfWidth,
  RACK_D,
  RACK_W,
  rackX,
  rowYaw,
  rowZ,
  worldX,
  Z_MIN,
  zMaxOf,
} from "@/features/datacenter/world";
import type { HealthSeverity } from "@/types/server";

const BASE = 0.1;
const SERVER_D = 0.75;
const FRONT_Z = -RACK_D / 2 + 0.03;
const REAR_Z = FRONT_Z + SERVER_D;
const RACK_H = BASE * 2 + RACK_U * U;
const TRAY_Y = RACK_H + 0.25;
const CEILING = 3.4;
const EYE = 1.7;
const CROUCH_EYE = 0.8;

const HEALTH_TINT: Record<HealthSeverity, string> = {
  HEALTHY: "#ffffff",
  WARNING: "#ffd27a",
  MAJOR: "#ff9a5c",
  CRITICAL: "#ff3a3a",
  UNKNOWN: "#8c8c8c",
};
const LINK_COLOR: Record<string, string> = {
  UP: "#22d37a",
  DOWN: "#ff2d2d",
  DISABLED: "#777777",
  UNKNOWN: "#d8b400",
};

export interface SceneProps {
  data: SceneData;
  /** Key whose cables stay bright while the rest dim. */
  focus: string | null;
  beacon: string | null;
  goto: GoTo | null;
  poseRef: RefObject<Pose>;
  onHover: (key: string | null) => void;
  onOpen: (key: string) => void;
}

function rowMatrix(r: number) {
  return new THREE.Matrix4().compose(
    new THREE.Vector3(0, 0, rowZ(r)),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), rowYaw(r)),
    new THREE.Vector3(1, 1, 1),
  );
}

const tmp = new THREE.Object3D();
function place(row: number, p: [number, number, number], ry: number, s: [number, number, number]) {
  tmp.position.set(...p);
  tmp.rotation.set(0, ry, 0);
  tmp.scale.set(...s);
  tmp.updateMatrix();
  return rowMatrix(row).multiply(tmp.matrix);
}

/** Where a picked thing stands in the room: the rack's x, the row's z and the item's height. */
function targetOf(data: SceneData, key: string) {
  const r = resolveKey(data, key);
  const per = data.racksPerRow;
  if (!r) return null;
  if (r.kind === "unit") {
    const spec = MODELS[r.unit.model];
    const y = BASE + (r.unit.u + (spec?.h ?? 1) / 2) * U;
    return { row: r.unit.row, x: worldX(r.unit.row, rackX(r.unit.rack, per) + r.unit.dx), y };
  }
  if (r.kind === "chassis") {
    const y = BASE + (r.chassis.u + (MODELS[r.chassis.model]?.h ?? 6) / 2) * U;
    return { row: r.chassis.row, x: worldX(r.chassis.row, rackX(r.chassis.rack, per)), y };
  }
  return { row: r.fi.row, x: worldX(r.fi.row, rackX(per, per)), y: BASE + (30 + r.fi.slot + 0.5) * U };
}

interface Group {
  matrices: THREE.Matrix4[];
  colors: THREE.Color[];
  ids: string[];
}

function buildInstances(data: SceneData) {
  const per = data.racksPerRow;
  const bodies: THREE.Matrix4[] = [];
  const fronts = new Map<string, Group>();
  const rears = new Map<string, THREE.Matrix4[]>();
  const white = new THREE.Color("#ffffff");
  const addFront = (model: string, m: THREE.Matrix4, color: THREE.Color, id: string) => {
    const g = fronts.get(model) ?? { matrices: [], colors: [], ids: [] };
    g.matrices.push(m);
    g.colors.push(color);
    g.ids.push(id);
    fronts.set(model, g);
  };
  const addBox = (
    row: number,
    x: number,
    y: number,
    model: string,
    spec: { w: number; h: number },
    id: string,
    color: THREE.Color,
  ) => {
    const hh = spec.h * U - 0.004;
    bodies.push(place(row, [x, y, FRONT_Z + SERVER_D / 2], 0, [spec.w, hh, 1]));
    addFront(model, place(row, [x, y, FRONT_Z - 0.002], Math.PI, [spec.w + 0.02, hh, 1]), color, id);
    rears.set(model, [...(rears.get(model) ?? []), place(row, [x, y, REAR_Z + 0.002], 0, [spec.w, hh, 1])]);
  };

  for (const u of data.units) {
    const spec = MODELS[u.model];
    if (!spec) continue;
    const x = rackX(u.rack, per) + u.dx;
    const y = BASE + (u.u + spec.h / 2) * U;
    const color = new THREE.Color(HEALTH_TINT[u.health]);
    if (spec.kind === "blade") {
      addFront(u.model, place(u.row, [x, y, FRONT_Z - 0.007], Math.PI, [spec.w - 0.006, spec.h * U - 0.008, 1]), color, u.id);
    } else {
      addBox(u.row, x, y, u.model, spec, u.id, color);
    }
  }
  data.chassis.forEach((c, i) => {
    const spec = MODELS[c.model];
    if (spec) addBox(c.row, rackX(c.rack, per), BASE + (c.u + spec.h / 2) * U, c.model, spec, chassisKey(i), white);
  });
  const fi = MODELS["FI 6454"];
  if (fi) {
    data.fis.forEach((f, i) => {
      addBox(f.row, rackX(per, per), BASE + (30 + f.slot + 0.5) * U, "FI 6454", fi, fiKey(i), white);
    });
  }
  return { bodies, fronts, rears };
}

interface CableRec {
  owner: string;
  fi: number;
  color: string;
  curve: THREE.CatmullRomCurve3;
}

function buildCableRecs(data: SceneData): CableRec[] {
  const per = data.racksPerRow;
  const lanes = new Map<number, number>();
  const recs: CableRec[] = [];
  const owners = [
    ...data.units.map((u) => ({ key: u.id, row: u.row, rack: u.rack, u: u.u, h: MODELS[u.model]?.h ?? 1, links: u.links })),
    ...data.chassis.map((c, i) => ({ key: chassisKey(i), row: c.row, rack: c.rack, u: c.u, h: MODELS[c.model]?.h ?? 6, links: c.links })),
  ];
  for (const o of owners) {
    o.links.forEach((l, k) => {
      const lane = lanes.get(o.row) ?? 0;
      lanes.set(o.row, lane + 1);
      const x0 = rackX(o.rack, per) - 0.15 + k * 0.07;
      const y0 = BASE + (o.u + o.h / 2) * U;
      const xf = rackX(per, per) - 0.17 + (lane % 8) * 0.045;
      const yf = BASE + (30 + l.fabric + 0.5) * U;
      const zl = REAR_Z + 0.1 + 0.02 * (lane % 16);
      const yt = TRAY_Y - 0.01 * (lane % 16);
      const m = rowMatrix(o.row);
      const pts = (
        [
          [x0, y0, REAR_Z + 0.01],
          [x0, y0, zl],
          [x0, yt, zl],
          [xf, yt, zl],
          [xf, yf, zl],
          [xf, yf, REAR_Z + 0.01],
        ] as [number, number, number][]
      ).map(([x, y, z]) => new THREE.Vector3(x, y, z).applyMatrix4(m));
      recs.push({
        owner: o.key,
        fi: data.fis.findIndex((f) => f.row === o.row && f.slot === l.fabric),
        color: LINK_COLOR[l.state] ?? "#d8b400",
        curve: new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0.15),
      });
    });
  }
  return recs;
}

/** One merged mesh per link colour keeps thousands of cables to a handful of draw calls. */
function mergeTubes(recs: CableRec[], radius: number, radial: number) {
  const byColor = new Map<string, THREE.BufferGeometry[]>();
  for (const r of recs) {
    byColor.set(r.color, [...(byColor.get(r.color) ?? []), new THREE.TubeGeometry(r.curve, 18, radius, radial, false)]);
  }
  const out: { color: string; geometry: THREE.BufferGeometry }[] = [];
  for (const [color, list] of byColor) {
    const merged = mergeGeometries(list);
    list.forEach((g) => g.dispose());
    if (merged) out.push({ color, geometry: merged });
  }
  return out;
}

function Instanced({
  geometry,
  matrices,
  colors,
  ids,
  children,
}: {
  geometry: THREE.BufferGeometry;
  matrices: THREE.Matrix4[];
  colors?: THREE.Color[];
  ids?: string[];
  children: ReactNode;
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const mesh = ref.current;
    if (!mesh) return;
    matrices.forEach((m, i) => mesh.setMatrixAt(i, m));
    mesh.instanceMatrix.needsUpdate = true;
    if (colors) {
      colors.forEach((c, i) => mesh.setColorAt(i, c));
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
    mesh.computeBoundingSphere();
  }, [matrices, colors]);
  return (
    <instancedMesh
      key={matrices.length}
      ref={ref}
      args={[geometry, undefined, matrices.length]}
      frustumCulled={false}
      userData={{ ids }}
    >
      {children}
    </instancedMesh>
  );
}

function Racks({ data }: { data: SceneData }) {
  const per = data.racksPerRow;
  const geometry = useMemo(() => {
    const parts: THREE.BufferGeometry[] = [];
    const add = (w: number, h: number, d: number, x: number, y: number, z: number) =>
      parts.push(new THREE.BoxGeometry(w, h, d).translate(x, y, z));
    for (const sx of [-1, 1]) {
      add(0.012, RACK_H, RACK_D, sx * (RACK_W / 2 - 0.01), RACK_H / 2, 0);
      for (const sz of [-1, 1]) add(0.03, RACK_H, 0.03, sx * 0.235, RACK_H / 2, sz * 0.46);
    }
    add(RACK_W, 0.01, RACK_D, 0, RACK_H - 0.005, 0);
    add(RACK_W, 0.1, RACK_D, 0, 0.05, 0);
    return mergeGeometries(parts);
  }, []);
  const matrices = useMemo(
    () => [
      ...data.racks.map((r) => place(r.row, [rackX(r.rack, per), 0, 0], 0, [1, 1, 1])),
      ...Array.from({ length: data.rows }, (_, row) => place(row, [rackX(per, per), 0, 0], 0, [1, 1, 1])),
    ],
    [data, per],
  );
  if (!geometry) return null;
  return (
    <Instanced geometry={geometry} matrices={matrices}>
      <meshStandardMaterial color="#34363c" metalness={0.7} roughness={0.4} />
    </Instanced>
  );
}

function Hardware({ data, frontsRef }: { data: SceneData; frontsRef: RefObject<THREE.Group | null> }) {
  const { bodies, fronts, rears } = useMemo(() => buildInstances(data), [data]);
  const box = useMemo(() => new THREE.BoxGeometry(1, 1, SERVER_D), []);
  const plane = useMemo(() => new THREE.PlaneGeometry(1, 1), []);
  return (
    <>
      <Instanced geometry={box} matrices={bodies}>
        <meshStandardMaterial color="#25272c" metalness={0.7} roughness={0.45} />
      </Instanced>
      <group ref={frontsRef}>
        {[...fronts].map(([model, g]) => (
          <Instanced key={model} geometry={plane} matrices={g.matrices} colors={g.colors} ids={g.ids}>
            <meshStandardMaterial map={frontTexture(model)} metalness={0.3} roughness={0.5} />
          </Instanced>
        ))}
      </group>
      {[...rears].map(([model, m]) => (
        <Instanced key={model} geometry={plane} matrices={m}>
          <meshStandardMaterial map={rearTexture(model)} metalness={0.3} roughness={0.6} />
        </Instanced>
      ))}
    </>
  );
}

function Cables({ data, focus }: { data: SceneData; focus: string | null }) {
  const recs = useMemo(() => buildCableRecs(data), [data]);
  const base = useMemo(() => mergeTubes(recs, 0.006, 4), [recs]);
  const lit = useMemo(() => {
    const owner = cableOwnerKey(data, focus);
    const fi = focus?.startsWith("fi:") ? Number(focus.slice(3)) : -1;
    if (!owner) return [];
    return mergeTubes(
      recs.filter((r) => r.owner === owner || (fi >= 0 && r.fi === fi)),
      0.012,
      6,
    );
  }, [data, focus, recs]);
  useEffect(() => () => base.forEach((c) => c.geometry.dispose()), [base]);
  useEffect(() => () => lit.forEach((c) => c.geometry.dispose()), [lit]);
  const dim = focus !== null && lit.length > 0;
  return (
    <>
      {base.map((c) => (
        <mesh key={c.color} geometry={c.geometry}>
          <meshStandardMaterial
            color={c.color}
            emissive={c.color}
            emissiveIntensity={0.35}
            roughness={0.5}
            transparent={dim}
            opacity={dim ? 0.08 : 1}
            depthWrite={!dim}
          />
        </mesh>
      ))}
      {lit.map((c) => (
        <mesh key={`lit${c.color}`} geometry={c.geometry}>
          <meshStandardMaterial color={c.color} emissive={c.color} emissiveIntensity={1.4} roughness={0.4} />
        </mesh>
      ))}
    </>
  );
}

function Beacon({ data, target }: { data: SceneData; target: string | null }) {
  const ref = useRef<THREE.Mesh>(null);
  const t = target ? targetOf(data, target) : null;
  useFrame(({ clock }) => {
    const m = ref.current?.material;
    if (m && !Array.isArray(m)) m.opacity = 0.3 + 0.15 * Math.sin(clock.elapsedTime * 3);
  });
  if (!t) return null;
  return (
    <mesh ref={ref} position={[t.x, CEILING / 2, rowZ(t.row)]}>
      <cylinderGeometry args={[0.05, 0.05, CEILING, 16, 1, true]} />
      <meshBasicMaterial color="#38e1ff" transparent opacity={0.4} depthWrite={false} side={THREE.DoubleSide} />
    </mesh>
  );
}

function Room({ data }: { data: SceneData }) {
  const half = halfWidth(data.racksPerRow);
  const zMax = zMaxOf(data.rows);
  const depth = zMax - Z_MIN;
  const cz = (Z_MIN + zMax) / 2;
  const floor = useMemo(() => {
    const t = floorTexture().clone();
    t.needsUpdate = true;
    t.repeat.set((half * 2) / 0.6, depth / 0.6);
    return t;
  }, [half, depth]);
  const aisles = useMemo(
    () =>
      aislesOf(data.rows).map((a) => {
        const tex = aisleTexture(a.kind).clone();
        tex.needsUpdate = true;
        tex.repeat.set(half / 2, 1);
        return { ...a, tex };
      }),
    [data.rows, half],
  );
  const lights = useMemo(() => {
    const out: THREE.Matrix4[] = [];
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(Math.PI / 2, 0, 0));
    for (let x = -half + 2; x < half - 1; x += 3) {
      for (let z = Z_MIN + 1.5; z < zMax; z += 2.6) {
        out.push(new THREE.Matrix4().compose(new THREE.Vector3(x, CEILING - 0.02, z), q, new THREE.Vector3(1.2, 0.3, 1)));
      }
    }
    return out;
  }, [half, zMax]);
  const plane = useMemo(() => new THREE.PlaneGeometry(1, 1), []);
  const wall = (pos: [number, number, number], ry: number, w: number) => (
    <mesh position={pos} rotation={[0, ry, 0]}>
      <planeGeometry args={[w, CEILING]} />
      <meshStandardMaterial color="#aeb4bb" roughness={0.9} />
    </mesh>
  );
  return (
    <group>
      <mesh position={[0, 0, cz]} rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[half * 2, depth]} />
        <meshStandardMaterial map={floor} roughness={0.55} metalness={0.2} />
      </mesh>
      {aisles.map((a) => (
        <mesh key={`${a.kind}${a.z}`} position={[0, 0.004, a.z]} rotation={[-Math.PI / 2, 0, 0]}>
          <planeGeometry args={[half * 2, a.width]} />
          <meshBasicMaterial map={a.tex} transparent depthWrite={false} />
        </mesh>
      ))}
      <mesh position={[0, CEILING, cz]} rotation={[Math.PI / 2, 0, 0]}>
        <planeGeometry args={[half * 2, depth]} />
        <meshStandardMaterial color="#d4d7db" roughness={0.9} />
      </mesh>
      {wall([0, CEILING / 2, Z_MIN], 0, half * 2)}
      {wall([0, CEILING / 2, zMax], Math.PI, half * 2)}
      {wall([-half, CEILING / 2, cz], Math.PI / 2, depth)}
      {wall([half, CEILING / 2, cz], -Math.PI / 2, depth)}
      <Instanced geometry={plane} matrices={lights}>
        <meshBasicMaterial color="#ffffff" />
      </Instanced>
      {Array.from({ length: data.rows }, (_, row) => (
        <mesh
          key={row}
          position={[0, RACK_H + 0.4, rowZ(row) + frontSign(row) * 0.1]}
          rotation={[0, frontSign(row) > 0 ? 0 : Math.PI, 0]}
        >
          <planeGeometry args={[0.8, 0.2]} />
          <meshBasicMaterial map={labelTexture(`ROW ${row + 1}`)} />
        </mesh>
      ))}
    </group>
  );
}

interface WalkerProps {
  data: SceneData;
  goto: GoTo | null;
  poseRef: RefObject<Pose>;
  frontsRef: RefObject<THREE.Group | null>;
  onHover: (key: string | null) => void;
  onOpen: (key: string) => void;
}

function Walker({ data, goto, poseRef, frontsRef, onHover, onOpen }: WalkerProps) {
  const { camera, raycaster } = useThree();
  const keys = useRef<Record<string, boolean>>({});
  const crouched = useRef(false);
  const tick = useRef(0);
  const lastQ = useRef(new THREE.Quaternion());
  const freeze = useRef<{ until: number; q: THREE.Quaternion } | null>(null);
  const hover = useRef<string | null>(null);
  const hoverCb = useRef(onHover);
  const openCb = useRef(onOpen);
  const per = data.racksPerRow;
  const half = halfWidth(per);
  const zMax = zMaxOf(data.rows);
  const solid = useMemo(() => data.racks.map((r) => ({ x: worldX(r.row, rackX(r.rack, per)), z: rowZ(r.row) })), [data, per]);

  useEffect(() => {
    hoverCb.current = onHover;
    openCb.current = onOpen;
  }, [onHover, onOpen]);

  useEffect(() => {
    if (data.rows >= 2) {
      camera.position.set(-half + 1.2, EYE, (rowZ(0) + rowZ(1)) / 2);
      camera.rotation.set(0, -Math.PI / 2, 0);
    } else {
      camera.position.set(0, EYE, rowZ(0) + 2.9);
      camera.rotation.set(0, 0, 0);
    }
    const inCard = (e: KeyboardEvent) => e.target instanceof Element && e.target.closest("[data-ui-card]") !== null;
    const down = (e: KeyboardEvent) => {
      if (inCard(e)) return;
      keys.current[e.code] = true;
      if ((e.code === "ControlLeft" || e.code === "ControlRight") && !e.repeat) {
        crouched.current = !crouched.current;
      }
    };
    const up = (e: KeyboardEvent) => (keys.current[e.code] = false);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [camera, data.rows, half]);

  useEffect(() => {
    if (!goto) return;
    if (goto.key) {
      const t = targetOf(data, goto.key);
      if (!t) return;
      camera.position.set(t.x, crouched.current ? CROUCH_EYE : EYE, rowZ(t.row) + frontSign(t.row) * (RACK_D / 2 + 0.95));
      camera.lookAt(t.x, t.y, rowZ(t.row));
    } else if (goto.x !== undefined && goto.z !== undefined) {
      camera.position.x = THREE.MathUtils.clamp(goto.x, -half + 0.3, half - 0.3);
      camera.position.z = THREE.MathUtils.clamp(goto.z, Z_MIN + 0.3, zMax - 0.3);
    }
    lastQ.current.copy(camera.quaternion);
    freeze.current = null;
  }, [goto, data, camera, half, zMax]);

  useEffect(() => {
    const hold = (ms: number) => {
      freeze.current ??= { until: performance.now() + ms, q: lastQ.current.clone() };
    };
    const click = () => {
      if (document.pointerLockElement && hover.current) {
        hold(400);
        openCb.current(hover.current);
      }
    };
    // Chrome sends a huge movementX when pointer lock is released or taken; drop it before the controls see it.
    const move = (e: MouseEvent) => {
      const f = freeze.current;
      if ((f && performance.now() < f.until) || Math.abs(e.movementX) > 300 || Math.abs(e.movementY) > 300) {
        e.stopImmediatePropagation();
      }
    };
    const lockChange = () => hold(300);
    window.addEventListener("click", click);
    window.addEventListener("mousemove", move, true);
    document.addEventListener("pointerlockchange", lockChange);
    return () => {
      window.removeEventListener("click", click);
      window.removeEventListener("mousemove", move, true);
      document.removeEventListener("pointerlockchange", lockChange);
    };
  }, []);

  useFrame((_, dt) => {
    const f = freeze.current;
    if (f && performance.now() < f.until) {
      camera.quaternion.copy(f.q);
    } else {
      freeze.current = null;
      lastQ.current.copy(camera.quaternion);
    }
    const k = keys.current;
    const speed = (crouched.current ? 0.8 : k.ShiftLeft ? 4 : 1.8) * Math.min(dt, 0.05);
    const fwd = new THREE.Vector3();
    camera.getWorldDirection(fwd);
    fwd.y = 0;
    fwd.normalize();
    const right = new THREE.Vector3().crossVectors(fwd, new THREE.Vector3(0, 1, 0));
    const step = new THREE.Vector3();
    if (k.KeyW || k.ArrowUp) step.add(fwd);
    if (k.KeyS || k.ArrowDown) step.sub(fwd);
    if (k.KeyD || k.ArrowRight) step.add(right);
    if (k.KeyA || k.ArrowLeft) step.sub(right);
    step.multiplyScalar(speed);

    const blocked = (x: number, z: number) =>
      solid.some((s) => Math.abs(x - s.x) < RACK_W / 2 + 0.25 && Math.abs(z - s.z) < RACK_D / 2 + 0.25);
    const nx = THREE.MathUtils.clamp(camera.position.x + step.x, -half + 0.3, half - 0.3);
    const nz = THREE.MathUtils.clamp(camera.position.z + step.z, Z_MIN + 0.3, zMax - 0.3);
    if (!blocked(nx, camera.position.z)) camera.position.x = nx;
    if (!blocked(camera.position.x, nz)) camera.position.z = nz;
    camera.position.y = THREE.MathUtils.damp(camera.position.y, crouched.current ? CROUCH_EYE : EYE, 12, dt);
    poseRef.current = { x: camera.position.x, z: camera.position.z, dx: fwd.x, dz: fwd.z };

    tick.current += dt;
    if (tick.current > 0.1 && frontsRef.current) {
      tick.current = 0;
      raycaster.setFromCamera(new THREE.Vector2(0, 0), camera);
      raycaster.far = 6;
      const hit = raycaster
        .intersectObject(frontsRef.current, true)
        .find((h) => h.instanceId !== undefined && Array.isArray(h.object.userData.ids));
      const key =
        hit && hit.instanceId !== undefined ? ((hit.object.userData.ids as string[])[hit.instanceId] ?? null) : null;
      if (key !== hover.current) {
        hover.current = key;
        hoverCb.current(key);
      }
    }
  });

  return null;
}

export function DatacenterScene({ data, focus, beacon, goto, poseRef, onHover, onOpen }: SceneProps) {
  const frontsRef = useRef<THREE.Group>(null);
  return (
    <Canvas camera={{ fov: 70, near: 0.05, far: 90 }} dpr={[1, 1.5]} gl={{ antialias: true }}>
      <color attach="background" args={["#0c0d10"]} />
      <fog attach="fog" args={["#0c0d10", 14, 45]} />
      <ambientLight intensity={0.9} />
      <hemisphereLight args={["#ffffff", "#666677", 0.9]} />
      <Environment resolution={128}>
        <Lightformer form="rect" intensity={2} position={[0, 4, 0]} scale={[10, 6, 1]} rotation-x={Math.PI / 2} />
        <Lightformer form="rect" intensity={1} position={[0, 1.5, -5]} scale={[10, 3, 1]} />
      </Environment>
      <Room data={data} />
      <Racks data={data} />
      <Hardware data={data} frontsRef={frontsRef} />
      <Cables data={data} focus={focus} />
      <Beacon data={data} target={beacon} />
      <Walker data={data} goto={goto} poseRef={poseRef} frontsRef={frontsRef} onHover={onHover} onOpen={onOpen} />
      <PointerLockControls />
    </Canvas>
  );
}
