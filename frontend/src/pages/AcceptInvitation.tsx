import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, apiAs, type InvitationAccepted } from "../api/client";
import { PlainHeader } from "../components/Layout";
import { EmptyState, ErrorState } from "../components/States";
import { useSession } from "../session";

// An invitation link is /invite#<token>. The fragment never reaches a server, a log or a
// Referer header. It is moved into this tab's sessionStorage before anything renders, so it
// survives the sign-in round trip (which keeps only the path), and removed from the address bar.
const KEY = "opsbridge.invite";
let pending: string | null = null;

export function captureInviteToken(): void {
  if (window.location.pathname !== "/invite" || window.location.hash.length < 2) return;
  pending = window.location.hash.slice(1);
  try {
    sessionStorage.setItem(KEY, pending);
  } catch {
    // Storage unavailable: the token is kept in memory for this page load only.
  }
  window.history.replaceState(null, "", "/invite");
}

function readInviteToken(): string | null {
  if (pending) return pending;
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function clearInviteToken(): void {
  pending = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // nothing stored
  }
}

export function AcceptInvitation() {
  const { me } = useSession();
  const [token] = useState(readInviteToken);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const accept = useMutation<InvitationAccepted, ApiError>({
    mutationFn: () => apiAs("AcceptOut", "/api/v1/invitations/accept", { method: "POST", body: { token } }),
    onSuccess: async (joined) => {
      clearInviteToken();
      await qc.invalidateQueries({ queryKey: ["me"] });
      navigate(`/t/${joined.tenant_slug}/cases`, { replace: true });
    },
  });

  if (!token) {
    return (
      <>
        <PlainHeader />
        <main className="content" id="main">
          <EmptyState title="No invitation found">
            <p>Open the invitation link you were sent, in this browser tab.</p>
          </EmptyState>
        </main>
      </>
    );
  }
  return (
    <>
      <PlainHeader />
      <main className="content" id="main">
        <h1>Accept an invitation</h1>
        <p>
          You are signed in as <strong>{me.display_name}</strong> <span className="muted mono">{me.username}</span>.
          An invitation works once, before it expires, and only for the email address it was sent to, if your
          identity provider has verified that address. Signed in with another account? Sign out, then open the link
          again.
        </p>
        {accept.isError && <ErrorState error={accept.error} />}
        <div className="actions">
          <button type="button" className="button primary" disabled={accept.isPending}
                  onClick={() => accept.mutate()}>
            Accept invitation
          </button>
        </div>
      </main>
    </>
  );
}
