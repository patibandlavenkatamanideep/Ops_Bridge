import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";
import { ApiError, apiAs, setCsrfToken, type Me, type Membership } from "./api/client";

interface SessionValue {
  me: Me;
  membership: (tenant: string) => Membership | undefined;
  can: (tenant: string, permission: string) => boolean;
}

const SessionContext = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}

export function useMe() {
  return useQuery<Me, ApiError>({
    queryKey: ["me"],
    queryFn: async () => {
      const me = await apiAs("MeOut", "/api/v1/me");
      setCsrfToken(me.csrf_token);
      return me;
    },
    retry: (count, err) => err.status !== 401 && count < 2,
    staleTime: 60_000,
  });
}

export function SessionProvider({ me, children }: { me: Me; children: ReactNode }) {
  const membership = (tenant: string) => me.memberships.find((m) => m.tenant_slug === tenant);
  // UI hints only. The server enforces every permission; hidden buttons are not authorization.
  const can = (tenant: string, permission: string) =>
    membership(tenant)?.permissions.includes(permission) ?? false;
  return <SessionContext.Provider value={{ me, membership, can }}>{children}</SessionContext.Provider>;
}
