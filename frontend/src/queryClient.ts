import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api/client";

export function createQueryClient(): QueryClient {
  // A 401 anywhere means the session has ended (expired, signed out elsewhere, or ended by the
  // identity provider): checking the session again shows sign-in instead of error panels while
  // the cached session still looks valid. The session query's own 401 is not re-checked.
  const sessionEnded = (error: unknown, key?: readonly unknown[]) => {
    if (error instanceof ApiError && error.status === 401 && key?.[0] !== "me") {
      void client.invalidateQueries({ queryKey: ["me"] });
    }
  };
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError: (error, query) => sessionEnded(error, query.queryKey) }),
    mutationCache: new MutationCache({ onError: (error) => sessionEnded(error) }),
    defaultOptions: {
      queries: {
        retry: (count, error) => error instanceof ApiError && error.retryable && count < 2,
        refetchOnWindowFocus: false,
      },
    },
  });
  return client;
}
