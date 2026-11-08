import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { getServer, getServerVmHost } from "@/api/servers";

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
