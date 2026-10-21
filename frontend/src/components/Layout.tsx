import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useSession } from "../session";

async function signOut() {
  try {
    const out = await api<{ logout_url: string | null }>("/auth/logout", { method: "POST" });
    window.location.assign(out.logout_url ?? "/");
  } catch {
    window.location.assign("/");
  }
}

/** Header for signed-in pages outside an organization (no access yet, accepting an invitation). */
export function PlainHeader() {
  const { me } = useSession();
  return (
    <header className="topbar">
      <div className="brand">OpsBridge</div>
      <div className="spacer" />
      <div className="user">
        <span>{me.display_name}</span>
      </div>
      <button type="button" className="button link" onClick={signOut}>
        Sign out
      </button>
    </header>
  );
}

export function Layout() {
  const { tenant = "" } = useParams();
  const { me, membership } = useSession();
  const navigate = useNavigate();
  const current = membership(tenant);
  const perms = current?.permissions ?? [];

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="brand">OpsBridge</div>
        <label className="tenant-switch">
          <span className="visually-hidden">Organization</span>
          <select value={tenant} onChange={(e) => navigate(`/t/${e.target.value}/cases`)}>
            {me.memberships.map((m) => (
              <option key={m.tenant_slug} value={m.tenant_slug}>
                {m.tenant_name}
              </option>
            ))}
          </select>
        </label>
        {current?.synthetic_demo && <span className="tag">Synthetic demo data</span>}
        <div className="spacer" />
        <div className="user">
          <span>{me.display_name}</span>
          <span className="muted small">{current?.roles.join(", ")}</span>
        </div>
        <button type="button" className="button link" onClick={signOut}>
          Sign out
        </button>
      </header>
      <nav className="sidenav" aria-label="Main">
        <NavLink to={`/t/${tenant}/cases`} end>
          Case queue
        </NavLink>
        {perms.includes("case.create") && <NavLink to={`/t/${tenant}/cases/new`}>New case</NavLink>}
        {(perms.includes("eval.read") || perms.includes("investigation.run")) && (
          <NavLink to={`/t/${tenant}/evaluation`}>Evaluation</NavLink>
        )}
        {perms.includes("tenant.read_settings") && <NavLink to={`/t/${tenant}/settings`}>Tenant settings</NavLink>}
      </nav>
      <main id="main" className="content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
