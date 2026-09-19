import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "@/context/useAuth";

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <span className="brand">Marketplace</span>
        <nav>
          <NavLink to="/search">Search</NavLink>
          <NavLink to="/rfqs">My RFQs</NavLink>
          <NavLink to="/rfqs/new">New RFQ</NavLink>
          <NavLink to="/messages">Messages</NavLink>
          <NavLink to="/profile">Profile</NavLink>
        </nav>
        <div className="sidebar-bottom">
          <span className="muted" style={{ fontSize: "0.85rem", overflow: "hidden", textOverflow: "ellipsis" }}>{user?.email}</span>
          <button type="button" className="secondary" onClick={handleLogout} style={{ width: "100%" }}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
