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
