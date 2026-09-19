import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { useAuth } from "@/context/useAuth";
import type { UserRole } from "@/types";

const MIN_PASSWORD_LENGTH = 8;

export function Signup() {
  const { user, loading, signup } = useAuth();
  const navigate = useNavigate();

  const [form, setForm] = useState({
    email: "",
    password: "",
    name: "",
    company_name: "",
    phone: "",
    city: "",
    role: "buyer" as UserRole,
  });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (loading) return <div className="centered muted">Loading...</div>;
  if (user) return <Navigate to="/rfqs" replace />;

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signup({
        email: form.email,
        password: form.password,
        name: form.name,
        role: form.role,
        // Empty optional strings are dropped rather than sent as "".
        company_name: form.company_name || undefined,
        phone: form.phone || undefined,
        city: form.city || undefined,
      });
      navigate("/rfqs", { replace: true });
    } catch (err) {
      setError(errorMessage(err, "Could not create the account"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="centered">
      <form className="card" onSubmit={handleSubmit}>
        <h1>Create an account</h1>
        {error && <p className="error">{error}</p>}

        <label htmlFor="name">Your name</label>
        <input id="name" required value={form.name} onChange={(e) => update("name", e.target.value)} />

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="email"
          required
          value={form.email}
          onChange={(e) => update("email", e.target.value)}
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="new-password"
          required
          minLength={MIN_PASSWORD_LENGTH}
          value={form.password}
          onChange={(e) => update("password", e.target.value)}
        />
        <small className="muted">At least {MIN_PASSWORD_LENGTH} characters.</small>

        <label htmlFor="role">I want to</label>
        <select id="role" value={form.role} onChange={(e) => update("role", e.target.value as UserRole)}>
          <option value="buyer">Buy</option>
          <option value="seller">Sell</option>
          <option value="both">Both</option>
        </select>

        <label htmlFor="company">Company (optional)</label>
        <input id="company" value={form.company_name} onChange={(e) => update("company_name", e.target.value)} />

        <label htmlFor="phone">Phone (optional)</label>
        <input
          id="phone"
          type="tel"
          autoComplete="tel"
          value={form.phone}
          onChange={(e) => update("phone", e.target.value)}
        />
        <small className="muted">
          Shared only with counterparties whose connection request you accept.
        </small>

        <label htmlFor="city">City (optional)</label>
        <input
          id="city"
          value={form.city}
          onChange={(e) => update("city", e.target.value)}
          placeholder="Indore"
        />

        <button type="submit" disabled={submitting}>
          {submitting ? "Creating..." : "Create account"}
        </button>
        <p className="muted">
          Already registered? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
