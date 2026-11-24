import type { ReactNode } from "react";

interface SectionProps {
  title: string;
  children: ReactNode;
}

function Section({ title, children }: SectionProps) {
  return (
    <section className="mt-8">
      <h2 className="text-base font-semibold text-[var(--text-primary)]">
        {title}
      </h2>
      <div className="mt-2 space-y-2 text-sm leading-relaxed text-[var(--text-secondary)]">
        {children}
      </div>
    </section>
  );
}

function Bullets({ items }: { items: ReactNode[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5">
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );
}

function Frame({ slug, title }: { slug: string; title: string }) {
  return (
    <iframe
      title={title}
      src={`/architecture/${slug}.html?theme=dark`}
      className="mt-4 h-[70vh] w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)]"
    />
  );
}

/** A plain-language tour of the platform for someone who has never seen it:
 * what it is for, the five steps from a vendor manager to the UI, how health,
 * staleness, pruning and events work, and what else talks to it. The
 * per-collector diagrams on the other tabs go deeper. No counts or intervals
 * that the diagrams own: those drift. */
export function HowItWorks() {
  return (
    <div className="max-w-4xl pb-8">
      <p className="mt-4 text-sm leading-relaxed text-[var(--text-secondary)]">
        Server Scan is the inventory of our physical servers. It answers three
        questions: what do we have, is it healthy, and is it free to use?
      </p>

      <Section title="The problem">
        <p>
          Our servers come from several vendors, and each vendor has its own
          management system (Cisco UCS and Intersight, Dell OpenManage, HPE
          OneView, or plain Redfish). Each has a different API and different
          names for the same things, so nobody can ask one question across all
          of them.
        </p>
      </Section>

      <Section title="The flow, in five steps">
        <ol className="list-decimal space-y-1 pl-5">
          <li>
            <strong className="text-[var(--text-primary)]">Collect.</strong> One
            scheduled job per vendor logs into that vendor&apos;s manager and
            reads its servers. It only reads, with one exception: restarting a
            stuck Dell iDRAC.
          </li>
          <li>
            <strong className="text-[var(--text-primary)]">Normalize.</strong>{" "}
            Every vendor&apos;s data is mapped into one common server shape:
            CPU, memory, disks, NICs, GPUs, power supplies, BMC address and
            site.
          </li>
          <li>
            <strong className="text-[var(--text-primary)]">Ingest.</strong> The
            platform stores each server in MongoDB, the single source of truth.
            A server whose content did not change is not rewritten.
          </li>
          <li>
            <strong className="text-[var(--text-primary)]">Judge.</strong>{" "}
            Classification rules give each server a role and a site. A health
            engine applies policies to the stored facts and gives the server a
            verdict: HEALTHY, WARNING, MAJOR or CRITICAL.
          </li>
          <li>
            <strong className="text-[var(--text-primary)]">Serve.</strong> The
            REST API and the web UI read from MongoDB, with Redis as a cache:
            the fleet table, the server page, events and the rules page.
          </li>
        </ol>
        <Frame
          slug="how-it-works-flow"
          title="How Server Scan works, collect to serve"
        />
      </Section>

      <Section title="Health: how a verdict is decided">
        <Bullets
          items={[
            'Each server\'s stored data is reduced to plain facts, such as "1 virtual drive degraded", "2 NICs down" or "BMC unreachable".',
            "Policies are rules over those facts. Each has a condition, a severity (WARNING, MAJOR or CRITICAL), a category (storage, RAID, network, power, GPU, name and so on) and a one-line reason. Example: virtual drive degraded gives MAJOR.",
            "A policy can be limited to certain collectors, because vendors do not all report the same things.",
            "The server's overall health is the worst result across its categories. The reasons are shown next to the badge, so you can see why.",
            "A category with nothing read is UNKNOWN and its policies are skipped. A failed read is never turned into healthy, and UNKNOWN is never counted as a failure.",
            "The built-in policies are seeded by the platform. Admins can adjust them on the Rules page, and re-evaluate a server on demand.",
          ]}
        />
      </Section>

      <Section title="Stale: is this data still fresh?">
        <Bullets
          items={[
            'Every collector run stamps the servers it read. A server that no collector has read successfully for the stale window (12 hours by default, INVENTORY_STALE_AFTER_SECONDS) gets a Stale chip and an age, for example "Stale 20h". The inventory can filter on it.',
            "Stale is a warning about the data, not about the hardware. It means: do not trust this row until a collector sees it again.",
            "A Prometheus gauge counts stale servers per collector. That is how you notice that a collector stopped running.",
          ]}
        />
      </Section>

      <Section title="Pruning: how servers leave the inventory">
        <Bullets
          items={[
            "A stale server is kept, so a temporary outage never loses a server.",
            "A separate prune job removes servers that no manager has listed for the prune window (24 hours by default, INVENTORY_PRUNE_AFTER_SECONDS).",
            "It has safety brakes. Every manager of that type must have had a clean, recent run, so a dead or half-failed collector cannot make servers look gone. A pass that would delete more than a fifth of one collector's servers is refused. A dead KubeVirt cluster holds pruning back.",
            "An admin can also delete a single server by hand from the UI.",
            "Every deletion is recorded as an event.",
          ]}
        />
      </Section>

      <Section title="Events: the audit trail">
        <Bullets
          items={[
            "Every meaningful change is an event: collector ingests, maintenance and reservation changes, reclassification, health recalculation, logins, an admin running a job, SERVER_PRUNED for a deleted server, an iDRAC reset, and a cluster that stopped reporting.",
            "Each event records who did it (a person, a token, or SYSTEM), what changed and when.",
            "Admins and auditors can read events. Viewers cannot.",
            "A weekly job removes events older than the retention period (180 days by default) and records the purge as an event of its own.",
          ]}
        />
      </Section>

      <Section title="The life of a server">
        <p>
          One path through all of it: a Cisco server loses a mirror disk and
          turns MAJOR. If nobody fixes it and its manager later stops listing
          it, it goes Stale and is eventually pruned, leaving an event behind.
        </p>
        <Frame
          slug="server-lifecycle"
          title="The life of a server, healthy to pruned"
        />
      </Section>

      <Section title="What else talks to it">
        <Bullets
          items={[
            "Provisioning tools call POST /servers/available to claim a free server. It is locked and re-checked live at its manager before it is handed out; GET only reads the inventory. An admin's Fetch now re-reads one server the same way.",
            "OpenShift jobs report which servers belong to which cluster, so a server shows as Installed or Available.",
            "People sign in with AD and get one of three roles: admin, auditor or viewer. Only admins can change anything.",
            "Operations: Prometheus gauges, the audit trail, and a Run job button for admins.",
          ]}
        />
      </Section>

      <Section title="Where to go next">
        <p>
          The tabs above go deeper: the full-flow diagram first, then one tab
          per collector, then the nodes-status jobs for OpenShift.
        </p>
      </Section>
    </div>
  );
}
