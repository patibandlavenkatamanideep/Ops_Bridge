import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, apiAs, tenantPath, type QueuePage, type QueueViewCounts } from "../api/client";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { StatusBadge, formatAge, statusLabel } from "../components/StatusBadge";
import { useSession } from "../session";

const STATUSES = [
  "new",
  "collecting_evidence",
  "investigating",
  "needs_information",
  "awaiting_review",
  "follow_up_pending",
  "resolved",
  "closed_without_resolution",
];

// Built-in views; each lives in the URL (?view=…), so it can be bookmarked or shared.
const VIEWS: { id: string; label: string; permission?: string }[] = [
  { id: "all", label: "All cases" },
  { id: "mine", label: "Mine", permission: "case.assign" },
  { id: "unassigned", label: "Unassigned", permission: "case.assign" },
  { id: "awaiting_approval", label: "Awaiting approval", permission: "proposal.read" },
  { id: "needs_review", label: "Needs review", permission: "operation.read" },
];

export function CaseQueue() {
  const { tenant = "" } = useParams();
  const { can } = useSession();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") ?? "all";
  const status = params.get("status") ?? "";
  const sort = params.get("sort") === "oldest" ? "oldest" : "newest";
  const [search, setSearch] = useState(params.get("q") ?? "");
  const term = useDebounced(search.trim(), 300); // one request per pause in typing, not per key
  useEffect(() => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (term) next.set("q", term);
      else next.delete("q");
      return next;
    }, { replace: true });
  }, [term, setParams]);
  function choose(key: string, value: string, fallback: string) {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value && value !== fallback) next.set(key, value);
      else next.delete(key);
      return next;
    });
  }
  const views = VIEWS.filter((v) => !v.permission || can(tenant, v.permission));
  const counts = useQuery<QueueViewCounts, ApiError>({
    queryKey: ["case-views", tenant],
    queryFn: ({ signal }) => apiAs("QueueViewCounts", tenantPath(tenant, "/cases/views"), { signal }),
  });
  const query = useInfiniteQuery<QueuePage, ApiError>({
    queryKey: ["cases", tenant, view, status, sort, term],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const q = new URLSearchParams({ limit: "25" });
      if (view !== "all") q.set("view", view);
      if (status) q.set("status", status);
      if (sort !== "newest") q.set("sort", sort);
      if (term) q.set("q", term);
      if (pageParam) q.set("cursor", pageParam as string);
      return apiAs("QueuePage", tenantPath(tenant, `/cases?${q}`), { signal });
    },
    getNextPageParam: (last) => last.next_cursor,
  });

  const items = query.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <section aria-labelledby="queue-title">
      <div className="page-header">
        <h1 id="queue-title">Case queue</h1>
        {can(tenant, "case.create") && (
          <Link className="button primary" to={`/t/${tenant}/cases/new`}>
            New case
          </Link>
        )}
      </div>
      <nav aria-label="Queue views" className="views">
        {views.map((v) => {
          const n = (counts.data as Record<string, number> | undefined)?.[v.id];
          return (
            <button key={v.id} type="button" className={`link-button ${v.id === view ? "current" : ""}`}
                    aria-pressed={v.id === view} onClick={() => choose("view", v.id, "all")}>
              {v.label}
              {typeof n === "number" ? ` (${n})` : ""}
            </button>
          );
        })}
      </nav>
      <div className="filters">
        <label>
          Search{" "}
          <input type="search" value={search} maxLength={100} placeholder="Case, invoice or PO number"
                 onChange={(e) => setSearch(e.target.value)} />
        </label>{" "}
        <label>
          Status{" "}
          <select value={status} onChange={(e) => choose("status", e.target.value, "")}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
        </label>{" "}
        <label>
          Order{" "}
          <select value={sort} onChange={(e) => choose("sort", e.target.value, "newest")}>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        </label>
      </div>
      {query.isPending ? (
        <Loading label="Loading cases" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title="No cases">
          <p>
            {view !== "all"
              ? "Nothing in this view right now."
              : status
                ? "No cases have this status."
                : "Blocked invoices you can see will appear here."}
          </p>
        </EmptyState>
      ) : (
        <>
          <table className="table">
            <caption className="visually-hidden">Cases, {sort === "oldest" ? "oldest" : "newest"} first</caption>
            <thead>
              <tr>
                <th scope="col">Case</th>
                <th scope="col">Discrepancy</th>
                <th scope="col">Owner</th>
                <th scope="col">Age</th>
                <th scope="col">Evidence</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id}>
                  <td>
                    <Link to={`/t/${tenant}/cases/${c.id}`}>
                      <span className="mono">{c.case_number}</span> {c.title}
                    </Link>
                  </td>
                  <td>{c.discrepancy_types.length ? c.discrepancy_types.map(statusLabel).join(", ") : "—"}</td>
                  <td>
                    {c.owner ? (c.owner_active ? c.owner : `${c.owner} (no longer an investigator)`) : "Unassigned"}
                    {c.assignee_group && <div className="muted small">{c.assignee_group}</div>}
                  </td>
                  <td>{formatAge(c.age_seconds)}</td>
                  <td>{statusLabel(c.evidence_readiness)}</td>
                  <td>
                    <StatusBadge status={c.status} />
                    {c.awaiting_approval && <span className="badge">Awaiting approval</span>}
                    {c.needs_review && <span className="badge">Needs review</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {query.hasNextPage && (
            <button
              type="button"
              className="button secondary"
              onClick={() => query.fetchNextPage()}
              disabled={query.isFetchingNextPage}
            >
              {query.isFetchingNextPage ? "Loading…" : "Load more"}
            </button>
          )}
        </>
      )}
    </section>
  );
}

function useDebounced<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}
