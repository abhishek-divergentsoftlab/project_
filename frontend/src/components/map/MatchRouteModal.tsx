import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import type { MatchCandidate, RFQ } from "@/types";
import { Modal } from "@/components/ui/Modal";
import {
  calculateDistanceKm,
  checkOceanFeasibility,
  checkRoadFeasibility,
  createMovingVehicleIcon,
  createShipmentWaypointIcon,
  fetchMultiModalRoute,
  getMapTileConfig,
  isDarkTheme,
  type FreightMode,
  type RouteGeometryResult,
} from "@/utils/mapUtils";
import { IconShield } from "@/components/icons";

interface MatchRouteModalProps {
  isOpen: boolean;
  onClose: () => void;
  ownRfq: RFQ | null;
  candidate: MatchCandidate | null;
}

export function MatchRouteModal({
  isOpen,
  onClose,
  ownRfq,
  candidate,
}: MatchRouteModalProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const routeLayerRef = useRef<L.LayerGroup | null>(null);

  // Determine origin and destination coordinates first for feasibility checks
  const origin = useMemo(() => {
    if (!ownRfq?.location?.latitude || !ownRfq?.location?.longitude) {
      return { lat: 22.7196, lng: 75.8577, name: ownRfq?.location?.city || "Your Hub" };
    }
    return {
      lat: Number(ownRfq.location.latitude),
      lng: Number(ownRfq.location.longitude),
      name: [ownRfq.location.city, ownRfq.location.country].filter(Boolean).join(", ") || "Your Facility",
    };
  }, [ownRfq]);

  const dest = useMemo(() => {
    if (!candidate?.location?.latitude || !candidate?.location?.longitude) {
      return { lat: 19.076, lng: 72.8777, name: candidate?.location?.city || "Counterparty Hub" };
    }
    return {
      lat: Number(candidate.location.latitude),
      lng: Number(candidate.location.longitude),
      name: [candidate.location.city, candidate.location.country].filter(Boolean).join(", ") || "Counterparty Hub",
    };
  }, [candidate]);

  const roadFeasibility = useMemo(() => {
    return checkRoadFeasibility(origin.lat, origin.lng, dest.lat, dest.lng, origin.name, dest.name);
  }, [origin, dest]);

  const oceanFeasibility = useMemo(() => {
    return checkOceanFeasibility(origin.lat, origin.lng, dest.lat, dest.lng, origin.name, dest.name);
  }, [origin, dest]);

  // Freight Mode State (Road, Ocean Cargo, Air Cargo)
  const [selectedMode, setSelectedMode] = useState<FreightMode>(() => {
    const pref = candidate?.logistics?.mode?.toLowerCase() || "";
    if (pref.includes("air") || pref.includes("flight") || pref.includes("express") || pref.includes("plane")) return "air";
    if (pref.includes("sea") || pref.includes("ocean") || pref.includes("maritime") || pref.includes("vessel")) return "ocean";

    // If road is unfeasible (e.g. South America to India trans-oceanic), default to ocean cargo
    const isRoadOk = checkRoadFeasibility(origin.lat, origin.lng, dest.lat, dest.lng, origin.name, dest.name).isFeasible;
    if (!isRoadOk) return "ocean";
    return "road";
  });

  const [routeResult, setRouteResult] = useState<RouteGeometryResult | null>(null);
  const [isLoadingRoute, setIsLoadingRoute] = useState(false);

  const directDistanceKm = useMemo(() => {
    if (candidate?.distance_km != null) return candidate.distance_km;
    return calculateDistanceKm(origin.lat, origin.lng, dest.lat, dest.lng);
  }, [candidate, origin, dest]);

  useEffect(() => {
    if (!isOpen || !containerRef.current) return;

    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
      routeLayerRef.current = null;
    }

    const tileConfig = getMapTileConfig();
    const map = L.map(containerRef.current, {
      zoomControl: true,
      scrollWheelZoom: true,
    });

    L.tileLayer(tileConfig.url, {
      attribution: tileConfig.attribution,
      maxZoom: 18,
    }).addTo(map);

    const bounds = L.latLngBounds([[origin.lat, origin.lng], [dest.lat, dest.lng]]);

    // Origin Pin
    L.marker([origin.lat, origin.lng], {
      icon: createShipmentWaypointIcon("origin", false, origin.name),
    })
      .addTo(map)
      .bindPopup(`<b>Your Facility:</b> ${origin.name}`);

    // Candidate Pin
    L.marker([dest.lat, dest.lng], {
      icon: createShipmentWaypointIcon("destination", false, dest.name),
    })
      .addTo(map)
      .bindPopup(`<b>${candidate?.counterparty.company_name || "Counterparty"}:</b> ${dest.name}`);

    map.fitBounds(bounds, { padding: [60, 60], maxZoom: 10 });
    mapRef.current = map;

    // Layer group for route polylines
    const routeGroup = L.layerGroup().addTo(map);
    routeLayerRef.current = routeGroup;

    // Faint provisional path while fetching
    const tempLine = L.polyline([[origin.lat, origin.lng], [dest.lat, dest.lng]], {
      color: selectedMode === "ocean" ? "#059669" : selectedMode === "air" ? "#7c3aed" : "#3b82f6",
      weight: 3,
      opacity: 0.4,
      dashArray: "4, 6",
    }).addTo(routeGroup);

    let active = true;
    setIsLoadingRoute(true);

    fetchMultiModalRoute([[origin.lat, origin.lng], [dest.lat, dest.lng]], selectedMode, origin.name, dest.name)
      .then((res) => {
        if (!active || !mapRef.current) return;
        setRouteResult(res);
        setIsLoadingRoute(false);

        // Remove placeholder line
        routeGroup.removeLayer(tempLine);

        const dark = isDarkTheme();

        if (res.mode === "road") {
          if (!res.isFeasible) {
            // Unfeasible Road Route: Draw subtle warning dashed line across trans-oceanic corridor
            L.polyline([[origin.lat, origin.lng], [dest.lat, dest.lng]], {
              color: "#f59e0b",
              weight: 3.5,
              opacity: 0.85,
              dashArray: "6, 8",
              lineCap: "round",
            }).addTo(routeGroup);

            // Warning Marker at Midpoint
            const midLat = (origin.lat + dest.lat) / 2;
            const midLng = (origin.lng + dest.lng) / 2;
            L.marker([midLat, midLng], {
              icon: L.divIcon({
                html: `
                  <div style="background: rgba(220, 38, 38, 0.92); color: #ffffff; border: 2px solid #ffffff; border-radius: 20px; padding: 3px 10px; font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; gap: 5px; box-shadow: 0 4px 14px rgba(0,0,0,0.35); white-space: nowrap;">
                    <span>⚠️</span> Road Unfeasible: Ocean / Air Required
                  </div>
                `,
                className: "leaflet-custom-div-icon",
                iconSize: [260, 28],
                iconAnchor: [130, 14],
              }),
              zIndexOffset: 400,
            }).addTo(routeGroup);
          } else {
            // Road: Google Maps Style Dual-Layer Highway Polyline
            L.polyline(res.coordinates, {
              color: dark ? "#1e3a8a" : "#1e40af",
              weight: 7,
              opacity: 0.7,
              lineCap: "round",
              lineJoin: "round",
              className: "route-road-casing",
            }).addTo(routeGroup);

            L.polyline(res.coordinates, {
              color: dark ? "#38bdf8" : "#2563eb",
              weight: 4.5,
              opacity: 0.95,
              lineCap: "round",
              lineJoin: "round",
              className: "route-road-core",
            }).addTo(routeGroup);
          }
        } else if (res.mode === "ocean") {
          if (!res.isFeasible) {
            // Unfeasible Ocean Route: Draw subtle warning dashed line instead of coastal sea loop
            L.polyline([[origin.lat, origin.lng], [dest.lat, dest.lng]], {
              color: "#f59e0b",
              weight: 3.5,
              opacity: 0.85,
              dashArray: "6, 8",
              lineCap: "round",
            }).addTo(routeGroup);

            // Warning Marker at Midpoint
            const midLat = (origin.lat + dest.lat) / 2;
            const midLng = (origin.lng + dest.lng) / 2;
            L.marker([midLat, midLng], {
              icon: L.divIcon({
                html: `
                  <div style="background: rgba(220, 38, 38, 0.92); color: #ffffff; border: 2px solid #ffffff; border-radius: 20px; padding: 3px 10px; font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; gap: 5px; box-shadow: 0 4px 14px rgba(0,0,0,0.35); white-space: nowrap;">
                    <span>⚠️</span> Ocean Unfeasible: Direct Road Recommended
                  </div>
                `,
                className: "leaflet-custom-div-icon",
                iconSize: [260, 28],
                iconAnchor: [130, 14],
              }),
              zIndexOffset: 400,
            }).addTo(routeGroup);
          } else {
            // Feasible Ocean Cargo: Deep Sea Wave Line + Emerald Nautical Channel
            L.polyline(res.coordinates, {
              color: dark ? "#064e3b" : "#047857",
              weight: 7,
              opacity: 0.75,
              lineCap: "round",
              lineJoin: "round",
              className: "route-sea-casing",
            }).addTo(routeGroup);

            L.polyline(res.coordinates, {
              color: dark ? "#34d399" : "#059669",
              weight: 4.5,
              opacity: 0.95,
              lineCap: "round",
              lineJoin: "round",
              dashArray: "12, 6",
              className: "route-sea-core",
            }).addTo(routeGroup);

            // If intermodal ports exist, add small port waypoints
            if (res.originPortName && res.drayageOriginKm && res.drayageOriginKm > 20) {
              L.marker(res.coordinates[1] || [origin.lat, origin.lng], {
                icon: createShipmentWaypointIcon("checkpoint", false, res.originPortName),
              })
                .addTo(routeGroup)
                .bindPopup(`<b>Origin Seaport:</b> ${res.originPortName}`);
            }
            if (res.destPortName && res.drayageDestKm && res.drayageDestKm > 20) {
              const destPortIdx = Math.max(0, res.coordinates.length - 2);
              L.marker(res.coordinates[destPortIdx] || [dest.lat, dest.lng], {
                icon: createShipmentWaypointIcon("checkpoint", false, res.destPortName),
              })
                .addTo(routeGroup)
                .bindPopup(`<b>Destination Seaport:</b> ${res.destPortName}`);
            }
          }
        } else {
          // Air Cargo: Supersonic Great-Circle Flight Trajectory Line
          L.polyline(res.coordinates, {
            color: dark ? "#581c87" : "#6b21a8",
            weight: 7,
            opacity: 0.75,
            lineCap: "round",
            lineJoin: "round",
            className: "route-air-casing",
          }).addTo(routeGroup);

          L.polyline(res.coordinates, {
            color: dark ? "#c084fc" : "#9333ea",
            weight: 4.5,
            opacity: 0.95,
            lineCap: "round",
            lineJoin: "round",
            dashArray: "8, 8",
            className: "route-air-core",
          }).addTo(routeGroup);
        }

        // Add Vehicle marker at midpoint if route is feasible
        if (res.isFeasible) {
          const midIdx = Math.floor(res.coordinates.length / 2);
          const midCoord = res.coordinates[midIdx] || [origin.lat, origin.lng];
          L.marker(midCoord, {
            icon: createMovingVehicleIcon(res.mode),
            zIndexOffset: 300,
          }).addTo(routeGroup);
        }

        // Fit map bounds precisely
        const roadBounds = res.isFeasible && res.coordinates.length > 1
          ? L.latLngBounds(res.coordinates)
          : L.latLngBounds([[origin.lat, origin.lng], [dest.lat, dest.lng]]);
        map.fitBounds(roadBounds, { padding: [55, 55], maxZoom: 11 });
      })
      .catch(() => {
        if (active) setIsLoadingRoute(false);
      });

    const timer = setTimeout(() => {
      map.invalidateSize();
    }, 250);

    return () => {
      active = false;
      clearTimeout(timer);
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
        routeLayerRef.current = null;
      }
    };
  }, [isOpen, origin, dest, candidate, selectedMode]);

  if (!isOpen || !candidate) return null;

  const displayDistanceKm = routeResult?.distanceKm ?? directDistanceKm;
  const drivingMinutes = routeResult?.durationMinutes ?? 0;
  const hours = Math.floor(drivingMinutes / 60);
  const mins = drivingMinutes % 60;

  return (
    <Modal
      open={isOpen}
      onClose={onClose}
      title="Counterparty Route & Multi-Modal Logistics Feasibility"
      size="lg"
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
        {/* Logistics Summary Cards */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: "12px",
          }}
        >
          <div className="card" style={{ padding: "12px" }}>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block" }}>
              {selectedMode === "road"
                ? (routeResult && !routeResult.isFeasible ? "Direct Great-Circle Distance" : "Road Driving Distance")
                : selectedMode === "ocean"
                  ? (routeResult && !routeResult.isFeasible ? "Direct Overland Distance" : "Maritime Voyage Distance")
                  : "Air Flight Distance"}
            </span>
            <strong style={{ fontSize: "16px", color: "var(--accent)" }}>
              {Math.round(displayDistanceKm).toLocaleString()} km
            </strong>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block", marginTop: "2px" }}>
              {selectedMode === "road"
                ? (routeResult && !routeResult.isFeasible
                    ? "Trans-oceanic corridor (no road connection)"
                    : routeResult?.isRoadRoute
                      ? `Highway route (${Math.round(directDistanceKm)} km direct)`
                      : "Great-circle distance")
                : selectedMode === "ocean"
                  ? (routeResult && !routeResult.isFeasible
                      ? "Contiguous inland corridor (no sea route)"
                      : routeResult?.nauticalMiles
                        ? `Approx ${routeResult.nauticalMiles.toLocaleString()} nmi nautical`
                        : "Maritime coastal lane")
                  : "Great-circle distance"}
            </span>
          </div>

          <div className="card" style={{ padding: "12px" }}>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block" }}>
              Freight Mode
            </span>
            <strong
              style={{
                fontSize: "14px",
                color: (selectedMode === "road" || selectedMode === "ocean") && routeResult && !routeResult.isFeasible ? "#ef4444" : "var(--text)",
              }}
            >
              {selectedMode === "road"
                ? (routeResult && !routeResult.isFeasible ? "Road Freight (Unfeasible)" : "Direct Road Freight (Trucking)")
                : selectedMode === "ocean"
                  ? (routeResult && !routeResult.isFeasible ? "Ocean Cargo (Unfeasible)" : "Ocean Cargo (Container Vessel)")
                  : "Express Air Cargo (Freighter)"}
            </strong>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block", marginTop: "2px" }}>
              {routeResult && !routeResult.isFeasible && selectedMode === "road"
                ? "Trans-Oceanic — Ship or Plane Required"
                : routeResult && !routeResult.isFeasible && selectedMode === "ocean"
                  ? "Inland Corridor — Road Freight Recommended"
                  : (routeResult?.viaSummary || "Direct Transit")}
            </span>
          </div>

          <div className="card" style={{ padding: "12px" }}>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block" }}>
              Transit Estimate
            </span>
            <strong style={{ fontSize: "14px", color: "var(--text)" }}>
              {selectedMode === "air"
                ? `${hours > 0 ? `${hours}h ` : ""}${mins}m flight time`
                : selectedMode === "ocean"
                  ? (routeResult && !routeResult.isFeasible
                      ? "N/A — Infeasible"
                      : `${routeResult?.transitDaysMin ?? 3}–${routeResult?.transitDaysMax ?? 7} days voyage`)
                  : (routeResult && !routeResult.isFeasible
                      ? "N/A — Infeasible"
                      : drivingMinutes > 0
                        ? `${hours > 0 ? `${hours}h ` : ""}${mins}m driving`
                        : "1–2 days transit")}
            </strong>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block", marginTop: "2px" }}>
              {selectedMode === "road" && routeResult && !routeResult.isFeasible
                ? "No highway connection across oceans"
                : selectedMode === "ocean" && routeResult && !routeResult.isFeasible
                  ? "No sea route between points"
                  : selectedMode === "air"
                    ? "Express dispatch (Same-day / 24h)"
                    : selectedMode === "ocean"
                      ? "Bulk freight / Lowest cost per TEU"
                      : "Commercial dispatch: 1–2 days"}
            </span>
          </div>

          <div className="card" style={{ padding: "12px" }}>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block" }}>
              Logistics & Carbon Footprint
            </span>
            <strong
              style={{
                fontSize: "14px",
                color:
                  (selectedMode === "road" || selectedMode === "ocean") && routeResult && !routeResult.isFeasible
                    ? "var(--text)"
                    : selectedMode === "ocean"
                      ? "var(--success)"
                      : "var(--text)",
              }}
            >
              {selectedMode === "road"
                ? (routeResult && !routeResult.isFeasible ? "Ship or Air Recommended" : "Eco-Standard Ground")
                : selectedMode === "ocean"
                  ? (routeResult && !routeResult.isFeasible ? "Road Freight Recommended" : "Lowest Carbon Emission")
                  : "Express Speed Priority"}
            </strong>
            <span style={{ fontSize: "11px", color: "var(--muted)", display: "block", marginTop: "2px" }}>
              {selectedMode === "road" && routeResult && !routeResult.isFeasible
                ? "Intercontinental route requires Ship or Air"
                : selectedMode === "ocean" && routeResult && !routeResult.isFeasible
                  ? "Direct door-to-door transit"
                  : routeResult?.emissionKgCO2
                    ? `Est. ~${routeResult.emissionKgCO2} kg CO₂ / tonne`
                    : "Standard corridor"}
            </span>
          </div>
        </div>

        {/* Counterparty Strip */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "10px 14px",
            background: "var(--bg-subtle)",
            borderRadius: "8px",
            fontSize: "13px",
          }}
        >
          <div>
            <strong>{candidate.counterparty.company_name}</strong> · {dest.name}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            {candidate.counterparty.gst_verified && (
              <span className="badge badge-success" style={{ fontSize: "11px" }}>
                <IconShield size={12} /> GST Verified
              </span>
            )}
            <span style={{ color: "var(--muted)", fontSize: "12px" }}>
              Match Score: <strong>{(candidate.score.total * 100).toFixed(0)}%</strong>
            </span>
          </div>
        </div>

        {/* Multi-Modal Freight Mode Selector Bar */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "10px",
          }}
        >
          <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--text)" }}>
            Select Logistics Routing Mode:
          </span>
          <div className="freight-mode-tab-group">
            <button
              type="button"
              className={`freight-mode-tab-btn ${selectedMode === "road" ? "active-road" : ""}`}
              onClick={() => setSelectedMode("road")}
              title={roadFeasibility.isFeasible ? "Road Freight" : "Road Freight (Not Feasible across oceans)"}
            >
              <span>🚚</span> Road Freight
              {!roadFeasibility.isFeasible && (
                <span className="unfeasible-tab-pill">Not Feasible</span>
              )}
            </button>
            <button
              type="button"
              className={`freight-mode-tab-btn ${selectedMode === "ocean" ? "active-ocean" : ""}`}
              onClick={() => setSelectedMode("ocean")}
              title={oceanFeasibility.isFeasible ? "Ocean Cargo" : "Ocean Cargo (Not Feasible for this inland corridor)"}
            >
              <span>🚢</span> Ocean Cargo
              {!oceanFeasibility.isFeasible && (
                <span className="unfeasible-tab-pill">Not Feasible</span>
              )}
            </button>
            <button
              type="button"
              className={`freight-mode-tab-btn ${selectedMode === "air" ? "active-air" : ""}`}
              onClick={() => setSelectedMode("air")}
            >
              <span>✈️</span> Air Cargo
            </button>
          </div>
        </div>

        {/* Road Feasibility Advisory Banner */}
        {selectedMode === "road" && routeResult && !routeResult.isFeasible && (
          <div className="route-unfeasible-banner">
            <div className="route-unfeasible-icon">⚠️</div>
            <div className="route-unfeasible-body">
              <div className="route-unfeasible-title">
                Road Freight Not Feasible for this Corridor
              </div>
              <div className="route-unfeasible-desc">
                {routeResult.unfeasibleReason ||
                  "There is no overland road or highway connection between these locations across oceans. Intercontinental ground trucking is impossible."}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap", marginTop: "4px" }}>
                <span style={{ fontSize: "12px", color: "var(--muted)" }}>
                  Recommended: <strong>{routeResult.recommendation || "Ocean Cargo or Air Freight"}</strong>
                </span>
                <button
                  type="button"
                  className="route-unfeasible-action"
                  onClick={() => setSelectedMode("ocean")}
                  style={{ background: "#059669" }}
                >
                  <span>🚢</span> Switch to Ocean Cargo
                </button>
                <button
                  type="button"
                  className="route-unfeasible-action"
                  onClick={() => setSelectedMode("air")}
                  style={{ background: "#7c3aed" }}
                >
                  <span>✈️</span> Switch to Air Cargo
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Ocean Feasibility Advisory Banner */}
        {selectedMode === "ocean" && routeResult && !routeResult.isFeasible && (
          <div className="route-unfeasible-banner">
            <div className="route-unfeasible-icon">⚠️</div>
            <div className="route-unfeasible-body">
              <div className="route-unfeasible-title">
                Ocean Cargo Not Feasible for this Corridor
              </div>
              <div className="route-unfeasible-desc">
                {routeResult.unfeasibleReason ||
                  "There is no ocean or navigable sea route between these locations. Both points are situated on the contiguous mainland."}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap", marginTop: "4px" }}>
                <span style={{ fontSize: "12px", color: "var(--muted)" }}>
                  Recommended: <strong>{routeResult.recommendation || "Direct Road Freight"}</strong>
                </span>
                <button
                  type="button"
                  className="route-unfeasible-action"
                  onClick={() => setSelectedMode("road")}
                >
                  <span>🚚</span> Switch to Direct Road Freight
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Interactive Map View with Floating Directions Overlay */}
        <div style={{ position: "relative" }}>
          <div className="route-map-floating-overlay">
            <div className="route-meta-chips">
              {selectedMode === "road" ? (
                <span
                  className={`route-pill-badge ${routeResult && !routeResult.isFeasible ? "" : "pill-road"}`}
                  style={
                    routeResult && !routeResult.isFeasible
                      ? {
                          background: "rgba(239, 68, 68, 0.2)",
                          color: "#f87171",
                          border: "1px solid rgba(239, 68, 68, 0.4)",
                        }
                      : undefined
                  }
                >
                  {routeResult && !routeResult.isFeasible ? "⚠️ Road Route Unfeasible" : "🛣️ Road Highway Network"}
                </span>
              ) : selectedMode === "ocean" ? (
                <span
                  className={`route-pill-badge ${routeResult && !routeResult.isFeasible ? "" : "pill-sea"}`}
                  style={
                    routeResult && !routeResult.isFeasible
                      ? {
                          background: "rgba(239, 68, 68, 0.2)",
                          color: "#f87171",
                          border: "1px solid rgba(239, 68, 68, 0.4)",
                        }
                      : undefined
                  }
                >
                  {routeResult && !routeResult.isFeasible ? "⚠️ Ocean Route Unfeasible" : "🚢 Maritime Sea Corridor"}
                </span>
              ) : (
                <span className="route-pill-badge pill-air">✈️ Great-Circle Air Corridor</span>
              )}

              {drivingMinutes > 0 && routeResult?.isFeasible && (
                <span className="route-pill-badge pill-time">
                  ⏱️{" "}
                  {selectedMode === "ocean"
                    ? `${routeResult?.transitDaysMin ?? 3}–${routeResult?.transitDaysMax ?? 7} days voyage`
                    : `${hours > 0 ? `${hours}h ` : ""}${mins}m ${selectedMode === "air" ? "flight" : "driving"}`}
                </span>
              )}

              <span className="route-pill-badge pill-distance">
                📍 {Math.round(displayDistanceKm).toLocaleString()} km
                {selectedMode === "ocean" && routeResult?.isFeasible && routeResult?.nauticalMiles
                  ? ` (${routeResult.nauticalMiles} nmi)`
                  : ""}
              </span>
            </div>

            {isLoadingRoute ? (
              <span style={{ fontSize: "12px", color: "var(--muted)", fontStyle: "italic" }}>
                Calculating {selectedMode} corridor...
              </span>
            ) : routeResult?.viaSummary ? (
              <span style={{ fontSize: "12px", color: "var(--muted)" }}>
                Route: <strong style={{ color: "var(--text)" }}>{routeResult.viaSummary}</strong>
              </span>
            ) : null}
          </div>

          <div
            ref={containerRef}
            className="map-canvas-container"
            style={{ height: "380px" }}
            aria-label="Route map between your facility and counterparty"
          />
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}


