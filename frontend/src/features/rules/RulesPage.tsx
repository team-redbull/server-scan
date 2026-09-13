import { ApiError } from "@/api/client";
import { vendorLabel } from "@/api/sites";
import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import { SEVERITY_ORDER } from "@/components/severity";
import { useClassificationRulesQuery } from "@/features/classification/hooks";
import { useHealthPoliciesQuery } from "@/features/health/hooks";
import { MANAGER_TYPE_LABELS } from "@/types/classification";
import type { ClassificationRuleResponse, RuleScope } from "@/types/classification";
import type { Condition, PolicyScope } from "@/types/health";
import { isAllOf, isAnyOf, isLeaf, isNot } from "@/types/health";
import type { HealthPolicyResponse } from "@/types/health";
import type { HealthSeverity } from "@/types/server";

/** A rule's scope as one string; `(unscoped)` rather than a blank that
 * reads as missing data. */
function scopeSummary(scope: RuleScope): string {
  const parts: string[] = [];
  if (scope.vendor) parts.push(`vendor=${scope.vendor}`);
  if (scope.manager_type) parts.push(`manager=${scope.manager_type}`);
  if (scope.site_id) parts.push(`site=${scope.site_id}`);
  return parts.length > 0 ? parts.join(", ") : "(unscoped)";
}

/** A policy scope as a section heading. */
function policyScopeLabel(scope: PolicyScope): string {
  const parts: string[] = [];
  if (scope.vendor) parts.push(vendorLabel(scope.vendor));
  if (scope.manager_types.length > 0) {
    parts.push(scope.manager_types.map((m) => MANAGER_TYPE_LABELS[m] ?? m).join(", "));
  }
  if (scope.site_id) parts.push(`Site ${scope.site_id}`);
  return parts.length > 0 ? parts.join(" — ") : "General";
}

/** Sections come in specificity order: general, vendor, collector, site. */
function policyScopeRank(scope: PolicyScope): number {
  if (scope.site_id) return 3;
  if (scope.manager_types.length > 0) return 2;
  if (scope.vendor) return 1;
  return 0;
}

interface PolicySection {
  label: string;
  rank: number;
  policies: HealthPolicyResponse[];
}

/** Policies grouped by scope, sections by specificity then label, rows by
 * severity then name. */
function policySections(policies: HealthPolicyResponse[]): PolicySection[] {
  const byLabel = new Map<string, PolicySection>();
  for (const policy of policies) {
    const label = policyScopeLabel(policy.scope);
    const section = byLabel.get(label) ?? {
      label,
      rank: policyScopeRank(policy.scope),
      policies: [],
    };
    section.policies.push(policy);
    byLabel.set(label, section);
  }
  const sections = [...byLabel.values()];
  sections.sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label));
  for (const section of sections) {
    section.policies.sort(
      (a, b) =>
        severityRank(a.severity) - severityRank(b.severity) || a.name.localeCompare(b.name),
    );
  }
  return sections;
}

function severityRank(severity: HealthSeverity): number {
  const index = SEVERITY_ORDER.indexOf(severity);
  return index === -1 ? SEVERITY_ORDER.length : index;
}

/** A health policy's condition as one line of readable text. */
function conditionSummary(condition: Condition): string {
  if (isAllOf(condition)) {
    return condition.all_of.map(conditionSummary).join(" AND ");
  }
  if (isAnyOf(condition)) {
    return `(${condition.any_of.map(conditionSummary).join(" OR ")})`;
  }
  if (isNot(condition)) {
    return `NOT ${conditionSummary(condition.not)}`;
  }
  if (isLeaf(condition)) {
    const parts = [condition.metric, condition.operator];
    if (condition.value != null) parts.push(JSON.stringify(condition.value));
    // COUNT_* without `equals` counts the list's own length.
    if (condition.equals != null) parts.push(`of ${JSON.stringify(condition.equals)}`);
    return parts.join(" ");
  }
  return "—";
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.problem.detail;
  if (error instanceof Error) return error.message;
  return fallback;
}

export function RulesPage() {
  const rulesQuery = useClassificationRulesQuery({ enabled: true });
  const policiesQuery = useHealthPoliciesQuery({ enabled: true });

  const rules = rulesQuery.data?.items ?? [];
  const policies = policiesQuery.data?.items ?? [];

  return (
    <main className="mx-auto max-w-7xl p-8">
      <h1 className="text-2xl font-semibold">Rules &amp; Policies</h1>
      <p className="mt-1 max-w-3xl text-sm text-gray-500">
        The rules and policies this deployment runs, and every deployment
        gets the same set. They are defined in code and seeded at startup,
        not created here — a rule that exists in one estate and not
        another makes two installations that look identical classify the
        same server differently. Anything disabled is not listed, because
        it is not something this deployment does.
      </p>

      <section className="mt-8">
        <h2 className="text-lg font-semibold">Classification rules</h2>
        <p className="mt-1 text-sm text-gray-500">
          Assign an installation type (HOSTED_CLUSTER / MCE / UPI / UNCLASSIFIED)
          from a server&apos;s own fields.
        </p>

        {rulesQuery.isPending && <p className="mt-3 text-gray-500">Loading…</p>}
        {rulesQuery.isError && (
          <p className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
            {errorText(rulesQuery.error, "Failed to load classification rules.")}
          </p>
        )}

        {!rulesQuery.isPending && !rulesQuery.isError && (
          <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-800/50">
                <tr>
                  {["Name", "Type", "Matches", "Priority", "Scope"].map((h) => (
                    <th
                      key={h}
                      scope="col"
                      className="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {rules.map((rule: ClassificationRuleResponse) => (
                  <tr key={rule.id}>
                    <td className="px-3 py-2 font-medium">{rule.name}</td>
                    <td className="px-3 py-2">
                      <Badge>{rule.installation_type}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      <code className="font-mono text-xs">
                        {rule.field} ~ {rule.pattern}
                      </code>
                      {rule.flags.ignore_case && (
                        <span className="ml-2 text-xs text-gray-500">(case-insensitive)</span>
                      )}
                    </td>
                    <td className="px-3 py-2">{rule.priority}</td>
                    <td className="px-3 py-2 text-gray-500">{scopeSummary(rule.scope)}</td>
                  </tr>
                ))}
                {rules.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                      No classification rules are configured.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Health policies</h2>
        <p className="mt-1 text-sm text-gray-500">
          Assign per-category and overall health severity. Within one{" "}
          <code className="font-mono text-xs">policy_key</code> the
          highest-priority enabled policy wins and the rest are shadowed.
        </p>

        {policiesQuery.isPending && <p className="mt-3 text-gray-500">Loading…</p>}
        {policiesQuery.isError && (
          <p className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
            {errorText(policiesQuery.error, "Failed to load health policies.")}
          </p>
        )}

        {!policiesQuery.isPending && !policiesQuery.isError && policies.length === 0 && (
          <p className="mt-3 rounded-lg border border-gray-200 px-3 py-6 text-center text-gray-500 dark:border-gray-700">
            No health policies are configured.
          </p>
        )}

        {!policiesQuery.isPending &&
          !policiesQuery.isError &&
          policySections(policies).map((section) => (
            <div key={section.label} className="mt-6">
              <h3 className="text-base font-semibold">{section.label}</h3>
              {section.rank === 0 && (
                <p className="mt-1 text-sm text-gray-500">
                  Applies to every server, whatever collected it.
                </p>
              )}
              <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
                <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
                  <thead className="bg-gray-50 dark:bg-gray-800/50">
                    <tr>
                      {["Name", "Category", "Severity", "Condition", "policy_key"].map((h) => (
                        <th
                          key={h}
                          scope="col"
                          className="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                    {section.policies.map((policy) => (
                      <tr key={policy.id}>
                        <td className="px-3 py-2 font-medium">{policy.name}</td>
                        <td className="px-3 py-2">{policy.category}</td>
                        <td className="px-3 py-2">
                          <HealthBadge severity={policy.severity} />
                        </td>
                        <td className="px-3 py-2">
                          <code className="font-mono text-xs">
                            {conditionSummary(policy.condition)}
                          </code>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs text-gray-500">
                          {policy.policy_key}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
      </section>
    </main>
  );
}
