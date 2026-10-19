import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ApiError, api, tenantPath, type Message, type MessageList } from "../../api/client";
import { EmptyState, ErrorState, Loading } from "../../components/States";
import { useSession } from "../../session";

/** Case notes. A correction is a new message that supersedes the old one; deletion removes the text but keeps
 *  who, when, why and the content digest. */
export function ConversationPanel({ tenant, caseId }: { tenant: string; caseId: string }) {
  const { can, me } = useSession();
  const qc = useQueryClient();
  const key = ["messages", tenant, caseId];
  const list = useQuery<MessageList, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => api<MessageList>(tenantPath(tenant, `/cases/${caseId}/messages`), { signal }),
  });
  const [correcting, setCorrecting] = useState<Message | null>(null);

  if (list.isPending) return <Loading label="Loading conversation" />;
  if (list.isError) return <ErrorState error={list.error} onRetry={() => list.refetch()} />;
  const items = list.data.items;
  const supersededBy = new Map(items.filter((m) => m.supersedes_id).map((m) => [m.supersedes_id!, m.seq]));
  const isAdmin = can(tenant, "tenant.manage_members");

  return (
    <section aria-labelledby="conversation-title">
      <h2 id="conversation-title" className="visually-hidden">Conversation</h2>
      {items.length === 0 ? (
        <EmptyState title="No notes yet">
          <p>Record calls, supplier replies and decisions that are not in a source document.</p>
        </EmptyState>
      ) : (
        <ol className="messages">
          {items.map((m) => (
            <MessageItem key={m.id} tenant={tenant} caseId={caseId} message={m}
                         supersededBy={supersededBy.get(m.id) ?? null}
                         canCorrect={can(tenant, "conversation.write") && m.author_id === me.user_id && !m.deleted_at}
                         canDelete={!m.deleted_at && (m.author_id === me.user_id || isAdmin)}
                         onCorrect={() => setCorrecting(m)} />
          ))}
        </ol>
      )}
      {can(tenant, "conversation.write") && (
        <Composer key={correcting?.id ?? "new"} tenant={tenant} caseId={caseId} correcting={correcting}
                  onDone={() => {
                    setCorrecting(null);
                    void qc.invalidateQueries({ queryKey: key });
                  }} />
      )}
    </section>
  );
}

function MessageItem({ tenant, caseId, message: m, supersededBy, canCorrect, canDelete, onCorrect }: {
  tenant: string; caseId: string; message: Message; supersededBy: number | null; canCorrect: boolean;
  canDelete: boolean; onCorrect: () => void }) {
  const qc = useQueryClient();
  const [deleting, setDeleting] = useState(false);
  const [reason, setReason] = useState("");
  const del = useMutation<Message, ApiError>({
    mutationFn: () =>
      api<Message>(tenantPath(tenant, `/cases/${caseId}/messages/${m.id}/delete`), { method: "POST",
        body: { reason } }),
    onSuccess: () => {
      setDeleting(false);
      void qc.invalidateQueries({ queryKey: ["messages", tenant, caseId] });
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    del.mutate();
  }
  return (
    <li className={`message ${supersededBy ? "superseded" : ""}`} aria-label={`Message ${m.seq}`}>
      <div className="small muted">
        #{m.seq} · {m.author ?? "Unknown"} · {new Date(m.created_at).toLocaleString()}
        {m.supersedes_id && " · correction"}
        {supersededBy && ` · corrected by #${supersededBy}`}
      </div>
      {m.deleted_at ? (
        <p className="muted">
          Text deleted {new Date(m.deleted_at).toLocaleString()}
          {m.deletion_reason ? ` — “${m.deletion_reason}”` : ""}. A content digest is kept for integrity.
        </p>
      ) : (
        <p className="message-body">{m.body}</p>
      )}
      {(canCorrect || canDelete) && !deleting && (
        <div className="actions">
          {canCorrect && (
            <button type="button" className="button link small" onClick={onCorrect}>Correct</button>
          )}
          {canDelete && (
            <button type="button" className="button link small" onClick={() => setDeleting(true)}>Delete text</button>
          )}
        </div>
      )}
      {deleting && (
        <form className="form inline-form" onSubmit={submit} aria-label={`Delete message ${m.seq}`}>
          <label>
            Reason for deletion (kept in the audit history)
            <input required minLength={5} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          {del.isError && <ErrorState error={del.error} />}
          <div className="actions">
            <button type="submit" className="button primary" disabled={del.isPending}>Delete message text</button>
            <button type="button" className="button secondary" onClick={() => setDeleting(false)}>Cancel</button>
          </div>
        </form>
      )}
    </li>
  );
}

function Composer({ tenant, caseId, correcting, onDone }: { tenant: string; caseId: string;
  correcting: Message | null; onDone: () => void }) {
  const [body, setBody] = useState(correcting?.body ?? "");
  const post = useMutation<Message, ApiError>({
    mutationFn: () =>
      api<Message>(tenantPath(tenant, `/cases/${caseId}/messages`), { method: "POST",
        body: { body, supersedes_id: correcting?.id ?? null } }),
    onSuccess: () => {
      setBody("");
      onDone();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    post.mutate();
  }
  return (
    <form className="form" onSubmit={submit} aria-label={correcting ? `Correct message ${correcting.seq}` : "Add note"}>
      <label>
        {correcting ? `Correction to message #${correcting.seq}` : "Add a note"}
        <textarea required maxLength={5000} rows={3} value={body} onChange={(e) => setBody(e.target.value)} />
      </label>
      <p className="muted small">
        Notes are case records. Avoid bank details and personal contact data; they are not needed to investigate.
      </p>
      {post.isError && <ErrorState error={post.error} />}
      <div className="actions">
        <button type="submit" className="button primary" disabled={post.isPending}>
          {correcting ? "Save correction" : "Add note"}
        </button>
        {correcting && (
          <button type="button" className="button secondary" onClick={onDone}>Cancel</button>
        )}
      </div>
    </form>
  );
}
