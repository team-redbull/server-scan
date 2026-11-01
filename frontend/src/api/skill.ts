import { apiFetch } from "@/api/client";

export interface SkillFile {
  path: string;
  content: string;
}

export interface Skill {
  name: string;
  description: string;
  when_to_use: string;
  allowed_tools: string;
  version: string;
  updated_at: string;
  files: SkillFile[];
}

export function getSkill(): Promise<Skill> {
  return apiFetch<Skill>("/api/v1/skill");
}

/** The zip's URL, with this origin written into `BASE_URL` so only the token is left to fill in. */
export function skillDownloadUrl(origin: string): string {
  return `/api/v1/skill/download?base_url=${encodeURIComponent(origin)}`;
}
