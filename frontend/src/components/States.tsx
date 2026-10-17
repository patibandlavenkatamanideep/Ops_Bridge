import type { ReactNode } from "react";
import { ApiError } from "../api/client";

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> {label}…
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const e = error instanceof ApiError ? error : null;
  const status = e?.status ?? (error as { status?: number } | null)?.status;
  const denied = status === 403;
  const missing = status === 404;
  const title = denied ? "You don't have access to this" : missing ? "Not found" : "Something went wrong";
  return (
    <div className={`state ${denied ? "state-denied" : "state-error"}`} role="alert">
      <h3>{title}</h3>
      <p>{e?.message ?? (missing ? "The requested item was not found." : "An unexpected error occurred.")}</p>
      {e?.correlationId && (
        <p className="muted small">
          Reference: <code>{e.correlationId}</code>
        </p>
      )}
      {onRetry && (e ? e.retryable : !missing && !denied) && (
        <button type="button" className="button secondary" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}
