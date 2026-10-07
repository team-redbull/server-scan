import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getMe, login, logout } from "@/api/auth";
import { queryKeys } from "@/api/queryKeys";

/** Whether login is required, and who's logged in if so — the SPA's one
 * source of truth on boot (`GET /api/v1/auth/me`). */
export function useAuth() {
  return useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: getMe,
  });
}

/** Whether the caller may see the audit trail (Events, a server's History):
 * admins and auditors, never a viewer (docs/adr/0043). False until `/auth/me`
 * resolves, so nothing audit-shaped flashes up for a viewer. The API
 * enforces the same rule with a 403; this only decides what is shown. */
export function useCanReadAudit(): boolean {
  const { data: me } = useAuth();
  return me?.role === "ADMIN" || me?.role === "AUDITOR";
}

export function useLoginMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      login(username, password),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.auth.me() });
    },
  });
}

export function useLogoutMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.auth.me() });
    },
  });
}
