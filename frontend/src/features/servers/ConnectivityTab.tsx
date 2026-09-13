import { LinkStateBadge } from "@/components/LinkStateBadge";
import type { ConnectivityAttachment, ConnectivityDetail } from "@/types/server";

interface FabricGroup {
  fabric: string | null;
  attachments: ConnectivityAttachment[];
}

/** Groups attachments by `fabric` in first-seen order; `null` goes into a
 * trailing "Other" group. */
function groupByFabric(attachments: ConnectivityAttachment[]): FabricGroup[] {
  const order: string[] = [];
  const groups = new Map<string, ConnectivityAttachment[]>();
  const other: ConnectivityAttachment[] = [];

  for (const attachment of attachments) {
    if (attachment.fabric === null) {
      other.push(attachment);
      continue;
    }
    const existing = groups.get(attachment.fabric);
    if (existing) {
      existing.push(attachment);
    } else {
      groups.set(attachment.fabric, [attachment]);
      order.push(attachment.fabric);
    }
  }

  const result: FabricGroup[] = order.map((fabric) => ({
    fabric,
    attachments: groups.get(fabric) ?? [],
  }));

  if (other.length > 0) {
    result.push({ fabric: null, attachments: other });
  }

  return result;
}

export function ConnectivityTab({ connectivity }: { connectivity: ConnectivityDetail | undefined }) {
  const attachments = connectivity?.attachments ?? [];

  if (attachments.length === 0) {
    return <p className="text-gray-500">No connectivity data.</p>;
  }

  // Cabled uplinks only — a port and its vNICs report the same `fabric`
  // (docs/cisco-collectors.md, "PHYSICAL" vs "VNIC").
  const physical = attachments.filter((a) => a.interface_kind === "PHYSICAL");

  if (physical.length === 0) {
    return <p className="text-gray-500">No physical fabric connections reported.</p>;
  }

  const groups = groupByFabric(physical);

  return (
    <div className="space-y-6">
      {connectivity && (
        <p className="text-sm text-gray-500">
          {connectivity.facts.fabric_paths_up}/{connectivity.facts.fabric_paths_total} fabric paths up
        </p>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {groups.map((group) => (
          <FabricCard key={group.fabric ?? "__other__"} group={group} />
        ))}
      </div>
    </div>
  );
}

function FabricCard({ group }: { group: FabricGroup }) {
  const title = group.fabric ?? "Other";

  return (
    <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700" data-testid="fabric-group">
      <h3 className="text-sm font-semibold">Fabric {title}</h3>
      <ul className="mt-3 space-y-3">
        {group.attachments.map((attachment, index) => (
          <li
            key={`${attachment.fabric_id ?? attachment.fabric_name ?? "fi"}-${attachment.server_port ?? index}`}
            className="rounded border border-gray-100 p-3 text-sm dark:border-gray-800"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">
                {attachment.fabric_name ?? attachment.fabric_id ?? "Unnamed interconnect"}
              </span>
              <LinkStateBadge state={attachment.oper_state} />
            </div>
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600 dark:text-gray-400">
              {attachment.fabric_model && <DetailRow label="Model" value={attachment.fabric_model} />}
              {attachment.fabric_serial && <DetailRow label="Serial" value={attachment.fabric_serial} />}
              {attachment.server_interface && (
                <DetailRow label="Server interface" value={attachment.server_interface} />
              )}
              {attachment.server_port && <DetailRow label="Server port" value={attachment.server_port} />}
              {attachment.fabric_port && <DetailRow label="Fabric port" value={attachment.fabric_port} />}
              <DetailRow label="Admin state" value={attachment.admin_state} />
              {attachment.speed_mbps != null && (
                <DetailRow label="Speed" value={`${attachment.speed_mbps} Mbps`} />
              )}
            </dl>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-gray-400 dark:text-gray-500">{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
