import { isRouteErrorResponse, Link, useRouteError } from "react-router";

/**
 * Catches a render throw anywhere in the routed tree. Without it
 * react-router's data router unmounts the whole app on any render error
 * (commit 1a896af was one such crash).
 */
export function RouteErrorBoundary() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : "Something went wrong.";

  return (
    <main className="mx-auto max-w-2xl px-8 py-16 text-center">
      <h1 className="text-lg font-semibold text-[var(--text-primary)]">Something went wrong</h1>
      <p role="alert" className="mt-2 text-sm text-[var(--text-secondary)]">
        {message}
      </p>
      <Link
        to="/"
        className="mt-6 inline-block rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1.5 text-sm text-[var(--text-primary)] hover:border-[var(--border-strong)]"
      >
        Back to Sites
      </Link>
    </main>
  );
}
