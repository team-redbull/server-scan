import { apiFetch } from "@/api/client";

export type Role = "ADMIN" | "VIEWER";

export interface Me {
  login_required: boolean;
  authenticated: boolean;
  username: string | null;
  role: Role | null;
}

export interface LoginResult {
  username: string;
  role: Role;
}

export function getMe(): Promise<Me> {
  return apiFetch<Me>("/api/v1/auth/me");
}

export function login(username: string, password: string): Promise<LoginResult> {
  return apiFetch<LoginResult>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function logout(): Promise<void> {
  return apiFetch<void>("/api/v1/auth/logout", { method: "POST" });
}
