import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import type { CatalogItem } from "@/types";
import {
  createMarketplacePinIcon,
  formatDistanceKm,
  getMapTileConfig,
} from "@/utils/mapUtils";
import {
  IconClose,
  IconMapPin,
  IconShield,
} from "@/components/icons";

interface MarketplaceMapProps {
  items: CatalogItem[];
  selectedItem: CatalogItem | null;
  onSelectItem: (item: CatalogItem | null) => void;
  onConnect: (rfqId: string) => void;
  connectingId: string | null;
  userLocation?: { lat: number; lng: number } | null;
  radiusKm?: number | null;
  onRadiusChange?: (radius: number | null) => void;
  onQuickView?: (item: CatalogItem) => void;
}

const RADIUS_OPTIONS = [
  { value: null, label: "All Regions (Global)" },
  { value: 100, label: "Within 100 km (Metro & Local)" },
  { value: 250, label: "Within 250 km (Regional Road)" },
  { value: 500, label: "Within 500 km (State Corridor)" },
  { value: 1000, label: "Within 1,000 km (Domestic)" },
  { value: 2500, label: "Within 2,500 km (Cross-Country)" },
];

export function MarketplaceMap({
  items,
  selectedItem,
  onSelectItem,
  onConnect,
  connectingId,
  userLocation,
  radiusKm = null,
  onRadiusChange,
  onQuickView,
}: MarketplaceMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const markersLayerRef = useRef<L.LayerGroup | null>(null);
  const radiusCircleRef = useRef<L.Circle | null>(null);

  const [activeRadius, setActiveRadius] = useState<number | null>(radiusKm);
  const [splitMode, setSplitMode] = useState<"split" | "map-only">("split");

  // Items with valid coordinates
  const geocodedItems = useMemo(() => {
    return items.filter(
      (item) =>
        item.location?.latitude != null &&
        item.location?.longitude != null &&
        !isNaN(Number(item.location.latitude)) &&
        !isNaN(Number(item.location.longitude)),
    );
  }, [items]);

  // Center coordinate reference: userLocation, or first item, or central India
  const centerCoord = useMemo<[number, number]>(() => {
    if (userLocation?.lat != null && userLocation?.lng != null) {
      return [userLocation.lat, userLocation.lng];
    }
    if (geocodedItems.length > 0) {
      return [
        Number(geocodedItems[0].location!.latitude),
        Number(geocodedItems[0].location!.longitude),
      ];
    }
    return [22.7196, 75.8577]; // Indore default
  }, [userLocation, geocodedItems]);

  // Initialize Leaflet Map
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const tileConfig = getMapTileConfig();
    const map = L.map(containerRef.current, {
      center: centerCoord,
      zoom: 5,
      zoomControl: true,
      scrollWheelZoom: true,
    });

    L.tileLayer(tileConfig.url, {
      attribution: tileConfig.attribution,
      maxZoom: 18,
    }).addTo(map);

    const markersGroup = L.layerGroup().addTo(map);
    markersLayerRef.current = markersGroup;
    mapRef.current = map;

    // Invalidate size on initial load
    const timer = setTimeout(() => {
      map.invalidateSize();
    }, 250);

    return () => {
      clearTimeout(timer);
      map.remove();
      mapRef.current = null;
      markersLayerRef.current = null;
    };
  }, [centerCoord]);

  // Render Markers on Map whenever geocoded items or selectedItem change
  useEffect(() => {
    const map = mapRef.current;
    const group = markersLayerRef.current;
    if (!map || !group) return;

    group.clearLayers();

    if (geocodedItems.length === 0) return;

    const bounds = L.latLngBounds([]);

    geocodedItems.forEach((item) => {
      const lat = Number(item.location!.latitude);
      const lng = Number(item.location!.longitude);
      const isSelected = selectedItem?.id === item.id;

      const marker = L.marker([lat, lng], {
        icon: createMarketplacePinIcon(item, isSelected),
        zIndexOffset: isSelected ? 1000 : 10,
      });

      marker.on("click", (e) => {
        L.DomEvent.stopPropagation(e);
        onSelectItem(item);
        map.panTo([lat, lng]);
      });

      marker.addTo(group);
      bounds.extend([lat, lng]);
    });

    // Auto fit bounds to visible markers
    if (bounds.isValid() && !selectedItem) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 12 });
    }
  }, [geocodedItems, selectedItem, onSelectItem]);

  // Draw Radius Circle when activeRadius is set
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    if (radiusCircleRef.current) {
      map.removeLayer(radiusCircleRef.current);
      radiusCircleRef.current = null;
    }

    if (activeRadius && activeRadius > 0) {
      const circleCenter = userLocation
        ? [userLocation.lat, userLocation.lng]
        : centerCoord;

      const circle = L.circle(circleCenter as [number, number], {
        radius: activeRadius * 1000,
        color: "#2563eb",
        fillColor: "#3b82f6",
        fillOpacity: 0.08,
        weight: 1.5,
        dashArray: "6, 6",
      }).addTo(map);

      radiusCircleRef.current = circle;
      map.fitBounds(circle.getBounds(), { padding: [20, 20] });
    }
  }, [activeRadius, userLocation, centerCoord]);

  // Fly to selected item if chosen from card list
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selectedItem || !selectedItem.location?.latitude || !selectedItem.location?.longitude) return;

    const lat = Number(selectedItem.location.latitude);
    const lng = Number(selectedItem.location.longitude);
    map.flyTo([lat, lng], 10, { duration: 1 });
  }, [selectedItem]);

  function handleRadiusChange(val: number | null) {
    setActiveRadius(val);
    onRadiusChange?.(val);
  }

  function handleResetView() {
    const map = mapRef.current;
    if (!map || geocodedItems.length === 0) return;
    const bounds = L.latLngBounds(
      geocodedItems.map((item) => [
        Number(item.location!.latitude),
        Number(item.location!.longitude),
      ]),
    );
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 12 });
    }
  }

  return (
    <div className="marketplace-map-layout">
      {/* Top Map Toolbar */}
      <div className="map-controls-toolbar">
        <div className="map-toolbar-left">
          {/* Radius selector */}
          <div className="map-radius-selector">
            <IconMapPin size={15} />
            <select
              className="map-radius-select"
              value={activeRadius ?? ""}
              onChange={(e) =>
                handleRadiusChange(e.target.value ? Number(e.target.value) : null)
              }
              aria-label="Filter listings by distance radius"
            >
              {RADIUS_OPTIONS.map((opt) => (
                <option key={opt.label} value={opt.value ?? ""}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          {/* Map Legend */}
          <div className="map-legend-pills">
            <span className="legend-pill seller">
              <span className="legend-dot" />
              Sellers ({geocodedItems.filter((i) => i.role === "seller").length})
            </span>
            <span className="legend-pill buyer">
              <span className="legend-dot" />
              Buyers ({geocodedItems.filter((i) => i.role === "buyer").length})
            </span>
          </div>
        </div>

        {/* View toggles & actions */}
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={handleResetView}
            title="Reset map view to fit all listings"
          >
            Fit All Listings
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setSplitMode(splitMode === "split" ? "map-only" : "split")}
          >
            {splitMode === "split" ? "Full Map" : "Show Side List"}
          </button>
        </div>
      </div>

      {/* Split View Container */}
      <div
        className="map-explorer-split-container"
        style={{
          gridTemplateColumns: splitMode === "map-only" ? "1fr" : undefined,
        }}
      >
        {/* Interactive Map Pane */}
        <div className="map-explorer-map-pane">
          <div
            ref={containerRef}
            className="map-canvas-container"
            style={{ height: "100%", minHeight: "520px" }}
          />

          {/* Selected Item Floating Drawer */}
          {selectedItem && (
            <div className="map-selected-drawer">
              <div className="drawer-header">
                <div className="drawer-title-row">
                  <div className="drawer-badges-row">
                    <span
                      className={`badge badge-${selectedItem.role === "seller" ? "seller" : "buyer"}`}
                    >
                      {selectedItem.role === "seller" ? "Selling" : "Buying"}
                    </span>
                    <span className="badge badge-neutral">{selectedItem.category}</span>
                    {selectedItem.counterparty.kyc_status === "verified" && (
                      <span className="badge badge-success" title="Verified Trade Entity">
                        <IconShield size={12} /> Verified
                      </span>
                    )}
                  </div>
                  <h4 className="drawer-item-title">{selectedItem.title}</h4>
                  <div style={{ fontSize: "12px", color: "var(--muted)" }}>
                    {selectedItem.counterparty.company_name} ·{" "}
                    {[selectedItem.location?.city, selectedItem.location?.country]
                      .filter(Boolean)
                      .join(", ") || "Location on request"}
                  </div>
                </div>
                <button
                  type="button"
                  className="drawer-close-btn"
                  onClick={() => onSelectItem(null)}
                  title="Close preview"
                >
                  <IconClose size={18} />
                </button>
              </div>

              {/* Specs Grid */}
              <div className="drawer-specs-grid">
                <div className="drawer-spec-item">
                  <span className="drawer-spec-label">Target Price</span>
                  <span className="drawer-spec-val">
                    {selectedItem.price_target
                      ? `${selectedItem.price_target.currency} ${selectedItem.price_target.amount.toLocaleString()}`
                      : "Negotiable"}
                  </span>
                </div>
                <div className="drawer-spec-item">
                  <span className="drawer-spec-label">Quantity</span>
                  <span className="drawer-spec-val">
                    {selectedItem.quantity
                      ? `${selectedItem.quantity.value.toLocaleString()} ${selectedItem.quantity.unit}`
                      : "Flexible"}
                  </span>
                </div>
                <div className="drawer-spec-item">
                  <span className="drawer-spec-label">Trust Score</span>
                  <span className="drawer-spec-val" style={{ color: "var(--success)" }}>
                    {selectedItem.counterparty.trust_score}/100
                  </span>
                </div>
              </div>

              {/* Action row */}
              <div className="drawer-actions-row">
                <div className="drawer-distance-indicator">
                  <IconMapPin size={14} />
                  <span>
                    {selectedItem.distance_km != null
                      ? formatDistanceKm(selectedItem.distance_km)
                      : selectedItem.location?.city
                        ? selectedItem.location.city
                        : "Geocoded Point"}
                  </span>
                </div>

                <div style={{ display: "flex", gap: "8px" }}>
                  {onQuickView && (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => onQuickView(selectedItem)}
                    >
                      Full Details
                    </button>
                  )}
                  <button
                    type="button"
                    className="drawer-btn-connect"
                    onClick={() => onConnect(selectedItem.id)}
                    disabled={
                      connectingId === selectedItem.id ||
                      selectedItem.counterparty.connection_status === "accepted" ||
                      selectedItem.counterparty.connection_status === "pending"
                    }
                  >
                    {selectedItem.counterparty.connection_status === "accepted"
                      ? "Connected"
                      : selectedItem.counterparty.connection_status === "pending"
                        ? "Request Sent"
                        : connectingId === selectedItem.id
                          ? "Connecting..."
                          : selectedItem.role === "seller"
                            ? "Request Quote"
                            : "Submit Offer"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Side Cards Pane in Split Mode */}
        {splitMode === "split" && (
          <div className="map-explorer-cards-pane">
            <div
              style={{
                fontSize: "12px",
                fontWeight: 600,
                color: "var(--muted)",
                padding: "4px 8px",
              }}
            >
              Showing {geocodedItems.length} locations on map
            </div>
            {geocodedItems.map((item) => {
              const isSelected = selectedItem?.id === item.id;
              return (
                <div
                  key={item.id}
                  className={`card ${isSelected ? "card-selected" : ""}`}
                  style={{
                    padding: "12px",
                    cursor: "pointer",
                    border: isSelected
                      ? "2px solid var(--accent)"
                      : "1px solid var(--border)",
                    transition: "all 0.15s ease",
                  }}
                  onClick={() => onSelectItem(item)}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "flex-start",
                      gap: "6px",
                      marginBottom: "4px",
                    }}
                  >
                    <span
                      className={`badge badge-${item.role === "seller" ? "seller" : "buyer"}`}
                      style={{ fontSize: "10px", padding: "1px 6px" }}
                    >
                      {item.role === "seller" ? "Sell" : "Buy"}
                    </span>
                    <span
                      style={{
                        fontSize: "12px",
                        fontWeight: 700,
                        color: "var(--text)",
                      }}
                    >
                      {item.price_target
                        ? `${item.price_target.currency} ${item.price_target.amount.toLocaleString()}`
                        : "Negotiable"}
                    </span>
                  </div>

                  <h5
                    style={{
                      fontSize: "13px",
                      fontWeight: 600,
                      margin: "4px 0",
                      lineHeight: 1.3,
                    }}
                  >
                    {item.title}
                  </h5>

                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginTop: "6px",
                      fontSize: "11px",
                      color: "var(--muted)",
                    }}
                  >
                    <span>
                      {item.location?.city || item.counterparty.city || "Commercial Hub"}
                    </span>
                    {item.distance_km != null && (
                      <span style={{ color: "var(--accent)", fontWeight: 600 }}>
                        {Math.round(item.distance_km)} km away
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
