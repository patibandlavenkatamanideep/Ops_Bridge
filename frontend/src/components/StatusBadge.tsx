const LABELS: Record<string, string> = {
  new: "New",
  collecting_evidence: "Collecting evidence",
  investigating: "Investigating",
  needs_information: "Needs information",
  awaiting_review: "Awaiting review",
  follow_up_pending: "Follow-up pending",
  resolved: "Resolved",
  closed_without_resolution: "Closed (unresolved)",
};

export function statusLabel(status: string): string {
  return LABELS[status] ?? status.replaceAll("_", " ");
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{statusLabel(status)}</span>;
}

export function formatAge(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}
