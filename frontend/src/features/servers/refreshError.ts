import { ApiError } from "@/api/client";

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem.detail;
  }
  return error instanceof Error ? error.message : "Failed to fetch the server.";
}
