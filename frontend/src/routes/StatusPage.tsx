import { useQuery } from "@tanstack/react-query";

import { getReadiness } from "@/api/platform";

/** Slice 0's backend-readiness page, kept as a debug page. */
export function StatusPage() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["platform", "readiness"],
    queryFn: getReadiness,
  });

  return (
    <main className="mx-auto max-w-2xl p-8">
      <h1 className="text-2xl font-semibold">Server Inventory Platform</h1>
      <p className="mt-2 text-sm text-gray-500">
        Phase 1 skeleton — inventory table lands in the next slice.
      </p>

      <section className="mt-6 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
        <h2 className="text-sm font-medium uppercase tracking-wide text-gray-500">
          Backend readiness
        </h2>
        {isPending && <p className="mt-2">Checking…</p>}
        {isError && (
          <p className="mt-2 text-red-600">
            Could not reach the API: {error instanceof Error ? error.message : "unknown error"}
          </p>
        )}
        {data && (
          <dl className="mt-2 grid grid-cols-2 gap-2 text-sm">
            <dt className="text-gray-500">status</dt>
            <dd>{data.status}</dd>
            <dt className="text-gray-500">mongo</dt>
            <dd>{data.dependencies.mongo}</dd>
            <dt className="text-gray-500">redis</dt>
            <dd>{data.dependencies.redis}</dd>
          </dl>
        )}
      </section>
    </main>
  );
}
