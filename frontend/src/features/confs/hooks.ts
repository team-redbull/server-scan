import { useQuery } from "@tanstack/react-query";

import { listConfs } from "@/api/confs";
import { queryKeys } from "@/api/queryKeys";

/** Every conf; the collection is small, so one unpaginated query. */
export function useConfsQuery() {
  return useQuery({
    queryKey: queryKeys.confs.list(),
    queryFn: () => listConfs(),
  });
}
