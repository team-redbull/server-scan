import { apiFetch } from "@/api/client";

export interface PersonalToken {
  username: string;
  role: "VIEWER" | "AUDITOR";
  prefix: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
}

export interface CreatedToken {
  token: string;
  info: PersonalToken;
}

export function getMyToken(): Promise<PersonalToken | null> {
  return apiFetch<PersonalToken | null>("/api/v1/auth/token");
}

export function createMyToken(): Promise<CreatedToken> {
  return apiFetch<CreatedToken>("/api/v1/auth/token", { method: "POST" });
}

export function revokeMyToken(): Promise<void> {
  return apiFetch<void>("/api/v1/auth/token", { method: "DELETE" });
}

/** The `SKILL.md` line the token goes on, named for the role it holds. */
export function tokenLineName(role: PersonalToken["role"]): "VIEWER_TOKEN" | "AUDITOR_TOKEN" {
  return role === "VIEWER" ? "VIEWER_TOKEN" : "AUDITOR_TOKEN";
}
