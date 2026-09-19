import { useEffect, useState, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { users } from "@/api/endpoints";
import { useAuth } from "@/context/useAuth";
import type { UserRole } from "@/types";

const ROLES: { value: UserRole; label: string; hint: string }[] = [
  { value: "buyer", label: "Buy", hint: "Post buy-side RFQs and see matching sellers." },
  { value: "seller", label: "Sell", hint: "Post sell-side listings and see matching buyers." },
  { value: "both", label: "Both", hint: "Choose a side on each RFQ." },
];

export function Profile() {
  const { user, refreshUser } = useAuth();
  const [form, setForm] = useState({
    name: "",
    company_name: "",
    phone: "",
    address: "",
    city: "",
    state: "",
    country: "",
  });
  const [role, setRole] = useState<UserRole>("buyer");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!user) return;
    setRole(user.role);
    const profile = user.profile;
    if (!profile) return;
    setForm({
      name: profile.name ?? "",
      company_name: profile.company_name ?? "",
      phone: profile.phone ?? "",
      address: profile.address ?? "",
      city: profile.city ?? "",
      state: profile.state ?? "",
      country: profile.country ?? "",
    });
  }, [user]);

  function update(key: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setStatus(null);
    setSaving(true);
    try {
      // A blank box means "no value", not the empty string -- otherwise saving
      // the form once wrote "" over every field the user had left alone.
      const payload = Object.fromEntries(
        Object.entries(form).map(([key, value]) => [key, value.trim() || null]),
      );
      await users.updateProfile(payload);
      if (user && role !== user.role) await users.updateRole(role);
      await refreshUser();
      setStatus("Saved.");
    } catch (err) {
      setError(errorMessage(err, "Could not save the profile"));
    } finally {
      setSaving(false);
    }
  }

  const activeRole = ROLES.find((option) => option.value === role);

  return (
    <section>
      <h1>Profile</h1>
      <p className="muted">Signed in as {user?.email}</p>

      <form className="card wide" onSubmit={handleSubmit}>
        {error && <p className="error">{error}</p>}
        {status && <p className="success">{status}</p>}

        <label htmlFor="p-role">I want to</label>
        <select
          id="p-role"
          value={role}
          onChange={(event) => setRole(event.target.value as UserRole)}
        >
          {ROLES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <small className="muted">{activeRole?.hint}</small>

        <label htmlFor="p-name">Name</label>
        <input id="p-name" value={form.name} onChange={(e) => update("name", e.target.value)} />

        <label htmlFor="p-company">Company</label>
        <input
          id="p-company"
          value={form.company_name}
          onChange={(e) => update("company_name", e.target.value)}
        />

        <label htmlFor="p-phone">Phone</label>
        <input
          id="p-phone"
          type="tel"
          autoComplete="tel"
          value={form.phone}
          onChange={(e) => update("phone", e.target.value)}
        />

        <label htmlFor="p-address">Address</label>
        <input
          id="p-address"
          value={form.address}
          onChange={(e) => update("address", e.target.value)}
        />
        <small className="muted">
          Your phone, email and address are shown only to counterparties whose connection
          request you accept.
        </small>

        <div className="row">
          <div>
            <label htmlFor="p-city">City</label>
            <input id="p-city" value={form.city} onChange={(e) => update("city", e.target.value)} />
          </div>
          <div>
            <label htmlFor="p-state">State</label>
            <input
              id="p-state"
              value={form.state}
              onChange={(e) => update("state", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="p-country">Country</label>
            <input
              id="p-country"
              value={form.country}
              onChange={(e) => update("country", e.target.value)}
            />
          </div>
        </div>
        <small className="muted">
          A recognised city fills in the state, country and coordinates for you.
        </small>

        <button type="submit" disabled={saving}>
          {saving ? "Saving…" : "Save profile"}
        </button>
      </form>
    </section>
  );
}
