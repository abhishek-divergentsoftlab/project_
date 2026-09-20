import { useEffect, useState, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { certifications, kyc, reviews as reviewsApi, users } from "@/api/endpoints";
import { useAuth } from "@/context/useAuth";
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

type Tab = "general" | "kyc" | "certificates" | "reviews";

export function Profile() {
  const { user, refreshUser } = useAuth();
  const [activeTab, setActiveTab] = useState<Tab>("general");

  // General profile form
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

  // KYC verification form
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

  // Certificates state
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

  // Reviews state
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
    });

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
  }, [user]);

  // Load certificates
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

  // Load reviews
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

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setStatus(null);
    setSaving(true);
    try {
      const payload = Object.fromEntries(
        Object.entries(form).map(([key, value]) => [key, value.trim() || null]),
      );
      await users.updateProfile(payload);
      if (user && role !== user.role) await users.updateRole(role);
      await refreshUser();
      setStatus("General profile updated successfully.");
    } catch (err) {
      setError(errorMessage(err, "Could not save the profile"));
    } finally {
      setSaving(false);
    }
  }

  async function handleKycSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setStatus(null);
    setKycSaving(true);
    try {
      const res = await kyc.verify(kycForm);
      await refreshUser();
      setStatus(res.message || "Company verification submitted successfully.");
    } catch (err) {
      setError(errorMessage(err, "Could not verify company KYC"));
    } finally {
      setKycSaving(false);
    }
  }

  async function handleAddCertificate(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setStatus(null);
    try {
      const payload: CertificateCreatePayload = {
        name: certForm.name.trim(),
        issuing_body: certForm.issuing_body.trim(),
        certificate_number: certForm.certificate_number.trim(),
        issue_date: new Date(certForm.issue_date).toISOString(),
        expiry_date: certForm.expiry_date ? new Date(certForm.expiry_date).toISOString() : undefined,
        document_url: certForm.document_url.trim() || undefined,
      };
      const created = await certifications.create(payload);
      setCertList((current) => [created, ...current]);
      setShowCertModal(false);
      setStatus("Certificate added and verified!");
      await refreshUser();
    } catch (err) {
      setError(errorMessage(err, "Could not add certificate"));
    }
  }

  async function handleDeleteCertificate(id: string) {
    if (!window.confirm("Are you sure you want to delete this certificate?")) return;
    try {
      await certifications.delete(id);
      setCertList((current) => current.filter((c) => c.id !== id));
      setStatus("Certificate removed.");
    } catch (err) {
      setError(errorMessage(err, "Could not delete certificate"));
    }
  }

  const activeRole = ROLES.find((option) => option.value === role);
  const kycStatus = user?.profile?.kyc_status ?? "unverified";
  const trustScore = user?.profile?.trust_score ?? 20;

  return (
    <section className="profile-page">
      <div className="section-head">
        <div>
          <h1>Enterprise Profile &amp; Trust</h1>
          <p className="muted">Signed in as {user?.email}</p>
        </div>
        <div className="trust-meter-badge">
          <span className="trust-score-title">Network Trust Score</span>
          <div className="trust-score-val">
            <span className="trust-num">{trustScore}</span> / 100
          </div>
          <span className={`badge badge-kyc-${kycStatus}`}>
            {kycStatus === "verified" ? "🛡️ GST Verified" : kycStatus === "pending" ? "⏳ KYC Pending" : "⚠️ Unverified"}
          </span>
        </div>
      </div>

      {error && <p className="error">{error}</p>}
      {status && <p className="success">{status}</p>}

      {/* Profile Navigation Tabs */}
      <div className="tab-bar">
        <button
          type="button"
          className={activeTab === "general" ? "tab active" : "tab"}
          onClick={() => setActiveTab("general")}
        >
          General Account
        </button>
        <button
          type="button"
          className={activeTab === "kyc" ? "tab active" : "tab"}
          onClick={() => setActiveTab("kyc")}
        >
          Company KYC &amp; GST Verification
        </button>
        <button
          type="button"
          className={activeTab === "certificates" ? "tab active" : "tab"}
          onClick={() => setActiveTab("certificates")}
        >
          Certifications {certList.length > 0 && `(${certList.length})`}
        </button>
        <button
          type="button"
          className={activeTab === "reviews" ? "tab active" : "tab"}
          onClick={() => setActiveTab("reviews")}
        >
          Ratings &amp; Reviews
        </button>
      </div>

      {/* Tab 1: General Account */}
      {activeTab === "general" && (
        <form className="card wide glass-card" onSubmit={handleSubmit}>
          <h2>Account Details</h2>

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

          <label htmlFor="p-name">Contact Person Name</label>
          <input id="p-name" value={form.name} onChange={(e) => update("name", e.target.value)} />

          <label htmlFor="p-company">Trade / Display Company Name</label>
          <input
            id="p-company"
            value={form.company_name}
            onChange={(e) => update("company_name", e.target.value)}
          />

          <label htmlFor="p-phone">Business Phone</label>
          <input
            id="p-phone"
            type="tel"
            autoComplete="tel"
            value={form.phone}
            onChange={(e) => update("phone", e.target.value)}
          />

          <label htmlFor="p-address">Warehouse / Registered Address</label>
          <input
            id="p-address"
            value={form.address}
            onChange={(e) => update("address", e.target.value)}
          />
          <small className="muted">
            Your phone, email and address are revealed only after accepting connection requests.
          </small>

          <div className="row">
            <div>
              <label htmlFor="p-city">City</label>
              <input id="p-city" value={form.city} onChange={(e) => update("city", e.target.value)} />
            </div>
            <div>
              <label htmlFor="p-state">State / Province</label>
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
            A recognized city fills in geographic coordinates for automatic freight and distance calculations.
          </small>

          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save Account Profile"}
          </button>
        </form>
      )}

      {/* Tab 2: Company Verification (KYC & Anti-Fraud) */}
      {activeTab === "kyc" && (
        <form className="card wide glass-card" onSubmit={handleKycSubmit}>
          <div className="form-header-badge">
            <h2>Company Verification &amp; Anti-Fraud</h2>
            <span className={`badge badge-kyc-${kycStatus}`}>
              Status: {kycStatus.toUpperCase()}
            </span>
          </div>
          <p className="muted">
            Providing official tax identifiers (GSTIN) and business registration details safeguards
            the platform against impersonation and awards a verified trust badge on your listings.
          </p>

          <div className="row">
            <div>
              <label htmlFor="kyc-gst">GST Number / Tax ID *</label>
              <input
                id="kyc-gst"
                required
                placeholder="e.g. 27AABCU9603R1ZM (15 chars) or International Tax ID"
                value={kycForm.gst_number}
                onChange={(e) => setKycForm({ ...kycForm, gst_number: e.target.value })}
              />
              <small className="muted">Valid 15-character GSTIN or international VAT/EIN format.</small>
            </div>
            <div>
              <label htmlFor="kyc-legal-name">Legal Business Name (On Tax Certificate) *</label>
              <input
                id="kyc-legal-name"
                required
                placeholder="Official registered entity name"
                value={kycForm.legal_business_name}
                onChange={(e) => setKycForm({ ...kycForm, legal_business_name: e.target.value })}
              />
            </div>
          </div>

          <div className="row">
            <div>
              <label htmlFor="kyc-type">Business Entity Type</label>
              <select
                id="kyc-type"
                value={kycForm.business_type}
                onChange={(e) => setKycForm({ ...kycForm, business_type: e.target.value })}
              >
                <option value="Private Limited">Private Limited (Pvt Ltd)</option>
                <option value="Public Limited">Public Limited (Ltd)</option>
                <option value="Proprietorship">Sole Proprietorship</option>
                <option value="Partnership">Partnership / LLP</option>
                <option value="LLC">LLC (Limited Liability Company)</option>
                <option value="Corporation">Corporation / Corp</option>
              </select>
            </div>
            <div>
              <label htmlFor="kyc-cin">Company Registration / CIN Number</label>
              <input
                id="kyc-cin"
                placeholder="e.g. U72200MH2020PTC123456"
                value={kycForm.registration_number ?? ""}
                onChange={(e) => setKycForm({ ...kycForm, registration_number: e.target.value })}
              />
            </div>
          </div>

          <div className="row">
            <div>
              <label htmlFor="kyc-year">Year Established</label>
              <input
                id="kyc-year"
                type="number"
                min="1850"
                max="2026"
                value={kycForm.year_established ?? 2020}
                onChange={(e) => setKycForm({ ...kycForm, year_established: parseInt(e.target.value, 10) || 2020 })}
              />
            </div>
            <div>
              <label htmlFor="kyc-pan">Corporate PAN Number</label>
              <input
                id="kyc-pan"
                placeholder="e.g. AABCU9603R"
                value={kycForm.pan_number ?? ""}
                onChange={(e) => setKycForm({ ...kycForm, pan_number: e.target.value })}
              />
            </div>
          </div>

          <div className="row">
            <div>
              <label htmlFor="kyc-website">Official Website URL</label>
              <input
                id="kyc-website"
                type="url"
                placeholder="https://company.example.com"
                value={kycForm.website ?? ""}
                onChange={(e) => setKycForm({ ...kycForm, website: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="kyc-signatory">Authorized Signatory / Director Name</label>
              <input
                id="kyc-signatory"
                placeholder="Managing Director / Authorized Representative"
                value={kycForm.signatory_name ?? ""}
                onChange={(e) => setKycForm({ ...kycForm, signatory_name: e.target.value })}
              />
            </div>
          </div>

          <button type="submit" disabled={kycSaving}>
            {kycSaving ? "Authenticating GST & Credentials…" : "Submit & Verify Company"}
          </button>
        </form>
      )}

      {/* Tab 3: Certifications */}
      {activeTab === "certificates" && (
        <div className="card wide glass-card">
          <div className="form-header-badge">
            <div>
              <h2>Compliance &amp; Quality Certificates</h2>
              <p className="muted">
                Add ISO, CE, FDA, GMP, or RoHS compliance credentials to prove product quality to buyers.
              </p>
            </div>
            <button
              type="button"
              className="deal-room-btn"
              onClick={() => setShowCertModal(true)}
            >
              + Add Certificate
            </button>
          </div>

          {loadingCerts ? (
            <p className="muted">Loading certificates…</p>
          ) : certList.length === 0 ? (
            <div className="empty-box">
              <p className="muted">No certifications added yet.</p>
              <small className="muted">
                Sellers with verified certificates receive higher match priority and an enterprise trust badge.
              </small>
            </div>
          ) : (
            <div className="cert-grid">
              {certList.map((cert) => (
                <div key={cert.id} className="cert-card">
                  <div className="cert-head">
                    <span className="cert-icon">📜</span>
                    <strong>{cert.name}</strong>
                    <span className="badge badge-accepted">{cert.verification_status}</span>
                  </div>
                  <div className="cert-body">
                    <p><strong>Issuing Body:</strong> {cert.issuing_body}</p>
                    <p><strong>Certificate #:</strong> {cert.certificate_number}</p>
                    <p className="muted small">
                      Issued: {new Date(cert.issue_date).toLocaleDateString()}
                      {cert.expiry_date && ` · Expires: ${new Date(cert.expiry_date).toLocaleDateString()}`}
                    </p>
                    {cert.document_url && (
                      <a href={cert.document_url} target="_blank" rel="noopener noreferrer" className="cert-link">
                        View Certificate Document &rarr;
                      </a>
                    )}
                  </div>
                  <button
                    type="button"
                    className="secondary danger-btn small-btn"
                    onClick={() => void handleDeleteCertificate(cert.id)}
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Add Certificate Modal */}
          {showCertModal && (
            <div className="modal-backdrop" onClick={() => setShowCertModal(false)}>
              <div className="modal-card" onClick={(e) => e.stopPropagation()}>
                <div className="modal-header">
                  <h3>Add Quality / Compliance Certificate</h3>
                  <button type="button" className="close-btn" onClick={() => setShowCertModal(false)}>
                    &times;
                  </button>
                </div>

                <form onSubmit={handleAddCertificate}>
                  <div>
                    <label htmlFor="c-name">Certificate Name *</label>
                    <input
                      id="c-name"
                      required
                      placeholder="e.g. ISO 9001:2015, CE Mark, FDA Registration"
                      value={certForm.name}
                      onChange={(e) => setCertForm({ ...certForm, name: e.target.value })}
                    />
                  </div>

                  <div className="row">
                    <div>
                      <label htmlFor="c-body">Issuing Body / Registrar *</label>
                      <input
                        id="c-body"
                        required
                        placeholder="e.g. SGS, TÜV Rheinland, BSI, Bureau Veritas"
                        value={certForm.issuing_body}
                        onChange={(e) => setCertForm({ ...certForm, issuing_body: e.target.value })}
                      />
                    </div>
                    <div>
                      <label htmlFor="c-num">Certificate Number *</label>
                      <input
                        id="c-num"
                        required
                        placeholder="e.g. CERT-2026-X891"
                        value={certForm.certificate_number}
                        onChange={(e) => setCertForm({ ...certForm, certificate_number: e.target.value })}
                      />
                    </div>
                  </div>

                  <div className="row">
                    <div>
                      <label htmlFor="c-issue">Issue Date *</label>
                      <input
                        id="c-issue"
                        type="date"
                        required
                        value={certForm.issue_date}
                        onChange={(e) => setCertForm({ ...certForm, issue_date: e.target.value })}
                      />
                    </div>
                    <div>
                      <label htmlFor="c-exp">Expiry Date (Optional)</label>
                      <input
                        id="c-exp"
                        type="date"
                        value={certForm.expiry_date}
                        onChange={(e) => setCertForm({ ...certForm, expiry_date: e.target.value })}
                      />
                    </div>
                  </div>

                  <div>
                    <label htmlFor="c-doc">Document URL (Optional PDF/Verification link)</label>
                    <input
                      id="c-doc"
                      type="url"
                      placeholder="https://certificates.example.com/iso9001.pdf"
                      value={certForm.document_url}
                      onChange={(e) => setCertForm({ ...certForm, document_url: e.target.value })}
                    />
                  </div>

                  <div className="modal-actions">
                    <button type="submit">Verify &amp; Save Certificate</button>
                    <button type="button" className="secondary" onClick={() => setShowCertModal(false)}>
                      Cancel
                    </button>
                  </div>
                </form>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Tab 4: Ratings & Reviews */}
      {activeTab === "reviews" && (
        <div className="card wide glass-card">
          <h2>Network Reputation &amp; Counterparty Reviews</h2>
          <p className="muted">
            Reviews and ratings submitted by trading counterparties after completed and delivered transactions.
          </p>

          {loadingReviews ? (
            <p className="muted">Loading reputation stats…</p>
          ) : !reviewStats || reviewStats.total_reviews === 0 ? (
            <div className="empty-box">
              <p className="muted">No ratings recorded yet.</p>
              <small className="muted">
                Complete and deliver your first B2B transaction in the Deal Room to earn verified star ratings.
              </small>
            </div>
          ) : (
            <div className="reviews-overview">
              <div className="rating-summary-box">
                <div className="big-stars">
                  <span className="big-star-num">{reviewStats.average_rating.toFixed(1)}</span>
                  <span className="stars-icons">★★★★★</span>
                  <span className="muted small">Based on {reviewStats.total_reviews} verified trade{reviewStats.total_reviews > 1 ? "s" : ""}</span>
                </div>
                <div className="rating-breakdown-bars">
                  {[5, 4, 3, 2, 1].map((stars) => {
                    const count = reviewStats.rating_breakdown[stars] || 0;
                    const pct = reviewStats.total_reviews ? Math.round((count / reviewStats.total_reviews) * 100) : 0;
                    return (
                      <div key={stars} className="breakdown-bar-row">
                        <span className="bar-label">{stars} ★</span>
                        <div className="bar-track">
                          <div className="bar-fill" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="bar-count">{count}</span>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="recent-reviews-list">
                <h3>Recent Counterparty Feedback</h3>
                {reviewStats.recent_reviews.map((r) => (
                  <article key={r.id} className="review-item">
                    <div className="review-item-head">
                      <div>
                        <strong>{r.reviewer_company || r.reviewer_name || "Verified Trader"}</strong>
                        <span className="muted small"> &middot; {new Date(r.created_at).toLocaleDateString()}</span>
                      </div>
                      <span className="star-pill">{"★".repeat(r.rating)}{"☆".repeat(5 - r.rating)}</span>
                    </div>
                    {r.comment && <p className="review-comment">&ldquo;{r.comment}&rdquo;</p>}
                    <div className="review-subratings">
                      {r.communication_rating && <span>Communication: {r.communication_rating}/5</span>}
                      {r.delivery_rating && <span>Delivery: {r.delivery_rating}/5</span>}
                      {r.quality_rating && <span>Quality: {r.quality_rating}/5</span>}
                    </div>
                  </article>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
