import { useCallback, useEffect, useRef, useState } from "react";
import L from "leaflet";
import {
  calculateDistanceKm,
  createCustomLocationPinIcon,
  getMapTileConfig,
  MAJOR_GEO_HUBS,
  reverseGeocodeCoords,
  searchGlobalPlaces,
  type GeoHub,
} from "@/utils/mapUtils";
import { IconMapPin, IconSearch } from "@/components/icons";

export interface LocationSelectResult {
  city: string;
  state: string;
  country: string;
  latitude: number;
  longitude: number;
  formatted?: string;
}

interface LocationMapPickerProps {
  initialLat?: number | null;
  initialLng?: number | null;
  initialCity?: string;
  initialState?: string;
  initialCountry?: string;
  onLocationSelect: (loc: LocationSelectResult) => void;
  height?: string;
  showPresets?: boolean;
}

// Default center: Indore / Central India trade corridor if not specified
const DEFAULT_CENTER: [number, number] = [22.7196, 75.8577];
const DEFAULT_ZOOM = 6;

export function LocationMapPicker({
  initialLat,
  initialLng,
  initialCity = "",
  initialState = "",
  initialCountry = "",
  onLocationSelect,
  height = "320px",
  showPresets = true,
}: LocationMapPickerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const markerRef = useRef<L.Marker | null>(null);

  const [coords, setCoords] = useState<[number, number]>(() => {
    if (initialLat != null && initialLng != null && !isNaN(initialLat) && !isNaN(initialLng)) {
      return [initialLat, initialLng];
    }
    return DEFAULT_CENTER;
  });

  const [locationName, setLocationName] = useState<string>(() => {
    return [initialCity, initialState, initialCountry].filter(Boolean).join(", ") || "Select location on map";
  });

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<GeoHub[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [isLocating, setIsLocating] = useState(false);

  // Sync with prop changes if they arrive later (e.g. edit mode prefill)
  useEffect(() => {
    if (
      initialLat != null &&
      initialLng != null &&
      !isNaN(initialLat) &&
      !isNaN(initialLng) &&
      (coords[0] !== initialLat || coords[1] !== initialLng)
    ) {
      setCoords([initialLat, initialLng]);
      if (initialCity || initialCountry) {
        setLocationName([initialCity, initialState, initialCountry].filter(Boolean).join(", "));
      }
      if (mapRef.current && markerRef.current) {
        markerRef.current.setLatLng([initialLat, initialLng]);
        mapRef.current.setView([initialLat, initialLng], 9);
      }
    }
  }, [initialLat, initialLng, initialCity, initialState, initialCountry, coords]);

  // Handle setting location from coordinates
  const handleLocationUpdate = useCallback(
    async (lat: number, lng: number, overrideCity?: string, overrideState?: string, overrideCountry?: string) => {
      setCoords([lat, lng]);

      let city = overrideCity;
      let state = overrideState;
      let country = overrideCountry;
      let formatted = "";

      if (!city || !country) {
        setIsSearching(true);
        try {
          const res = await reverseGeocodeCoords(lat, lng);
          city = city || res.city;
          state = state || res.state;
          country = country || res.country;
          formatted = res.formatted;
        } finally {
          setIsSearching(false);
        }
      }

      const display = [city, state, country].filter(Boolean).join(", ") || `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
      setLocationName(display);

      onLocationSelect({
        city: city || "",
        state: state || "",
        country: country || "",
        latitude: Math.round(lat * 1000000) / 1000000,
        longitude: Math.round(lng * 1000000) / 1000000,
        formatted: formatted || display,
      });
    },
    [onLocationSelect],
  );

  // Initialize Map
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const tileConfig = getMapTileConfig();
    const map = L.map(containerRef.current, {
      center: coords,
      zoom: initialLat != null ? 9 : DEFAULT_ZOOM,
      zoomControl: true,
      scrollWheelZoom: true,
    });

    L.tileLayer(tileConfig.url, {
      attribution: tileConfig.attribution,
      maxZoom: 19,
    }).addTo(map);

    // Create Draggable Pin
    const pinIcon = createCustomLocationPinIcon("Delivery / Dispatch Hub", true);
    const marker = L.marker(coords, {
      icon: pinIcon,
      draggable: true,
      autoPan: true,
    }).addTo(map);

    marker.on("dragend", () => {
      const pos = marker.getLatLng();
      void handleLocationUpdate(pos.lat, pos.lng);
    });

    // Map Click drops pin
    map.on("click", (e: L.LeafletMouseEvent) => {
      marker.setLatLng(e.latlng);
      map.panTo(e.latlng);
      void handleLocationUpdate(e.latlng.lat, e.latlng.lng);
    });

    mapRef.current = map;
    markerRef.current = marker;

    // Force map redraw in case DOM was animating
    const timeout = setTimeout(() => {
      map.invalidateSize();
    }, 200);

    return () => {
      clearTimeout(timeout);
      map.remove();
      mapRef.current = null;
      markerRef.current = null;
    };
  }, [coords, initialLat, handleLocationUpdate]);

  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Search input change handler with instant local match + debounced global lookup
  function handleSearchChange(val: string) {
    setSearchQuery(val);
    if (!val.trim()) {
      setSearchResults([]);
      return;
    }
    const q = val.toLowerCase().trim();
    const localMatches = MAJOR_GEO_HUBS.filter(
      (h) =>
        h.name.toLowerCase().includes(q) ||
        h.state.toLowerCase().includes(q) ||
        h.country.toLowerCase().includes(q) ||
        h.tag.toLowerCase().includes(q),
    ).slice(0, 6);
    setSearchResults(localMatches);

    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (val.trim().length >= 2) {
      searchTimerRef.current = setTimeout(async () => {
        try {
          const globalResults = await searchGlobalPlaces(val);
          if (globalResults.length > 0) {
            setSearchResults(globalResults);
          }
        } catch {
          // Keep existing local matches on network error
        }
      }, 350);
    }
  }

  // Select a hub from search or preset
  function handleSelectHub(hub: GeoHub) {
    setSearchQuery("");
    setSearchResults([]);
    if (mapRef.current && markerRef.current) {
      markerRef.current.setLatLng([hub.lat, hub.lng]);
      mapRef.current.flyTo([hub.lat, hub.lng], 11, { duration: 1.2 });
    }
    void handleLocationUpdate(hub.lat, hub.lng, hub.name, hub.state, hub.country);
  }

  // Use Device GPS
  function handleLocateMe() {
    if (!navigator.geolocation) {
      alert("Geolocation is not supported by your browser");
      return;
    }
    setIsLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setIsLocating(false);
        const { latitude, longitude } = pos.coords;
        if (mapRef.current && markerRef.current) {
          markerRef.current.setLatLng([latitude, longitude]);
          mapRef.current.flyTo([latitude, longitude], 12, { duration: 1.2 });
        }
        void handleLocationUpdate(latitude, longitude);
      },
      (err) => {
        setIsLocating(false);
        console.warn("Geolocation failed", err);
      },
      { timeout: 8000, enableHighAccuracy: true },
    );
  }

  return (
    <div className="location-picker-widget">
      {/* Search & Locate controls */}
      <div className="picker-search-bar">
        <span className="picker-search-icon">
          <IconSearch size={16} />
        </span>
        <input
          type="text"
          className="picker-search-input"
          placeholder="Search city, port, or logistics hub (e.g. Mumbai, Shenzhen, Dubai)..."
          value={searchQuery}
          onChange={(e) => handleSearchChange(e.target.value)}
        />
        <button
          type="button"
          className="picker-locate-btn"
          onClick={handleLocateMe}
          title="Detect my current location"
          disabled={isLocating}
        >
          <IconMapPin size={15} />
          {isLocating ? "Locating..." : "My Location"}
        </button>

        {/* Auto-suggest dropdown */}
        {searchResults.length > 0 && (
          <div
            style={{
              position: "absolute",
              top: "100%",
              left: 0,
              right: "120px",
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              boxShadow: "var(--shadow)",
              zIndex: 1000,
              marginTop: "4px",
              maxHeight: "220px",
              overflowY: "auto",
            }}
          >
            {searchResults.map((hub) => (
              <div
                key={`${hub.name}-${hub.lat}`}
                style={{
                  padding: "8px 12px",
                  cursor: "pointer",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  borderBottom: "1px solid var(--border)",
                }}
                className="picker-suggestion-item"
                onClick={() => handleSelectHub(hub)}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: "13px", color: "var(--text)" }}>
                    {hub.name}, {hub.state}
                  </div>
                  <div style={{ fontSize: "11px", color: "var(--muted)" }}>
                    {hub.country} · {hub.tag}
                  </div>
                </div>
                <span
                  style={{
                    fontSize: "10px",
                    fontWeight: 700,
                    textTransform: "uppercase",
                    padding: "2px 6px",
                    borderRadius: "4px",
                    background: "var(--accent-soft)",
                    color: "var(--accent)",
                  }}
                >
                  {hub.type.replace("_", " ")}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Preset Hub Chips */}
      {showPresets && (
        <div className="picker-presets-row">
          <span className="preset-chip-label">Key Hubs:</span>
          {MAJOR_GEO_HUBS.slice(0, 7).map((hub) => {
            const isNear = calculateDistanceKm(coords[0], coords[1], hub.lat, hub.lng) < 25;
            return (
              <button
                key={hub.name}
                type="button"
                className={`preset-chip-btn ${isNear ? "active" : ""}`}
                onClick={() => handleSelectHub(hub)}
              >
                {hub.name}
              </button>
            );
          })}
        </div>
      )}

      {/* Interactive Leaflet Map */}
      <div
        ref={containerRef}
        className="map-canvas-container"
        style={{ height }}
        aria-label="Interactive location selection map"
      />

      {/* Status Bar */}
      <div className="picker-status-bar">
        <div className="picker-status-location">
          <IconMapPin size={14} />
          <span>{isSearching ? "Detecting address..." : locationName}</span>
        </div>
        <div className="picker-status-coords">
          Lat: {coords[0].toFixed(5)}, Lng: {coords[1].toFixed(5)} · Click or drag pin to adjust
        </div>
      </div>
    </div>
  );
}
