import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api, tenantPath, type SettingsView } from "../api/client";
import { ErrorState, Loading } from "../components/States";
import { statusLabel } from "../components/StatusBadge";
import { useSession } from "../session";

const RETENTION_LABELS: Record<string, string> = {
  raw_documents_days: "Original documents and their extracted text",
  conversation_days: "Case conversation message text",
  model_artifacts_days: "Model requests and responses",
  tool_artifacts_days: "Tool requests and responses",
};

export function TenantSettings() {
  const { tenant = "" } = useParams();
  const query = useQuery<SettingsView, ApiError>({
    queryKey: ["settings", tenant],
    queryFn: ({ signal }) => api<SettingsView>(tenantPath(tenant, "/settings"), { signal }),
  });
  if (query.isPending) return <Loading label="Loading settings" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  // Keyed on the saved values so the form re-initializes after a save.
  return <SettingsBody key={JSON.stringify(query.data.settings)} tenant={tenant} view={query.data} />;
}

function SettingsBody({ tenant, view }: { tenant: string; view: SettingsView }) {
  const { can } = useSession();
  const qc = useQueryClient();
  const canPolicy = can(tenant, "tenant.manage_policy");
  const canConnectors = can(tenant, "tenant.manage_connectors");
  const [modelMode, setModelMode] = useState(view.settings.model_mode);
  const [connector, setConnector] = useState(view.settings.task_connector);
  const [retention, setRetention] = useState<Record<string, string>>(() =>
    Object.fromEntries(view.retention_keys.map((k) => [k, view.settings.retention[k]?.toString() ?? ""])),
  );
  const [saved, setSaved] = useState(false);
  const modes = ["rules_only", ...view.entitlements.model_modes.filter((m) => m !== "rules_only")];

  const save = useMutation<SettingsView, ApiError>({
    mutationFn: () => {
      const body: Record<string, unknown> = {};
      if (canPolicy) {
        body.model_mode = modelMode;
        body.retention = Object.fromEntries(
          Object.entries(retention).filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, Number(v)]),
        );
      }
      if (canConnectors) body.task_connector = connector;
      return api<SettingsView>(tenantPath(tenant, "/settings"), { method: "PUT", body });
    },
    onSuccess: (data) => {
      qc.setQueryData(["settings", tenant], data);
      setSaved(true);
    },
  });
  const sweep = useMutation<{ job_id: string }, ApiError>({
    mutationFn: () => api(tenantPath(tenant, "/retention/run"), { method: "POST" }),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    save.mutate();
  }

  return (
    <section aria-labelledby="settings-title">
      <div className="page-header">
        <h1 id="settings-title">Tenant settings</h1>
      </div>
      <dl className="facts">
        <dt>Organization</dt>
        <dd>{view.tenant.name}</dd>
        <dt>Service tier</dt>
        <dd>{statusLabel(view.tenant.tier)}</dd>
        <dt>Purchasing pack</dt>
        <dd className="mono">{view.tenant.pack_id}</dd>
        <dt>Data</dt>
        <dd>{view.tenant.synthetic_demo ? "Synthetic demo tenant" : "Tenant data"}</dd>
      </dl>
      <p className="muted small">
        The tier decides which features are available. It never grants a person roles, approval authority or access
        to another organization’s data.
      </p>

      <form className="form" onSubmit={submit} aria-label="Tenant settings">
        <fieldset disabled={!canPolicy && !canConnectors}>
          <legend>Investigation and follow-up</legend>
          <label>
            Default investigation mode
            <select value={modelMode} onChange={(e) => setModelMode(e.target.value)} disabled={!canPolicy}>
              {modes.map((m) => (
                <option key={m} value={m}>
                  {statusLabel(m)}
                </option>
              ))}
            </select>
          </label>
          <p className="muted small">
            Modes this tier allows: {modes.map(statusLabel).join(", ")}. Hosted models also need a configured key and
            explicit opt-in on each run.
          </p>
          <label>
            Follow-up task connector
            <select value={connector} onChange={(e) => setConnector(e.target.value)} disabled={!canConnectors}>
              {view.entitlements.external_connectors.map((c) => (
                <option key={c} value={c}>
                  {statusLabel(c)}
                </option>
              ))}
            </select>
          </label>
        </fieldset>
        <fieldset disabled={!canPolicy}>
          <legend>Retention (days; leave blank to keep)</legend>
          {view.retention_keys.map((k) => (
            <label key={k}>
              {RETENTION_LABELS[k] ?? statusLabel(k)}
              <input type="number" inputMode="numeric" min={0} max={3650} value={retention[k] ?? ""}
                     onChange={(e) => setRetention({ ...retention, [k]: e.target.value })} />
            </label>
          ))}
          <p className="muted small">
            Deletion removes stored content. Digests, decisions and audit events stay, so a deleted document can no
            longer be reviewed or replayed. A digest of short or guessable text is not anonymization.
          </p>
        </fieldset>
        {save.isError && <ErrorState error={save.error} />}
        {saved && (
          <p role="status" className="small">
            Settings saved.
          </p>
        )}
        {(canPolicy || canConnectors) && (
          <div className="actions">
            <button type="submit" className="button primary" disabled={save.isPending}>
              Save settings
            </button>
          </div>
        )}
      </form>

      {canPolicy && (
        <section aria-labelledby="sweep-title">
          <h2 id="sweep-title">Run retention now</h2>
          <p className="small">
            Applies the saved retention periods immediately. Deleted content cannot be restored.
          </p>
          <div className="actions">
            <button type="button" className="button secondary" disabled={sweep.isPending || sweep.isSuccess}
                    onClick={() => sweep.mutate()}>
              Run retention sweep
            </button>
          </div>
          {sweep.isSuccess && (
            <p role="status" className="small">
              Retention sweep queued (job <code>{sweep.data.job_id.slice(0, 8)}</code>).
            </p>
          )}
          {sweep.isError && <ErrorState error={sweep.error} />}
        </section>
      )}

      <section aria-labelledby="members-title">
        <h2 id="members-title">Members</h2>
        <table className="table">
          <thead>
            <tr>
              <th scope="col">User</th>
              <th scope="col">Roles</th>
              <th scope="col">Approval scope</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {view.members.map((m) => (
              <tr key={m.username}>
                <th scope="row">
                  {m.display_name ?? m.username} <span className="muted small mono">{m.username}</span>
                </th>
                <td>{m.roles.map(statusLabel).join(", ")}</td>
                <td className="small">{m.approval_scope.length ? m.approval_scope.join(", ") : "—"}</td>
                <td>{statusLabel(m.status)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">
          In this build, memberships and roles come from the seed configuration; there is no in-app member editor.
        </p>
      </section>
    </section>
  );
}
