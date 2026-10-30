import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { Layout, PlainHeader } from "./components/Layout";
import { EmptyState, ErrorState, Loading } from "./components/States";
import { AcceptInvitation } from "./pages/AcceptInvitation";
import { CaseDetail } from "./pages/CaseDetail";
import { CaseQueue } from "./pages/CaseQueue";
import { Evaluation } from "./pages/Evaluation";
import { NewCase } from "./pages/NewCase";
import { SignIn } from "./pages/SignIn";
import { TenantSettings } from "./pages/TenantSettings";
import { SessionProvider, useMe, useSession } from "./session";

function TenantGuard() {
  const { tenant = "" } = useParams();
  const { membership } = useSession();
  if (!membership(tenant)) {
    return (
      <main className="content" id="main">
        <ErrorState error={{ status: 404 }} />
        <p>
          You are not a member of this organization. <a href="/">Go to your organizations</a>
        </p>
      </main>
    );
  }
  return <Layout />;
}

function Home() {
  const { me } = useSession();
  const first = me.memberships[0];
  if (!first) {
    return (
      <>
        <PlainHeader />
        <main className="content" id="main">
          <EmptyState title="No organization access">
            <p>
              You are signed in as {me.username}, but no organization has granted you access yet. Ask a tenant
              administrator for an invitation, then open its link in this browser.
            </p>
          </EmptyState>
        </main>
      </>
    );
  }
  return <Navigate to={`/t/${first.tenant_slug}/cases`} replace />;
}

export function App() {
  const me = useMe();
  if (me.isPending) return <Loading label="Checking your session" />;
  if (me.isError) {
    if (me.error.status === 401) return <SignIn />;
    return <ErrorState error={me.error} onRetry={() => me.refetch()} />;
  }
  return (
    <SessionProvider me={me.data}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/invite" element={<AcceptInvitation />} />
        <Route path="/t/:tenant" element={<TenantGuard />}>
          <Route index element={<Navigate to="cases" replace />} />
          <Route path="cases" element={<CaseQueue />} />
          <Route path="cases/new" element={<NewCase />} />
          <Route path="cases/:caseId" element={<CaseDetail />} />
          <Route path="settings" element={<TenantSettings />} />
          <Route path="settings/:section" element={<TenantSettings />} />
          <Route path="evaluation" element={<Evaluation />} />
          <Route path="evaluation/:benchmark/:runId" element={<Evaluation />} />
        </Route>
        <Route path="*" element={<Home />} />
      </Routes>
    </SessionProvider>
  );
}
