import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, tenantPath, type QueuePage } from "../api/client";
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

export function CaseQueue() {
  const { tenant = "" } = useParams();
  const { can } = useSession();
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const term = useDebounced(search.trim(), 300); // one request per pause in typing, not per key
  const query = useInfiniteQuery<QueuePage, ApiError>({
    queryKey: ["cases", tenant, status, term],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams({ limit: "25" });
      if (status) params.set("status", status);
      if (term) params.set("q", term);
      if (pageParam) params.set("cursor", pageParam as string);
      return api<QueuePage>(tenantPath(tenant, `/cases?${params}`), { signal });
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
      <div className="filters">
        <label>
          Search{" "}
          <input type="search" value={search} maxLength={100} placeholder="Case, invoice or PO number"
                 onChange={(e) => setSearch(e.target.value)} />
        </label>{" "}
        <label>
          Status{" "}
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {query.isPending ? (
        <Loading label="Loading cases" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title="No cases">
          <p>{status ? "No cases have this status." : "Blocked invoices you can see will appear here."}</p>
        </EmptyState>
      ) : (
        <>
          <table className="table">
            <caption className="visually-hidden">Cases, newest first</caption>
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
                  <td>{c.owner ?? c.assignee_group ?? "Unassigned"}</td>
                  <td>{formatAge(c.age_seconds)}</td>
                  <td>{statusLabel(c.evidence_readiness)}</td>
                  <td>
                    <StatusBadge status={c.status} />
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
