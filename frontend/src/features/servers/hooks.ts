import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { getServer, getServerVmHost, refreshServer } from "@/api/servers";

export function useServerDetailQuery(id: string) {
  return useQuery({
    queryKey: queryKeys.servers.detail(id),
    queryFn: () => getServer(id),
    enabled: id.length > 0,
  });
}

export function useVmHostQuery(id: string) {
  return useQuery({
    queryKey: queryKeys.servers.vmHost(id),
    queryFn: () => getServerVmHost(id),
  });
}

/**
 * Read one server from its vendor manager now (`POST /servers/{id}/refresh`).
 *
 * Returns:
 *   The mutation, taking the server id; the detail cache takes the response at once, and the
 *   rows, sites and the server's history refetch.
 */
export function useRefreshServerMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => refreshServer(id),
    onSuccess: (server) => {
      queryClient.setQueryData(queryKeys.servers.detail(server.id), server);
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.rows() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.events.all });
    },
  });
}
