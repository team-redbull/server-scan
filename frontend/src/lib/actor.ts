import type { Actor } from "@/types/events";

export type ActorKind = "user" | "token" | "system";

const TOKEN_ID = /^api-token-(.+)$/;

/** Human name for an actor id alone (a maintenance `created_by`): the
 * static API tokens read "API token (admin)", anything else is the id. */
export function actorIdLabel(id: string): string {
  const m = TOKEN_ID.exec(id);
  return m ? `API token (${m[1] ?? ""})` : id;
}

/** How an actor is shown everywhere: the text, and the chip kind beside it. */
export function actorLabel(actor: Actor): { label: string; kind: ActorKind } {
  if (actor.type === "SYSTEM") {
    return { label: `System (${actor.id})`, kind: "system" };
  }
  if (actor.type === "TOKEN") {
    return { label: actorIdLabel(actor.id), kind: "token" };
  }
  return { label: actor.display ?? actor.id, kind: "user" };
}
