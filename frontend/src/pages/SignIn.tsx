export function SignIn({ expired = false }: { expired?: boolean }) {
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  return (
    <main className="signin" id="main">
      <h1>OpsBridge</h1>
      <p>Investigate blocked invoices with cited evidence and approved follow-up actions.</p>
      {expired && <p role="status">Your session has ended. Sign in again to continue.</p>}
      <a className="button primary" href={`/auth/login?next=${next}`}>
        Sign in with your organization
      </a>
      <p className="muted small">Sign-in is handled by your identity provider. OpsBridge never sees your password.</p>
    </main>
  );
}
