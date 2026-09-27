import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import {
  calculateDistanceKm,
  checkOceanFeasibility,
  checkRoadFeasibility,
  createMovingVehicleIcon,
  createShipmentWaypointIcon,
  fetchMultiModalRoute,
  getMapTileConfig,
  isDarkTheme,
  MAJOR_GEO_HUBS,
  type FreightMode,
  type RouteGeometryResult,
} from "@/utils/mapUtils";

interface TrackingCheckpoint {
  checkpoint_location?: string;
  status_label?: string;
  timestamp?: string;
  description?: string;
}

interface ShipmentRouteMapProps {
  originCity: string;
  originCountry?: string;
  destinationCity: string;
  destinationCountry?: string;
  trackingEvents?: TrackingCheckpoint[];
  shippingMode?: string;
  carrierName?: string;
  trackingNumber?: string;
  height?: string;
}

// Fallback coordinate lookup for cities
function resolveCityCoords(cityName: string, fallbackDefault: [number, number]): [number, number] {
  if (!cityName) return fallbackDefault;
  const q = cityName.toLowerCase().trim();
  const match = MAJOR_GEO_HUBS.find(
    (h) => h.name.toLowerCase().includes(q) || q.includes(h.name.toLowerCase()),
  );
  if (match) return [match.lat, match.lng];

  // Default hubs for major trade gateways
  if (q.includes("mumbai") || q.includes("bombay")) return [18.9496, 72.9515];
  if (q.includes("indore")) return [22.7196, 75.8577];
  if (q.includes("delhi")) return [28.6139, 77.209];
  if (q.includes("surat")) return [21.1702, 72.8311];
  if (q.includes("ahmedabad")) return [23.0225, 72.5714];
  if (q.includes("pune")) return [18.5204, 73.8567];
  if (q.includes("bangalore") || q.includes("bengaluru")) return [12.9716, 77.5946];
  if (q.includes("chennai")) return [13.0827, 80.2707];
  if (q.includes("kolkata")) return [22.5726, 88.3639];
  if (q.includes("shenzhen")) return [22.5431, 114.0579];
  if (q.includes("dubai")) return [25.0113, 55.0617];
  if (q.includes("singapore")) return [1.2804, 103.8509];
  if (q.includes("santos")) return [-23.9608, -46.3336];
  if (q.includes("sao paulo") || q.includes("são paulo") || q.includes("brazil")) return [-23.5505, -46.6333];
  if (q.includes("buenos aires") || q.includes("argentina")) return [-34.6037, -58.3816];
  if (q.includes("santiago") || q.includes("chile")) return [-33.4489, -70.6693];
  if (q.includes("bogota") || q.includes("colombia")) return [4.711, -74.0721];
  if (q.includes("lima") || q.includes("peru")) return [-12.0464, -77.0428];
  if (q.includes("sydney") || q.includes("australia")) return [-33.8688, 151.2093];

  return fallbackDefault;
}

export function ShipmentRouteMap({
  originCity,
  originCountry = "",
  destinationCity,
  destinationCountry = "",
  trackingEvents = [],
  shippingMode = "road",
  carrierName = "Freight Logistics",
  trackingNumber = "",
  height = "320px",
}: ShipmentRouteMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const routeLayerRef = useRef<L.LayerGroup | null>(null);

  // Compute waypoint coordinates
  const originCoords = useMemo<[number, number]>(() => {
    return resolveCityCoords(originCity, [19.076, 72.8777]);
  }, [originCity]);

  const destCoords = useMemo<[number, number]>(() => {
    return resolveCityCoords(destinationCity, [28.6139, 77.209]);
  }, [destinationCity]);

  const oceanFeasibility = useMemo(() => {
    return checkOceanFeasibility(
      originCoords[0],
      originCoords[1],
      destCoords[0],
      destCoords[1],
      originCity,
      destinationCity,
    );
  }, [originCoords, destCoords, originCity, destinationCity]);

  const roadFeasibility = useMemo(() => {
    return checkRoadFeasibility(
      originCoords[0],
      originCoords[1],
      destCoords[0],
      destCoords[1],
      originCity,
      destinationCity,
    );
  }, [originCoords, destCoords, originCity, destinationCity]);

  const [selectedMode, setSelectedMode] = useState<FreightMode>(() => {
    const m = shippingMode.toLowerCase();
    if (m.includes("air") || m.includes("flight") || m.includes("plane")) return "air";
    if (m.includes("sea") || m.includes("ocean") || m.includes("ship") || m.includes("maritime")) return "ocean";

    // If road is unfeasible (e.g. South America to India across oceans), default to ocean cargo
    const isRoadOk = checkRoadFeasibility(
      originCoords[0],
      originCoords[1],
      destCoords[0],
      destCoords[1],
      originCity,
      destinationCity,
    ).isFeasible;
    if (!isRoadOk) return "ocean";
    return "road";
  });

  const [routeResult, setRouteResult] = useState<RouteGeometryResult | null>(null);

  // Intermediate checkpoints
  const checkpointCoords = useMemo<Array<{ coords: [number, number]; label: string; time?: string }>>(() => {
    return trackingEvents
      .filter((evt) => evt.checkpoint_location)
      .map((evt, idx) => {
        const coords = resolveCityCoords(evt.checkpoint_location!, [
          originCoords[0] + (destCoords[0] - originCoords[0]) * ((idx + 1) / (trackingEvents.length + 1)),
          originCoords[1] + (destCoords[1] - originCoords[1]) * ((idx + 1) / (trackingEvents.length + 1)),
        ]);
        return {
          coords,
          label: evt.checkpoint_location!,
          time: evt.timestamp,
        };
      });
  }, [trackingEvents, originCoords, destCoords]);

  // Total direct distance
  const directDistanceKm = useMemo(() => {
    return calculateDistanceKm(originCoords[0], originCoords[1], destCoords[0], destCoords[1]);
  }, [originCoords, destCoords]);

  // Active position (last checkpoint or origin)
  const activePosition = useMemo<[number, number]>(() => {
    if (checkpointCoords.length > 0) {
      return checkpointCoords[checkpointCoords.length - 1].coords;
    }
    return originCoords;
  }, [checkpointCoords, originCoords]);

  // Initialize and update Map
  useEffect(() => {
    if (!containerRef.current) return;

    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
      routeLayerRef.current = null;
    }

    const tileConfig = getMapTileConfig();
    const map = L.map(containerRef.current, {
      zoomControl: true,
      scrollWheelZoom: false,
    });

    L.tileLayer(tileConfig.url, {
      attribution: tileConfig.attribution,
      maxZoom: 18,
    }).addTo(map);

    const bounds = L.latLngBounds([originCoords, destCoords]);

    // 1. Origin Marker
    const originMarker = L.marker(originCoords, {
      icon: createShipmentWaypointIcon("origin", false, originCity),
    }).addTo(map);
    originMarker.bindPopup(`<b>Origin Hub:</b> ${originCity} ${originCountry}`);

    // 2. Destination Marker
    const destMarker = L.marker(destCoords, {
      icon: createShipmentWaypointIcon("destination", false, destinationCity),
    }).addTo(map);
    destMarker.bindPopup(`<b>Destination Port/Dock:</b> ${destinationCity} ${destinationCountry}`);

    // 3. Intermediate Checkpoints
    checkpointCoords.forEach((cp) => {
      bounds.extend(cp.coords);
      const cpMarker = L.marker(cp.coords, {
        icon: createShipmentWaypointIcon("checkpoint", false, cp.label),
      }).addTo(map);
      cpMarker.bindPopup(`<b>Checkpoint:</b> ${cp.label}${cp.time ? `<br><small>${cp.time}</small>` : ""}`);
    });

    map.fitBounds(bounds, { padding: [45, 45], maxZoom: 11 });
    mapRef.current = map;

    // Layer group for route polylines
    const routeGroup = L.layerGroup().addTo(map);
    routeLayerRef.current = routeGroup;

    const routeWaypoints: [number, number][] = [
      originCoords,
      ...checkpointCoords.map((c) => c.coords),
      destCoords,
    ];

    // Initial placeholder polyline
    const initialLine = L.polyline(routeWaypoints, {
      color: selectedMode === "ocean" ? "#059669" : selectedMode === "air" ? "#7c3aed" : "#3b82f6",
      weight: 3,
      opacity: 0.4,
      dashArray: "4, 6",
    }).addTo(routeGroup);

    // Fetch multi-modal road, ocean, or air route
    let active = true;
    fetchMultiModalRoute(routeWaypoints, selectedMode, originCity, destinationCity).then((res) => {
      if (!active || !mapRef.current) return;
      setRouteResult(res);
      routeGroup.removeLayer(initialLine);

      const dark = isDarkTheme();
      if (res.mode === "road") {
        if (!res.isFeasible) {
          // Unfeasible Road: Draw warning dashed indicator instead of highway across ocean
          L.polyline([originCoords, destCoords], {
            color: "#f59e0b",
            weight: 3.5,
            opacity: 0.85,
            dashArray: "6, 8",
            lineCap: "round",
          }).addTo(routeGroup);

          const midLat = (originCoords[0] + destCoords[0]) / 2;
          const midLng = (originCoords[1] + destCoords[1]) / 2;
          L.marker([midLat, midLng], {
            icon: L.divIcon({
              html: `
                <div style="background: rgba(220, 38, 38, 0.92); color: #ffffff; border: 2px solid #ffffff; border-radius: 20px; padding: 2px 8px; font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; gap: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); white-space: nowrap;">
                  <span>⚠️</span> Road Unfeasible: Ocean / Air Required
                </div>
              `,
              className: "leaflet-custom-div-icon",
              iconSize: [220, 24],
              iconAnchor: [110, 12],
            }),
            zIndexOffset: 400,
          }).addTo(routeGroup);
        } else {
          // Casing polyline
          L.polyline(res.coordinates, {
            color: dark ? "#1e3a8a" : "#1e40af",
            weight: 7,
            opacity: 0.7,
            lineCap: "round",
            lineJoin: "round",
            className: "route-road-casing",
          }).addTo(routeGroup);

          // Core road line
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
          // Unfeasible Ocean: Draw warning dashed indicator instead of ocean loop
          L.polyline([originCoords, destCoords], {
            color: "#f59e0b",
            weight: 3.5,
            opacity: 0.85,
            dashArray: "6, 8",
            lineCap: "round",
          }).addTo(routeGroup);

          const midLat = (originCoords[0] + destCoords[0]) / 2;
          const midLng = (originCoords[1] + destCoords[1]) / 2;
          L.marker([midLat, midLng], {
            icon: L.divIcon({
              html: `
                <div style="background: rgba(220, 38, 38, 0.92); color: #ffffff; border: 2px solid #ffffff; border-radius: 20px; padding: 2px 8px; font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; gap: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); white-space: nowrap;">
                  <span>⚠️</span> Ocean Unfeasible: Inland Corridor
                </div>
              `,
              className: "leaflet-custom-div-icon",
              iconSize: [220, 24],
              iconAnchor: [110, 12],
            }),
            zIndexOffset: 400,
          }).addTo(routeGroup);
        } else {
          // Ocean cargo maritime wave polyline
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
        }
      } else {
        // Air cargo flight vector polyline
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

      // Moving Vehicle Marker at latest position (only if feasible)
      if (res.isFeasible) {
        L.marker(activePosition, {
          icon: createMovingVehicleIcon(res.mode),
          zIndexOffset: 500,
        }).addTo(routeGroup);
      }

      const fullBounds = res.isFeasible && res.coordinates.length > 1
        ? L.latLngBounds(res.coordinates)
        : L.latLngBounds([originCoords, destCoords]);
      mapRef.current.fitBounds(fullBounds, { padding: [45, 45], maxZoom: 11 });
    });

    const timer = setTimeout(() => {
      map.invalidateSize();
    }, 200);

    return () => {
      active = false;
      clearTimeout(timer);
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
        routeLayerRef.current = null;
      }
    };
  }, [originCoords, destCoords, checkpointCoords, activePosition, originCity, originCountry, destinationCity, destinationCountry, selectedMode]);

  const displayDistanceKm = routeResult?.distanceKm ?? directDistanceKm;

  return (
    <div className="shipment-route-map-box">
      <div className="shipment-map-header">
        <div className="shipment-map-title-row" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <span>Transit Route: </span>
            <strong>
              {originCity} → {destinationCity}
            </strong>
          </div>

          {/* Quick Freight Mode Switcher */}
          <div className="freight-mode-tab-group" style={{ padding: "2px", transform: "scale(0.9)", transformOrigin: "right" }}>
            <button
              type="button"
              className={`freight-mode-tab-btn ${selectedMode === "road" ? "active-road" : ""}`}
              onClick={() => setSelectedMode("road")}
              title={roadFeasibility.isFeasible ? "Road Freight" : "Road Freight (Not Feasible across oceans)"}
            >
              <span>🚚</span> Road
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
              <span>🚢</span> Ocean
              {!oceanFeasibility.isFeasible && (
                <span className="unfeasible-tab-pill">Not Feasible</span>
              )}
            </button>
            <button
              type="button"
              className={`freight-mode-tab-btn ${selectedMode === "air" ? "active-air" : ""}`}
              onClick={() => setSelectedMode("air")}
              title="Air Cargo"
            >
              <span>✈️</span> Air
            </button>
          </div>
        </div>

        <div className="shipment-map-meta-strip">
          <span className="shipment-meta-item">
            Carrier: <strong>{carrierName}</strong>
          </span>
          {trackingNumber && (
            <span className="shipment-meta-item">
              AWB: <strong>{trackingNumber}</strong>
            </span>
          )}
          <span className="shipment-meta-item">
            {selectedMode === "ocean"
              ? (routeResult && !routeResult.isFeasible ? "Direct Overland: " : "Sea Lane: ")
              : selectedMode === "road"
                ? (routeResult && !routeResult.isFeasible ? "Direct Distance: " : "Road Route: ")
                : "Air Flight: "}
            <strong>{Math.round(displayDistanceKm).toLocaleString()} km</strong>
            {selectedMode === "ocean" && routeResult?.isFeasible && routeResult?.nauticalMiles ? (
              <span style={{ marginLeft: "4px", color: "var(--muted)" }}>
                ({routeResult.nauticalMiles.toLocaleString()} nmi)
              </span>
            ) : null}
            {routeResult?.durationMinutes && routeResult?.isFeasible ? (
              <span style={{ marginLeft: "4px", color: "var(--accent)" }}>
                (~{selectedMode === "ocean"
                  ? `${routeResult?.transitDaysMin ?? 3}–${routeResult?.transitDaysMax ?? 7}d voyage`
                  : `${Math.floor(routeResult.durationMinutes / 60) > 0 ? `${Math.floor(routeResult.durationMinutes / 60)}h ` : ""}${routeResult.durationMinutes % 60}m`})
              </span>
            ) : null}
          </span>
        </div>
      </div>

      {/* Unfeasible Road Advisory Banner */}
      {selectedMode === "road" && routeResult && !routeResult.isFeasible && (
        <div style={{ padding: "0 16px 12px" }}>
          <div className="route-unfeasible-banner">
            <div className="route-unfeasible-icon">⚠️</div>
            <div className="route-unfeasible-body">
              <div className="route-unfeasible-title">Road Freight Not Feasible for this Corridor</div>
              <div className="route-unfeasible-desc">
                {routeResult.unfeasibleReason ||
                  "There is no overland road or highway connection between these locations across oceans. Ground trucking is impossible."}
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
        </div>
      )}

      {/* Unfeasible Ocean Advisory Banner */}
      {selectedMode === "ocean" && routeResult && !routeResult.isFeasible && (
        <div style={{ padding: "0 16px 12px" }}>
          <div className="route-unfeasible-banner">
            <div className="route-unfeasible-icon">⚠️</div>
            <div className="route-unfeasible-body">
              <div className="route-unfeasible-title">Ocean Cargo Not Feasible for this Corridor</div>
              <div className="route-unfeasible-desc">
                {routeResult.unfeasibleReason ||
                  "There is no ocean or navigable sea route between these locations. Direct highway routing is recommended."}
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
        </div>
      )}

      <div
        ref={containerRef}
        className="map-canvas-container"
        style={{ height, borderRadius: "0 0 14px 14px", border: "none" }}
        aria-label="Shipment live transit route map"
      />
    </div>
  );
}

