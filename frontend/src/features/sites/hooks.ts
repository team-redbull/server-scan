import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { listSites } from "@/api/sites";

/** The site list, one query key for every page that names or offers a site. */
export function useSitesQuery() {
  return useQuery({
    queryKey: queryKeys.sites.list(),
    queryFn: () => listSites(),
  });
}
