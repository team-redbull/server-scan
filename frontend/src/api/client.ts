/** Thin fetch wrapper: every non-2xx response is an RFC 9457 problem-details
 * body (`app.exception_handlers`), parsed into a typed `ApiError`. */

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  code: string;
  request_id: string | null;
  details: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly problem: ProblemDetails;

  constructor(problem: ProblemDetails) {
    super(problem.detail);
    this.name = "ApiError";
    this.problem = problem;
  }
}

// Same-origin in production; Vite's proxy (vite.config.ts) covers dev.
const API_BASE = "";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // `HeadersInit` may be an object, a `Headers` or tuples; only the first is
  // spreadable, so merge through `Headers`, which normalizes all three.
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    const problem = (await response.json().catch(() => null)) as ProblemDetails | null;
    if (problem) {
      throw new ApiError(problem);
    }
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
