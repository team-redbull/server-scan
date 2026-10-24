import { useEffect, useState } from "react";
import type { MouseEvent, RefObject } from "react";

import type { SceneData } from "@/features/datacenter/model";
import type { Pose } from "@/features/datacenter/world";
import { aislesOf, halfWidth, RACK_D, RACK_W, rackX, rowZ, worldX, Z_MIN, zMaxOf } from "@/features/datacenter/world";

const WIDTH_PX = 230;

/** Top-down plan of the room: click it to teleport; the arrow is where you stand and look. */
export function Minimap({
  data,
  poseRef,
  focusRack,
  onGo,
}: {
  data: SceneData;
  poseRef: RefObject<Pose>;
  focusRack: { row: number; rack: number } | null;
  onGo: (x: number, z: number) => void;
}) {
  const per = data.racksPerRow;
  const half = halfWidth(per);
  const width = half * 2;
  const depth = zMaxOf(data.rows) - Z_MIN;
  const [pose, setPose] = useState<Pose>(poseRef.current);

  useEffect(() => {
    const t = setInterval(() => setPose({ ...poseRef.current }), 120);
    return () => clearInterval(t);
  }, [poseRef]);

  const onClick = (e: MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    onGo(-half + ((e.clientX - r.left) / r.width) * width, Z_MIN + ((e.clientY - r.top) / r.height) * depth);
  };
  const rackRect = (row: number, rack: number, key: string, fill: string) => (
    <rect
      key={key}
      x={worldX(row, rackX(rack, per)) - RACK_W / 2}
      y={rowZ(row) - RACK_D / 2}
      width={RACK_W}
      height={RACK_D}
      fill={fill}
      stroke={focusRack?.row === row && focusRack.rack === rack ? "#ffffff" : "none"}
      strokeWidth={0.12}
    />
  );
  const angle = (Math.atan2(pose.dz, pose.dx) * 180) / Math.PI;

  return (
    <svg
      role="img"
      aria-label="Datacenter map; click to teleport"
      viewBox={`${-half} ${Z_MIN} ${width} ${depth}`}
      width={WIDTH_PX}
      height={(WIDTH_PX * depth) / width}
      className="cursor-crosshair rounded-md border border-white/20 bg-black/70"
      onClick={onClick}
    >
      {aislesOf(data.rows).map((a) => (
        <rect
          key={`${a.kind}${a.z}`}
          x={-half}
          y={a.z - a.width / 2}
          width={width}
          height={a.width}
          fill={a.kind === "cold" ? "#2a8cff" : "#ff5a1f"}
          opacity={0.35}
        />
      ))}
      {data.racks.map((r) => rackRect(r.row, r.rack, `r${r.row}-${r.rack}`, "#5b616b"))}
      {Array.from({ length: data.rows }, (_, row) => rackRect(row, per, `fi${row}`, "#38b6ff"))}
      <g transform={`translate(${pose.x} ${pose.z}) rotate(${angle})`}>
        <polygon points="0.9,0 -0.5,0.45 -0.5,-0.45" fill="#ffe14a" stroke="#000" strokeWidth={0.08} />
      </g>
    </svg>
  );
}
