import { QueryClient } from "@tanstack/react-query";

/** Shared TanStack Query client. `staleTime` above zero suits data that
 * changes per ingestion run, not per second; a failed request here is far
 * more likely a real 4xx/5xx than a blip, so retries stay low. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
