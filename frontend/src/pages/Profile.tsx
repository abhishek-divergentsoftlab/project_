import { useEffect, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { certifications, kyc, reviews as reviewsApi, users } from "@/api/endpoints";
import {
  IconClock,
  IconExternalLink,
  IconFileText,
  IconMapPin,
  IconPlus,
  IconShield,
  IconStar,
  IconUpload,
  IconX,
} from "@/components/icons";
import { LocationMapPicker } from "@/components/map/LocationMapPicker";
import { Menu } from "@/components/ui/Menu";
import { Modal } from "@/components/ui/Modal";
import { useAuth } from "@/context/useAuth";
import { useFeedback } from "@/context/useFeedback";
import { formatDate } from "@/utils/format";
import type {
  Certificate,
  CertificateCreatePayload,
  KYCVerificationPayload,
  UserReviewStats,
  UserRole,
} from "@/types";

const ROLES: { value: UserRole; label: string; hint: string }[] = [
  { value: "buyer", label: "Buy", hint: "Post buy-side RFQs and see matching sellers." },
  { value: "seller", label: "Sell", hint: "Post sell-side listings and see matching buyers." },
  { value: "both", label: "Both", hint: "Choose a side on each RFQ." },
];

const ROLE_BADGE: Record<UserRole, string> = {
  buyer: "Buyer",
  seller: "Seller",
  both: "Buyer & seller",
};

type Tab = "general" | "matching" | "kyc" | "certificates" | "reviews";

const TABS: { id: Tab; label: string }[] = [
  { id: "general", label: "Account" },
  { id: "matching", label: "Matching Criteria" },
  { id: "kyc", label: "KYC & GST" },
  { id: "certificates", label: "Certificates" },
  { id: "reviews", label: "Reviews" },
];

const CERT_QUICK_TYPES = [
  "ISO 9001:2015 Quality Management",
  "ISO 14001 Environmental",
  "CE Marking",
  "FDA Registration",
  "GMP Compliance",
  "RoHS Declaration",
];

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getInitials(name?: string | null, email?: string | null): string {
  const source = (name || email || "U").trim();
  const parts = source.split(/[\s@]+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function kycLabel(status: string): string {
  if (status === "verified") return "Verified";
  if (status === "pending") return "Pending review";
  return "Unverified";
}

export function Profile() {
  const { user, refreshUser } = useAuth();
  const { toast, confirm } = useFeedback();
  const [activeTab, setActiveTab] = useState<Tab>("general");

  const [form, setForm] = useState({
    name: "",
    company_name: "",
    phone: "",
    address: "",
    city: "",
    state: "",
    country: "",
    latitude: null as number | null,
    longitude: null as number | null,
  });
  const [showMapPicker, setShowMapPicker] = useState(false);
  const [role, setRole] = useState<UserRole>("buyer");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [matchingPreset, setMatchingPreset] = useState<
    "quality_first" | "price_first" | "fast_delivery" | "balanced" | "custom"
  >("balanced");
  const [customWeights, setCustomWeights] = useState({
    relevance: 28,
    attributes: 22,
    price: 16,
    quantity: 12,
    location: 8,
    deadline: 4,
  });
  const [savingMatching, setSavingMatching] = useState(false);

  const [kycForm, setKycForm] = useState<KYCVerificationPayload>({
    gst_number: "",
    legal_business_name: "",
    business_type: "Private Limited",
    registration_number: "",
    year_established: 2020,
    website: "",
    pan_number: "",
    signatory_name: "",
  });
  const [kycSaving, setKycSaving] = useState(false);

  const [certList, setCertList] = useState<Certificate[]>([]);
  const [loadingCerts, setLoadingCerts] = useState(false);
  const [showCertModal, setShowCertModal] = useState(false);
  const [certForm, setCertForm] = useState<{
    name: string;
    issuing_body: string;
    certificate_number: string;
    issue_date: string;
    expiry_date: string;
    document_url: string;
  }>({
    name: "ISO 9001:2015 Quality Management",
    issuing_body: "SGS International",
    certificate_number: "",
    issue_date: new Date().toISOString().slice(0, 10),
    expiry_date: new Date(Date.now() + 3 * 365 * 86400000).toISOString().slice(0, 10),
    document_url: "",
  });

  const [certDocMode, setCertDocMode] = useState<"upload" | "url">("upload");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadingCert, setUploadingCert] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [previewModalCert, setPreviewModalCert] = useState<Certificate | null>(null);
  const [directUploadLoadingId, setDirectUploadLoadingId] = useState<string | null>(null);
  const [directTargetCertId, setDirectTargetCertId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const directFileInputRef = useRef<HTMLInputElement | null>(null);

  const [reviewStats, setReviewStats] = useState<UserReviewStats | null>(null);
  const [loadingReviews, setLoadingReviews] = useState(false);

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
      latitude: profile.latitude ?? null,
      longitude: profile.longitude ?? null,
    });
    if (profile.latitude != null && profile.longitude != null) {
      setShowMapPicker(true);
    }

    setKycForm({
      gst_number: profile.gst_number ?? "",
      legal_business_name: profile.legal_business_name ?? profile.company_name ?? "",
      business_type: profile.business_type ?? "Private Limited",
      registration_number: profile.registration_number ?? "",
      year_established: profile.year_established ?? 2020,
      website: profile.website ?? "",
      pan_number: profile.pan_number ?? "",
      signatory_name: profile.signatory_name ?? profile.name ?? "",
    });

    if (profile.matching_preferences) {
      if (profile.matching_preferences.preset) {
        setMatchingPreset(profile.matching_preferences.preset);
      }
      if (profile.matching_preferences.weights) {
        setCustomWeights({
          relevance: Math.round((profile.matching_preferences.weights.relevance ?? 0.28) * 100),
          attributes: Math.round((profile.matching_preferences.weights.attributes ?? 0.22) * 100),
          price: Math.round((profile.matching_preferences.weights.price ?? 0.16) * 100),
          quantity: Math.round((profile.matching_preferences.weights.quantity ?? 0.12) * 100),
          location: Math.round((profile.matching_preferences.weights.location ?? 0.08) * 100),
          deadline: Math.round((profile.matching_preferences.weights.deadline ?? 0.04) * 100),
        });
      }
    }
  }, [user]);

  useEffect(() => {
    if (activeTab === "certificates" && user) {
      setLoadingCerts(true);
      void certifications
        .list()
        .then(setCertList)
        .catch((err) => setError(errorMessage(err, "Could not load certificates")))
        .finally(() => setLoadingCerts(false));
    }
  }, [activeTab, user]);

  useEffect(() => {
    if (activeTab === "reviews" && user) {
      setLoadingReviews(true);
      void reviewsApi
        .getUserReviews(user.id)
        .then(setReviewStats)
        .catch((err) => setError(errorMessage(err, "Could not load reviews")))
        .finally(() => setLoadingReviews(false));
    }
  }, [activeTab, user]);

  function update(key: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function clearAlerts() {
    setError(null);
  }

  function closeCertModal() {
    if (uploadingCert) return;
    setShowCertModal(false);
    setSelectedFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    clearAlerts();
    setSaving(true);
    try {
      const payload = Object.fromEntries(
        Object.entries(form).map(([key, value]) => [
          key,
          typeof value === "string" ? value.trim() || null : value ?? null,
        ]),
      );
      await users.updateProfile(payload);
      if (user && role !== user.role) await users.updateRole(role);
      await refreshUser();
      toast("Profile saved.");
    } catch (err) {
      setError(errorMessage(err, "Could not save the profile"));
    } finally {
      setSaving(false);
    }
  }

  async function handleKycSubmit(event: FormEvent) {
    event.preventDefault();
    clearAlerts();
    setKycSaving(true);
    try {
      const res = await kyc.verify(kycForm);
      await refreshUser();
      toast(res.message || "Verification submitted.");
    } catch (err) {
      setError(errorMessage(err, "Could not verify company KYC"));
    } finally {
      setKycSaving(false);
    }
  }

  function applyPreset(preset: "quality_first" | "price_first" | "fast_delivery" | "balanced" | "custom") {
    setMatchingPreset(preset);
    if (preset === "quality_first") {
      setCustomWeights({ relevance: 35, attributes: 35, price: 12, location: 8, quantity: 6, deadline: 4 });
    } else if (preset === "price_first") {
      setCustomWeights({ price: 36, relevance: 22, quantity: 18, attributes: 12, location: 8, deadline: 4 });
    } else if (preset === "fast_delivery") {
      setCustomWeights({ location: 30, deadline: 25, relevance: 20, attributes: 12, price: 8, quantity: 5 });
    } else if (preset === "balanced") {
      setCustomWeights({ relevance: 28, attributes: 22, price: 16, quantity: 12, location: 8, deadline: 4 });
    }
  }

  async function handleSaveMatching(e: FormEvent) {
    e.preventDefault();
    clearAlerts();
    setSavingMatching(true);
    try {
      const sum = Object.values(customWeights).reduce((a, b) => a + b, 0);
      const normalizedWeights = Object.fromEntries(
        Object.entries(customWeights).map(([k, v]) => [k, sum > 0 ? Number((v / sum).toFixed(4)) : 0])
      );
      await users.updateProfile({
        matching_preferences: {
          preset: matchingPreset,
          weights: normalizedWeights,
        },
      });
      await refreshUser();
      toast("Matching priority criteria saved! Your RFQ matches now follow these weights.");
    } catch (err) {
      setError(errorMessage(err, "Could not save matching preferences"));
    } finally {
      setSavingMatching(false);
    }
  }

  const ALLOWED_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".webp"];
  const MAX_CERT_SIZE = 10 * 1024 * 1024; // 10 MB

  function handleFileSelect(file: File) {
    clearAlerts();
    const ext = "." + file.name.split(".").pop()?.toLowerCase();
    if (!ALLOWED_EXTS.includes(ext)) {
      setError(
        `Unsupported file format '${ext}'. Please select a valid PDF, PNG, JPG, or WebP document.`,
      );
      return;
    }
    if (file.size > MAX_CERT_SIZE) {
      setError(`File size (${formatFileSize(file.size)}) exceeds the maximum 10 MB limit.`);
      return;
    }
    setSelectedFile(file);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      handleFileSelect(e.dataTransfer.files[0]);
    }
  }

  async function handleAddCertificate(event: FormEvent) {
    event.preventDefault();
    clearAlerts();
    setUploadingCert(true);
    try {
      let documentUrl: string | undefined = undefined;

      if (certDocMode === "upload" && selectedFile) {
        const uploadRes = await certifications.uploadDocument(selectedFile);
        documentUrl = uploadRes.document_url;
      } else if (certDocMode === "url" && certForm.document_url.trim()) {
        documentUrl = certForm.document_url.trim();
      }

      const payload: CertificateCreatePayload = {
        name: certForm.name.trim(),
        issuing_body: certForm.issuing_body.trim(),
        certificate_number: certForm.certificate_number.trim(),
        issue_date: new Date(certForm.issue_date).toISOString(),
        expiry_date: certForm.expiry_date ? new Date(certForm.expiry_date).toISOString() : undefined,
        document_url: documentUrl,
      };
      const created = await certifications.create(payload);
      setCertList((current) => [created, ...current]);
      setShowCertModal(false);
      setSelectedFile(null);
      setCertForm((current) => ({
        ...current,
        certificate_number: "",
        document_url: "",
      }));
      toast("Certificate added.");
      await refreshUser();
    } catch (err) {
      setError(errorMessage(err, "Could not add certificate"));
    } finally {
      setUploadingCert(false);
    }
  }

  async function handleDirectFileUpload(e: ChangeEvent<HTMLInputElement>) {
    if (!e.target.files || !e.target.files[0] || !directTargetCertId) return;
    const file = e.target.files[0];
    const certId = directTargetCertId;
    e.target.value = "";

    clearAlerts();
    const ext = "." + file.name.split(".").pop()?.toLowerCase();
    if (!ALLOWED_EXTS.includes(ext)) {
      setError(
        `Unsupported file format '${ext}'. Please select a valid PDF, PNG, JPG, or WebP document.`,
      );
      return;
    }
    if (file.size > MAX_CERT_SIZE) {
      setError(`File size (${formatFileSize(file.size)}) exceeds the maximum 10 MB limit.`);
      return;
    }

    setDirectUploadLoadingId(certId);
    try {
      const updated = await certifications.uploadToCertificate(certId, file);
      setCertList((current) => current.map((c) => (c.id === certId ? updated : c)));
      toast("Document uploaded.");
    } catch (err) {
      setError(errorMessage(err, "Could not upload certificate document"));
    } finally {
      setDirectUploadLoadingId(null);
      setDirectTargetCertId(null);
    }
  }

  function triggerDirectUpload(certId: string) {
    setDirectTargetCertId(certId);
    directFileInputRef.current?.click();
  }

  async function handleDeleteCertificate(id: string) {
    const ok = await confirm({
      title: "Remove this certificate?",
      message: "It will no longer count toward your trust score or appear to counterparties.",
      confirmLabel: "Remove",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await certifications.delete(id);
      setCertList((current) => current.filter((c) => c.id !== id));
      toast("Certificate removed.");
    } catch (err) {
      setError(errorMessage(err, "Could not delete certificate"));
    }
  }

  const kycStatus = user?.profile?.kyc_status ?? "unverified";
  const trustScore = user?.profile?.trust_score ?? 20;
  const displayName =
    user?.profile?.company_name || user?.profile?.name || user?.email || "Your profile";
  const contactName = user?.profile?.name || "Contact";
  const initials = getInitials(user?.profile?.company_name || user?.profile?.name, user?.email);
  return (
    <section className="page page-narrow profile-page">
      <header className="profile-hero">
        <div className="profile-avatar" aria-hidden="true">
          {initials}
        </div>
        <div className="profile-hero-copy">
          <h1>{displayName}</h1>
          <p className="profile-hero-meta">
            {contactName}
            {user?.email ? ` · ${user.email}` : ""}
          </p>
          <div className="profile-hero-tags">
            <span className={`badge badge-kyc-${kycStatus}`}>
              {kycStatus === "verified" ? (
                <IconShield size={12} />
              ) : kycStatus === "pending" ? (
                <IconClock size={12} />
              ) : null}
              {kycLabel(kycStatus)}
            </span>
            <span className="badge">{ROLE_BADGE[user?.role ?? "buyer"]}</span>
          </div>
        </div>

        <div
          className="profile-trust"
          title="Built from verification, certificates and completed trades"
          aria-label={`Trust score ${trustScore} out of 100`}
        >
          <span className="profile-trust-label">Trust score</span>
          <span className="profile-trust-value">
            {trustScore}
            <span>/100</span>
          </span>
          <span className="profile-trust-bar" aria-hidden="true">
            <span style={{ width: `${Math.min(100, Math.max(0, trustScore))}%` }} />
          </span>
        </div>
      </header>

      <nav className="tab-bar" aria-label="Profile sections">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            aria-current={activeTab === tab.id ? "page" : undefined}
            className={activeTab === tab.id ? "tab active" : "tab"}
            onClick={() => {
              clearAlerts();
              setActiveTab(tab.id);
            }}
          >
            {tab.label}
            {tab.id === "certificates" && certList.length > 0 && (
              <span className="tab-count">{certList.length}</span>
            )}
          </button>
        ))}
      </nav>

      {error && <p className="error">{error}</p>}

      {activeTab === "general" && (
        <form className="panel form-panel" onSubmit={handleSubmit}>
          <div className="form-section">
            <div className="form-section-head">
              <h2>How you trade</h2>
              <p>Decides which side of the market you can post on.</p>
            </div>
            <div className="role-picker" role="radiogroup" aria-label="Trading role">
              {ROLES.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={role === option.value}
                  className={`role-option ${role === option.value ? "selected" : ""}`}
                  onClick={() => setRole(option.value)}
                >
                  <span className="role-option-label">{option.label}</span>
                  <span className="role-option-hint">{option.hint}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="form-section">
            <div className="form-section-head">
              <h2>Company &amp; contact</h2>
              <p>Phone and address are only shared after you accept a connection.</p>
            </div>
            <div className="field-grid field-grid-2">
              <div className="field">
                <label htmlFor="p-company">Company name</label>
                <input
                  id="p-company"
                  value={form.company_name}
                  onChange={(e) => update("company_name", e.target.value)}
                  placeholder="Trading name"
                />
              </div>
              <div className="field">
                <label htmlFor="p-name">Contact person</label>
                <input
                  id="p-name"
                  value={form.name}
                  onChange={(e) => update("name", e.target.value)}
                  placeholder="Full name"
                />
              </div>
              <div className="field">
                <label htmlFor="p-phone">Business phone</label>
                <input
                  id="p-phone"
                  type="tel"
                  autoComplete="tel"
                  value={form.phone}
                  onChange={(e) => update("phone", e.target.value)}
                  placeholder="+91 …"
                />
              </div>
              <div className="field field-span">
                <label htmlFor="p-address">Warehouse or registered address</label>
                <input
                  id="p-address"
                  value={form.address}
                  onChange={(e) => update("address", e.target.value)}
                  placeholder="Street, building, landmark"
                />
              </div>
            </div>
          </div>

          <div className="form-section">
            <div className="form-section-head">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%", flexWrap: "wrap", gap: "8px" }}>
                <div>
                  <h2>Facility & Dispatch Location</h2>
                  <p>Pinpoint your primary facility, warehouse, or factory on the map for distance and freight calculations.</p>
                </div>
                <button
                  type="button"
                  className="secondary small-btn"
                  style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                  onClick={() => setShowMapPicker(!showMapPicker)}
                >
                  <IconMapPin size={14} />
                  {showMapPicker ? "Hide Map" : "🗺️ Pinpoint on Map"}
                </button>
              </div>
            </div>

            {showMapPicker && (
              <div style={{ marginBottom: "16px" }}>
                <LocationMapPicker
                  initialLat={form.latitude}
                  initialLng={form.longitude}
                  initialCity={form.city}
                  initialState={form.state}
                  initialCountry={form.country}
                  onLocationSelect={(loc) => {
                    setForm((current) => ({
                      ...current,
                      city: loc.city,
                      state: loc.state,
                      country: loc.country,
                      latitude: loc.latitude,
                      longitude: loc.longitude,
                    }));
                  }}
                  height="280px"
                  showPresets={true}
                />
              </div>
            )}

            <div className="field-grid">
              <div className="field">
                <label htmlFor="p-city">City</label>
                <input id="p-city" value={form.city} onChange={(e) => update("city", e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="p-state">State / province</label>
                <input
                  id="p-state"
                  value={form.state}
                  onChange={(e) => update("state", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="p-country">Country</label>
                <input
                  id="p-country"
                  value={form.country}
                  onChange={(e) => update("country", e.target.value)}
                />
              </div>
            </div>
          </div>

          <div className="form-footer">
            <button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>
      )}

      {activeTab === "matching" && (
        <form className="panel form-panel" onSubmit={handleSaveMatching}>
          <div className="form-section">
            <div className="form-section-head">
              <h2>Matchmaking Priority &amp; Dynamic Criteria</h2>
              <p>
                Configure how our Match Engine scores prospective counterparties for your RFQs.
                Physical Feasibility &amp; Spec Compatibility are strictly prioritized above commercial terms.
              </p>
            </div>

            <div style={{ display: "grid", gap: "1rem", marginBottom: "1.5rem" }}>
              <div
                style={{
                  background: "var(--color-bg-secondary, #f8fafc)",
                  padding: "1rem",
                  borderRadius: "8px",
                  border: "1px solid var(--color-border, #e2e8f0)",
                }}
              >
                <h4 style={{ margin: "0 0 0.5rem 0", fontSize: "0.95rem", display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  <span>🛡️</span> 4-Pillars Match Engine Active
                </h4>
                <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--color-text-secondary, #64748b)", lineHeight: "1.4" }}>
                  <strong>Key-Agnostic Fact Inversion</strong> (e.g. <code>type: organic</code> ↔ <code>organic: true</code>),
                  <strong> Physical Dimensional SI Reduction</strong> (e.g. <code>2cm == 20mm</code>),
                  <strong> Asymmetric Specificity Guard</strong> (generic items cannot beat specific variants), and
                  <strong> Two-Tier Multiplicative Gating</strong> are dynamically enforced on every search.
                </p>
              </div>

              <div className="form-group">
                <label style={{ fontWeight: 600, display: "block", marginBottom: "0.5rem" }}>
                  Choose a Trade Priority Strategy
                </label>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "0.75rem" }}>
                  {[
                    {
                      id: "quality_first" as const,
                      title: "Quality & Specs First",
                      desc: "Specs & Relevance take 70% weight. Ideal for manufacturing & OEM.",
                      icon: "🎯",
                    },
                    {
                      id: "balanced" as const,
                      title: "Balanced (Default)",
                      desc: "Standard enterprise distribution across specs, price & logistics.",
                      icon: "⚖️",
                    },
                    {
                      id: "price_first" as const,
                      title: "Budget & Price Focus",
                      desc: "Price carries 36% weight. Best for commodities & raw bulk materials.",
                      icon: "💰",
                    },
                    {
                      id: "fast_delivery" as const,
                      title: "Rapid Dispatch & Local",
                      desc: "Proximity & deadline take 55% weight. Best for urgent fulfillment.",
                      icon: "🚚",
                    },
                  ].map((p) => (
                    <div
                      key={p.id}
                      onClick={() => applyPreset(p.id)}
                      style={{
                        padding: "1rem",
                        borderRadius: "8px",
                        border: matchingPreset === p.id ? "2px solid var(--color-primary, #2563eb)" : "1px solid var(--color-border, #e2e8f0)",
                        background: matchingPreset === p.id ? "rgba(37, 99, 235, 0.05)" : "var(--color-surface, #ffffff)",
                        cursor: "pointer",
                        transition: "all 0.15s ease",
                      }}
                    >
                      <div style={{ fontSize: "1.25rem", marginBottom: "0.25rem" }}>{p.icon}</div>
                      <div style={{ fontWeight: 600, fontSize: "0.9rem", color: "var(--color-text, #0f172a)" }}>{p.title}</div>
                      <div style={{ fontSize: "0.8rem", color: "var(--color-text-secondary, #64748b)", marginTop: "0.25rem" }}>{p.desc}</div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="form-group" style={{ marginTop: "1rem" }}>
                <label style={{ fontWeight: 600, display: "block", marginBottom: "0.75rem" }}>
                  Dimension Weights Breakdown
                </label>
                <div style={{ display: "grid", gap: "0.85rem" }}>
                  {[
                    { key: "relevance" as const, label: "Core Product Relevance", desc: "Semantic alignment with requested product", max: 50 },
                    { key: "attributes" as const, label: "Technical Specifications & Tolerance", desc: "Exact spec values, SI dimensional units & materials", max: 50 },
                    { key: "price" as const, label: "Target vs Ask Price", desc: "Seller ask price vs your target budget with currency FX", max: 50 },
                    { key: "quantity" as const, label: "Quantity Capacity", desc: "Fulfillment volume with mass/container conversion", max: 50 },
                    { key: "location" as const, label: "Proximity & Distance", desc: "Haversine km distance and regional logistics mode", max: 50 },
                    { key: "deadline" as const, label: "Delivery Lead Time", desc: "Counterparty dispatch window before required delivery", max: 50 },
                  ].map((dim) => (
                    <div
                      key={dim.key}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "180px 1fr 60px",
                        alignItems: "center",
                        gap: "1rem",
                        padding: "0.5rem 0.75rem",
                        borderRadius: "6px",
                        background: "var(--color-bg-secondary, #f8fafc)",
                      }}
                    >
                      <div>
                        <div style={{ fontWeight: 600, fontSize: "0.85rem" }}>{dim.label}</div>
                        <div style={{ fontSize: "0.75rem", color: "var(--color-text-secondary, #64748b)" }}>{dim.desc}</div>
                      </div>
                      <input
                        type="range"
                        min="0"
                        max={dim.max}
                        value={customWeights[dim.key]}
                        onChange={(e) => {
                          setMatchingPreset("custom");
                          setCustomWeights((prev) => ({
                            ...prev,
                            [dim.key]: Number(e.target.value),
                          }));
                        }}
                        style={{ width: "100%", cursor: "pointer" }}
                      />
                      <div style={{ textAlign: "right", fontWeight: 700, fontSize: "0.85rem" }}>
                        {customWeights[dim.key]}%
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="form-footer">
            <button type="submit" disabled={savingMatching}>
              {savingMatching ? "Saving Preferences…" : "Save Matching Preferences"}
            </button>
          </div>
        </form>
      )}

      {activeTab === "kyc" && (
        <form className="panel form-panel" onSubmit={handleKycSubmit}>
          <div className="form-section">
            <div className="form-section-head form-section-head-row">
              <div>
                <h2>Business verification</h2>
                <p>Verified businesses get a badge on every listing and rank higher in matches.</p>
              </div>
              <span className={`badge badge-kyc-${kycStatus}`}>{kycLabel(kycStatus)}</span>
            </div>

            <div className="field-grid field-grid-2">
              <div className="field">
                <label htmlFor="kyc-gst">GST or tax ID</label>
                <input
                  id="kyc-gst"
                  required
                  placeholder="15-character GSTIN or tax ID"
                  value={kycForm.gst_number}
                  onChange={(e) => setKycForm({ ...kycForm, gst_number: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="kyc-legal-name">Legal business name</label>
                <input
                  id="kyc-legal-name"
                  required
                  placeholder="As on the tax certificate"
                  value={kycForm.legal_business_name}
                  onChange={(e) => setKycForm({ ...kycForm, legal_business_name: e.target.value })}
                />
              </div>
            </div>
          </div>

          <div className="form-section">
            <div className="form-section-head">
              <h2>
                Company details <span className="optional">Optional</span>
              </h2>
            </div>
            <div className="field-grid field-grid-2">
              <div className="field">
                <label htmlFor="kyc-type">Entity type</label>
                <select
                  id="kyc-type"
                  value={kycForm.business_type}
                  onChange={(e) => setKycForm({ ...kycForm, business_type: e.target.value })}
                >
                  <option value="Private Limited">Private Limited</option>
                  <option value="Public Limited">Public Limited</option>
                  <option value="Proprietorship">Sole Proprietorship</option>
                  <option value="Partnership">Partnership / LLP</option>
                  <option value="LLC">LLC</option>
                  <option value="Corporation">Corporation</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="kyc-cin">Registration number (CIN)</label>
                <input
                  id="kyc-cin"
                  placeholder="e.g. U72200MH2020PTC123456"
                  value={kycForm.registration_number ?? ""}
                  onChange={(e) => setKycForm({ ...kycForm, registration_number: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="kyc-year">Year established</label>
                <input
                  id="kyc-year"
                  type="number"
                  min="1850"
                  max="2026"
                  value={kycForm.year_established ?? 2020}
                  onChange={(e) =>
                    setKycForm({ ...kycForm, year_established: parseInt(e.target.value, 10) || 2020 })
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="kyc-pan">Company PAN</label>
                <input
                  id="kyc-pan"
                  placeholder="e.g. AABCU9603R"
                  value={kycForm.pan_number ?? ""}
                  onChange={(e) => setKycForm({ ...kycForm, pan_number: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="kyc-website">Website</label>
                <input
                  id="kyc-website"
                  type="url"
                  placeholder="https://company.example.com"
                  value={kycForm.website ?? ""}
                  onChange={(e) => setKycForm({ ...kycForm, website: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="kyc-signatory">Authorised signatory</label>
                <input
                  id="kyc-signatory"
                  placeholder="Director or representative"
                  value={kycForm.signatory_name ?? ""}
                  onChange={(e) => setKycForm({ ...kycForm, signatory_name: e.target.value })}
                />
              </div>
            </div>
          </div>

          <div className="form-footer">
            <button type="submit" disabled={kycSaving}>
              {kycSaving ? "Submitting…" : kycStatus === "unverified" ? "Submit for verification" : "Update details"}
            </button>
          </div>
        </form>
      )}

      {activeTab === "certificates" && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>Certificates</h2>
              <p className="panel-head-sub">ISO, CE, FDA, GMP or RoHS credentials strengthen your match ranking.</p>
            </div>
            {certList.length > 0 && (
              <button type="button" className="primary small-btn" onClick={() => setShowCertModal(true)}>
                <IconPlus size={14} />
                Add certificate
              </button>
            )}
          </div>

          {loadingCerts ? (
            <div className="loading-state">
              <span className="spinner" />
              Loading certificates…
            </div>
          ) : certList.length === 0 ? (
            <div className="empty-state">
              <span className="empty-state-icon">
                <IconFileText size={18} />
              </span>
              <strong>No certificates yet</strong>
              <p>Add a verified credential to rank higher in matches and earn a trust badge.</p>
              <button type="button" className="primary" onClick={() => setShowCertModal(true)}>
                <IconPlus size={15} />
                Add certificate
              </button>
            </div>
          ) : (
            <>
              <input
                type="file"
                ref={directFileInputRef}
                hidden
                accept=".pdf,.png,.jpg,.jpeg,.webp"
                onChange={(e) => void handleDirectFileUpload(e)}
              />
              <ul className="cert-list">
                {certList.map((cert) => (
                  <li key={cert.id} className="cert-row">
                    <span className="cert-row-icon" aria-hidden="true">
                      <IconFileText size={17} />
                    </span>
                    <div className="cert-row-main">
                      <span className="cert-row-title">{cert.name}</span>
                      <span className="cert-row-meta">
                        {cert.issuing_body} · No. {cert.certificate_number} · {formatDate(cert.issue_date)}
                        {cert.expiry_date ? ` – ${formatDate(cert.expiry_date)}` : " · No expiry"}
                      </span>
                    </div>
                    <span className={`badge badge-${cert.verification_status === "verified" ? "success" : cert.verification_status === "rejected" ? "danger" : "pending"}`}>
                      {cert.verification_status === "verified"
                        ? "Verified"
                        : cert.verification_status === "rejected"
                          ? "Rejected"
                          : "In review"}
                    </span>
                    <div className="cert-row-actions">
                      {cert.document_url ? (
                        <button
                          type="button"
                          className="ghost small-btn"
                          onClick={() => setPreviewModalCert(cert)}
                        >
                          View document
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="secondary small-btn"
                          disabled={directUploadLoadingId === cert.id}
                          onClick={() => triggerDirectUpload(cert.id)}
                        >
                          <IconUpload size={13} />
                          {directUploadLoadingId === cert.id ? "Uploading…" : "Attach document"}
                        </button>
                      )}
                      <Menu
                        label={`More actions for ${cert.name}`}
                        items={[
                          {
                            label: "Remove",
                            tone: "danger",
                            onSelect: () => void handleDeleteCertificate(cert.id),
                          },
                        ]}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}

          <Modal
            open={showCertModal}
            onClose={closeCertModal}
            busy={uploadingCert}
            size="lg"
            title="Add certificate"
            description="Documents are reviewed for authenticity before they count toward your trust score."
          >
            <form className="cert-add-form" onSubmit={handleAddCertificate}>
              <div className="field">
                <span className="field-label">Common types</span>
                <div className="cert-quick-types" role="group" aria-label="Common certificate types">
                  {CERT_QUICK_TYPES.map((type) => (
                    <button
                      key={type}
                      type="button"
                      aria-pressed={certForm.name === type}
                      className={`cert-quick-chip ${certForm.name === type ? "selected" : ""}`}
                      onClick={() => setCertForm({ ...certForm, name: type })}
                    >
                      {type.replace(/ Quality Management| Environmental| Declaration| Compliance| Registration| Marking/g, "")}
                    </button>
                  ))}
                </div>
              </div>

              <div className="field-grid field-grid-2">
                <div className="field field-span">
                  <label htmlFor="c-name">Certificate name</label>
                  <input
                    id="c-name"
                    required
                    placeholder="e.g. ISO 9001:2015 Quality Management"
                    value={certForm.name}
                    onChange={(e) => setCertForm({ ...certForm, name: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="c-body">Issuing body</label>
                  <input
                    id="c-body"
                    required
                    placeholder="e.g. SGS, TÜV, BSI"
                    value={certForm.issuing_body}
                    onChange={(e) => setCertForm({ ...certForm, issuing_body: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="c-num">Certificate number</label>
                  <input
                    id="c-num"
                    required
                    placeholder="e.g. CERT-2026-X891"
                    value={certForm.certificate_number}
                    onChange={(e) => setCertForm({ ...certForm, certificate_number: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="c-issue">Issue date</label>
                  <input
                    id="c-issue"
                    type="date"
                    required
                    value={certForm.issue_date}
                    onChange={(e) => setCertForm({ ...certForm, issue_date: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="c-exp">
                    Expiry date <span className="optional">Optional</span>
                  </label>
                  <input
                    id="c-exp"
                    type="date"
                    value={certForm.expiry_date}
                    onChange={(e) => setCertForm({ ...certForm, expiry_date: e.target.value })}
                  />
                </div>
              </div>

              <div className="field">
                <div className="cert-doc-head">
                  <span className="field-label">Document</span>
                  <div className="tabs" role="tablist" aria-label="Document source">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={certDocMode === "upload"}
                      className={certDocMode === "upload" ? "tab active" : "tab"}
                      onClick={() => setCertDocMode("upload")}
                    >
                      Upload file
                    </button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={certDocMode === "url"}
                      className={certDocMode === "url" ? "tab active" : "tab"}
                      onClick={() => setCertDocMode("url")}
                    >
                      Link
                    </button>
                  </div>
                </div>

                {certDocMode === "upload" ? (
                  <>
                    <input
                      type="file"
                      ref={fileInputRef}
                      hidden
                      accept=".pdf,.png,.jpg,.jpeg,.webp"
                      onChange={(e) => {
                        if (e.target.files && e.target.files[0]) {
                          handleFileSelect(e.target.files[0]);
                        }
                      }}
                    />
                    {!selectedFile ? (
                      <div
                        className={`cert-dropzone ${isDragging ? "dragover" : ""}`}
                        onClick={() => fileInputRef.current?.click()}
                        onDragOver={(e) => {
                          e.preventDefault();
                          setIsDragging(true);
                        }}
                        onDragLeave={() => setIsDragging(false)}
                        onDrop={handleDrop}
                        role="button"
                        tabIndex={0}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            fileInputRef.current?.click();
                          }
                        }}
                      >
                        <IconUpload size={18} />
                        <span>
                          <strong>Choose a file</strong> or drag it here
                        </span>
                        <span className="cert-dropzone-hint">PDF, PNG, JPG or WebP, up to 10 MB</span>
                      </div>
                    ) : (
                      <div className="cert-file-pill">
                        <IconFileText size={18} />
                        <div className="cert-file-meta">
                          <span className="cert-file-name">{selectedFile.name}</span>
                          <span className="cert-file-size">{formatFileSize(selectedFile.size)}</span>
                        </div>
                        <button
                          type="button"
                          className="ghost small-btn"
                          onClick={() => fileInputRef.current?.click()}
                        >
                          Replace
                        </button>
                        <button
                          type="button"
                          className="icon-btn"
                          onClick={() => {
                            setSelectedFile(null);
                            if (fileInputRef.current) fileInputRef.current.value = "";
                          }}
                          title="Remove file"
                          aria-label="Remove file"
                        >
                          <IconX size={16} />
                        </button>
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <input
                      id="c-doc"
                      type="url"
                      aria-label="Document URL"
                      placeholder="https://certificates.example.com/iso9001.pdf"
                      value={certForm.document_url}
                      onChange={(e) => setCertForm({ ...certForm, document_url: e.target.value })}
                    />
                    <small>A direct link to a PDF or image.</small>
                  </>
                )}
              </div>

              <div className="modal-actions">
                <button
                  type="button"
                  className="secondary"
                  onClick={closeCertModal}
                  disabled={uploadingCert}
                >
                  Cancel
                </button>
                <button type="submit" disabled={uploadingCert}>
                  {uploadingCert ? "Saving…" : "Add certificate"}
                </button>
              </div>
            </form>
          </Modal>

          <Modal
            open={previewModalCert !== null}
            onClose={() => setPreviewModalCert(null)}
            size="lg"
            title={previewModalCert?.name ?? ""}
            description={
              previewModalCert &&
              `Issued by ${previewModalCert.issuing_body} · No. ${previewModalCert.certificate_number}`
            }
            footer={
              previewModalCert?.document_url && (
                <a
                  href={previewModalCert.document_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="button secondary"
                >
                  <IconExternalLink size={14} />
                  Open in new tab
                </a>
              )
            }
          >
            {previewModalCert && (
              <div className="cert-preview-content">
                {previewModalCert.document_url ? (
                  previewModalCert.document_url.toLowerCase().endsWith(".pdf") ? (
                    <iframe
                      src={previewModalCert.document_url}
                      title={previewModalCert.name}
                      className="cert-preview-frame"
                    />
                  ) : (
                    <img
                      src={previewModalCert.document_url}
                      alt={previewModalCert.name}
                      className="cert-preview-img"
                    />
                  )
                ) : (
                  <p className="muted">No document attached.</p>
                )}
              </div>
            )}
          </Modal>
        </div>
      )}

      {activeTab === "reviews" && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>Ratings &amp; reviews</h2>
              <p className="panel-head-sub">Left by counterparties after a completed delivery.</p>
            </div>
          </div>

          {loadingReviews ? (
            <div className="loading-state">
              <span className="spinner" />
              Loading reviews…
            </div>
          ) : !reviewStats || reviewStats.total_reviews === 0 ? (
            <div className="empty-state">
              <span className="empty-state-icon">
                <IconStar size={18} />
              </span>
              <strong>No reviews yet</strong>
              <p>Complete a trade in the deal room and your counterparty can rate it.</p>
            </div>
          ) : (
            <div className="reviews-overview">
              <div className="rating-summary-box">
                <div className="big-stars">
                  <span className="big-star-num">{reviewStats.average_rating.toFixed(1)}</span>
                  <span className="stars-icons" aria-hidden="true">
                    {"★".repeat(Math.round(reviewStats.average_rating))}
                    <span className="stars-empty">{"★".repeat(5 - Math.round(reviewStats.average_rating))}</span>
                  </span>
                  <span className="muted small">
                    {reviewStats.total_reviews} {reviewStats.total_reviews === 1 ? "review" : "reviews"}
                  </span>
                </div>
                <div className="rating-breakdown-bars">
                  {[5, 4, 3, 2, 1].map((stars) => {
                    const count = reviewStats.rating_breakdown[stars] || 0;
                    const pct = reviewStats.total_reviews
                      ? Math.round((count / reviewStats.total_reviews) * 100)
                      : 0;
                    return (
                      <div key={stars} className="breakdown-bar-row">
                        <span className="bar-label">{stars}</span>
                        <div className="bar-track">
                          <div className="bar-fill" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="bar-count">{count}</span>
                      </div>
                    );
                  })}
                </div>
              </div>

              <ul className="recent-reviews-list">
                {reviewStats.recent_reviews.map((r) => (
                  <li key={r.id} className="review-item">
                    <div className="review-item-head">
                      <strong>{r.reviewer_company || r.reviewer_name || "Verified trader"}</strong>
                      <span className="star-pill" aria-label={`${r.rating} out of 5`}>
                        {"★".repeat(r.rating)}
                        <span className="stars-empty">{"★".repeat(5 - r.rating)}</span>
                      </span>
                      <span className="review-date">{formatDate(r.created_at)}</span>
                    </div>
                    {r.comment && <p className="review-comment">{r.comment}</p>}
                    {(r.communication_rating || r.delivery_rating || r.quality_rating) && (
                      <div className="review-subratings">
                        {r.communication_rating && <span>Communication {r.communication_rating}/5</span>}
                        {r.delivery_rating && <span>Delivery {r.delivery_rating}/5</span>}
                        {r.quality_rating && <span>Quality {r.quality_rating}/5</span>}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
