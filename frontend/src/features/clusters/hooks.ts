import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { decommissionCluster, listClusterJobs } from "@/api/openshift";
import type { MembershipKind } from "@/api/openshift";
import { queryKeys } from "@/api/queryKeys";
import { useIsAdmin } from "@/features/auth/useAuth";

/** Every membership job, with how long since it last ran. Admin only: no request for anyone else. */
export function useClusterJobsQuery() {
  const isAdmin = useIsAdmin();
  return useQuery({
    queryKey: queryKeys.openshift.clusters(),
    queryFn: () => listClusterJobs(),
    enabled: isAdmin,
  });
}

/** Decommission one job: the listings, the fleet rows, the sites and the events refetch. */
export function useDecommissionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, name }: { kind: MembershipKind; name: string }) =>
      decommissionCluster(kind, name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.openshift.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.events.all });
    },
  });
}
