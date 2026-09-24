import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { AuthShell } from "@/components/AuthShell";
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

  if (loading) return <div className="centered muted">Loading…</div>;
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
    <AuthShell>
      <form className="card auth-card auth-card-wide" onSubmit={handleSubmit}>
        <div>
          <h1>Create an account</h1>
          <p className="auth-lead">Post what you need or what you sell, and get matched.</p>
        </div>

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        <div className="form-stack">
          <div className="field">
            <span className="field-label" id="role-label">
              I want to
            </span>
            <div className="tabs segmented segmented-full" role="radiogroup" aria-labelledby="role-label">
              {(
                [
                  ["buyer", "Buy"],
                  ["seller", "Sell"],
                  ["both", "Both"],
                ] as [UserRole, string][]
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={form.role === value}
                  className={form.role === value ? "tab active" : "tab"}
                  onClick={() => update("role", value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="field-grid field-grid-2">
            <div className="field">
              <label htmlFor="name">Your name</label>
              <input
                id="name"
                autoComplete="name"
                required
                value={form.name}
                onChange={(e) => update("name", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="company">
                Company <span className="optional">Optional</span>
              </label>
              <input
                id="company"
                autoComplete="organization"
                value={form.company_name}
                onChange={(e) => update("company_name", e.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="email">Work email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={form.email}
              onChange={(e) => update("email", e.target.value)}
            />
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="new-password"
              required
              minLength={MIN_PASSWORD_LENGTH}
              aria-describedby="password-hint"
              value={form.password}
              onChange={(e) => update("password", e.target.value)}
            />
            <small id="password-hint">At least {MIN_PASSWORD_LENGTH} characters.</small>
          </div>

          <div className="field-grid field-grid-2">
            <div className="field">
              <label htmlFor="phone">
                Phone <span className="optional">Optional</span>
              </label>
              <input
                id="phone"
                type="tel"
                autoComplete="tel"
                value={form.phone}
                onChange={(e) => update("phone", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="city">
                City <span className="optional">Optional</span>
              </label>
              <input
                id="city"
                autoComplete="address-level2"
                value={form.city}
                onChange={(e) => update("city", e.target.value)}
                placeholder="e.g. Indore"
              />
            </div>
          </div>
          <small className="field-hint">
            Your phone is only shared with counterparties whose request you accept.
          </small>
        </div>

        <button type="submit" className="btn-block btn-lg" disabled={submitting}>
          {submitting ? "Creating account…" : "Create account"}
        </button>

        <p className="auth-footer">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </AuthShell>
  );
}
