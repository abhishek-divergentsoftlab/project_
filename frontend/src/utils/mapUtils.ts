import L from "leaflet";
import type { CatalogItem } from "@/types";

export interface GeoHub {
  name: string;
  state: string;
  country: string;
  lat: number;
  lng: number;
  type: "industrial_hub" | "seaport" | "inland_depot" | "metro";
  tag: string;
}

export const MAJOR_GEO_HUBS: GeoHub[] = [
  // India Core Corridors & Industrial Hubs
  { name: "Indore", state: "Madhya Pradesh", country: "India", lat: 22.7196, lng: 75.8577, type: "industrial_hub", tag: "Central Hub / Pharma" },
  { name: "Bhopal", state: "Madhya Pradesh", country: "India", lat: 23.2599, lng: 77.4126, type: "industrial_hub", tag: "Heavy Electricals / Auto" },
  { name: "Mumbai (JNPT)", state: "Maharashtra", country: "India", lat: 18.9496, lng: 72.9515, type: "seaport", tag: "Major Container Port" },
  { name: "Pune", state: "Maharashtra", country: "India", lat: 18.5204, lng: 73.8567, type: "industrial_hub", tag: "Automotive & Engineering" },
  { name: "Surat", state: "Gujarat", country: "India", lat: 21.1702, lng: 72.8311, type: "industrial_hub", tag: "Textile & Gems Hub" },
  { name: "Ahmedabad", state: "Gujarat", country: "India", lat: 23.0225, lng: 72.5714, type: "industrial_hub", tag: "Chemicals & Machinery" },
  { name: "Mundra Port", state: "Gujarat", country: "India", lat: 22.7441, lng: 69.7226, type: "seaport", tag: "Deep-Water Mega Port" },
  { name: "Delhi NCR", state: "Delhi", country: "India", lat: 28.6139, lng: 77.209, type: "metro", tag: "Northern Trade Depot" },
  { name: "Gurugram", state: "Haryana", country: "India", lat: 28.4595, lng: 77.0266, type: "industrial_hub", tag: "Auto Components / Tech" },
  { name: "Ludhiana", state: "Punjab", country: "India", lat: 30.901, lng: 75.8573, type: "industrial_hub", tag: "Textiles, Cycles, Steel" },
  { name: "Panipat", state: "Haryana", country: "India", lat: 29.3909, lng: 76.9635, type: "industrial_hub", tag: "Textile Recycling Hub" },
  { name: "Bengaluru", state: "Karnataka", country: "India", lat: 12.9716, lng: 77.5946, type: "metro", tag: "Electronics & Precision" },
  { name: "Chennai Port", state: "Tamil Nadu", country: "India", lat: 13.0827, lng: 80.2707, type: "seaport", tag: "Automotive & Sea Hub" },
  { name: "Tiruppur", state: "Tamil Nadu", country: "India", lat: 11.1085, lng: 77.3411, type: "industrial_hub", tag: "Knitwear Export Capital" },
  { name: "Coimbatore", state: "Tamil Nadu", country: "India", lat: 11.0168, lng: 76.9558, type: "industrial_hub", tag: "Pumps & Foundry Capital" },
  { name: "Hyderabad", state: "Telangana", country: "India", lat: 17.385, lng: 78.4867, type: "industrial_hub", tag: "Pharma City & Bulk Drugs" },
  { name: "Kolkata", state: "West Bengal", country: "India", lat: 22.5726, lng: 88.3639, type: "seaport", tag: "Eastern River Port" },

  // International Export & Maritime Gateways
  { name: "Shenzhen", state: "Guangdong", country: "China", lat: 22.5431, lng: 114.0579, type: "seaport", tag: "Global Hardware & Electronics" },
  { name: "Ningbo-Zhoushan", state: "Zhejiang", country: "China", lat: 29.8683, lng: 121.544, type: "seaport", tag: "World's Busiest Cargo Port" },
  { name: "Shanghai", state: "Shanghai", country: "China", lat: 31.2304, lng: 121.4737, type: "seaport", tag: "Global Container Terminal" },
  { name: "Yiwu", state: "Zhejiang", country: "China", lat: 29.3069, lng: 120.0759, type: "inland_depot", tag: "Wholesale Commodities Hub" },
  { name: "Dubai (Jebel Ali)", state: "Dubai", country: "United Arab Emirates", lat: 25.0113, lng: 55.0617, type: "seaport", tag: "Middle East Mega Transshipment" },
  { name: "Singapore Port", state: "Singapore", country: "Singapore", lat: 1.2804, lng: 103.8509, type: "seaport", tag: "Global Bunker & Straits Hub" },
  { name: "Ho Chi Minh City", state: "Ho Chi Minh", country: "Vietnam", lat: 10.8231, lng: 106.6297, type: "seaport", tag: "Garments & Electronics Assembly" },
  { name: "Rotterdam", state: "South Holland", country: "Netherlands", lat: 51.9244, lng: 4.4777, type: "seaport", tag: "Europe's Largest Port" },
  { name: "Hamburg", state: "Hamburg", country: "Germany", lat: 53.5511, lng: 9.9937, type: "seaport", tag: "Northern European Rail & Sea" },
  { name: "Los Angeles", state: "California", country: "United States", lat: 33.7432, lng: -118.2673, type: "seaport", tag: "US Pacific Gateway" },
  { name: "Newark / NY", state: "New Jersey", country: "United States", lat: 40.7357, lng: -74.1724, type: "seaport", tag: "US Atlantic Maritime Hub" },
  { name: "Chicago", state: "Illinois", country: "United States", lat: 41.8781, lng: -87.6298, type: "inland_depot", tag: "North American Rail Crossroad" },
  { name: "Santos / São Paulo", state: "São Paulo", country: "Brazil", lat: -23.9608, lng: -46.3336, type: "seaport", tag: "South America's Largest Seaport" },
  { name: "Buenos Aires Port", state: "Buenos Aires", country: "Argentina", lat: -34.5833, lng: -58.3667, type: "seaport", tag: "South American Atlantic Gateway" },
  { name: "Sydney Port Botany", state: "New South Wales", country: "Australia", lat: -33.9744, lng: 151.2186, type: "seaport", tag: "Australian Trans-Pacific Hub" },
];

/** Haversine formula to compute great-circle distance in kilometers */
export function calculateDistanceKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371; // Earth radius in km
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return Math.round(R * c * 10) / 10;
}

export function formatDistanceKm(km: number | null | undefined): string {
  if (km == null || isNaN(km)) return "Distance unknown";
  if (km < 1) return "< 1 km away";
  if (km < 50) return `${Math.round(km)} km (Local Metro)`;
  if (km < 500) return `${Math.round(km)} km (Direct Road)`;
  if (km < 2500) return `${Math.round(km)} km (Domestic Freight)`;
  return `${Math.round(km).toLocaleString()} km (Intermodal / Maritime)`;
}

/** Check if current document has dark theme active */
export function isDarkTheme(): boolean {
  if (typeof document === "undefined") return false;
  return (
    document.documentElement.getAttribute("data-theme") === "dark" ||
    document.body.classList.contains("dark") ||
    window.matchMedia?.("(prefers-color-scheme: dark)").matches
  );
}

/** Get appropriate styled map tile layer based on dark/light theme and configured API key */
export function getMapTileConfig(): { url: string; attribution: string } {
  const dark = isDarkTheme();
  const mapboxToken = import.meta.env.VITE_MAPBOX_TOKEN;
  const geoapifyKey = import.meta.env.VITE_GEOAPIFY_API_KEY;

  // 1. Mapbox (High-res Retina vector/raster tiles if VITE_MAPBOX_TOKEN provided)
  if (mapboxToken) {
    const styleId = dark ? "mapbox/dark-v11" : "mapbox/streets-v12";
    return {
      url: `https://api.mapbox.com/styles/v1/${styleId}/tiles/256/{z}/{x}/{y}@2x?access_token=${mapboxToken}`,
      attribution:
        '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    };
  }

  // 2. Geoapify (if VITE_GEOAPIFY_API_KEY provided)
  if (geoapifyKey) {
    const styleId = dark ? "dark-matter-purple-roads" : "osm-bright";
    return {
      url: `https://maps.geoapify.com/v1/tile/${styleId}/{z}/{x}/{y}.png?apiKey=${geoapifyKey}`,
      attribution:
        'Powered by <a href="https://www.geoapify.com/" target="_blank">Geoapify</a> | &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    };
  }

  // 3. Built-in free CartoDB Voyager / Dark Matter (zero configuration, no API key needed)
  if (dark) {
    return {
      url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    };
  }
  return {
    url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  };
}

/** Instant local reverse geocoder against our known trade gazetteer */
export function findNearestHub(lat: number, lng: number, thresholdKm = 40): GeoHub | null {
  let closest: GeoHub | null = null;
  let minDistance = Infinity;

  for (const hub of MAJOR_GEO_HUBS) {
    const dist = calculateDistanceKm(lat, lng, hub.lat, hub.lng);
    if (dist < minDistance && dist <= thresholdKm) {
      minDistance = dist;
      closest = hub;
    }
  }

  return closest;
}

/** Reverse geocoding helper that combines fast local gazetteer with Mapbox/Geoapify/Nominatim */
export async function reverseGeocodeCoords(
  lat: number,
  lng: number,
): Promise<{ city: string; state: string; country: string; formatted: string }> {
  // 1. Fast check in local gazetteer (<35km)
  const localMatch = findNearestHub(lat, lng, 35);
  if (localMatch) {
    return {
      city: localMatch.name,
      state: localMatch.state,
      country: localMatch.country,
      formatted: `${localMatch.name}, ${localMatch.state}, ${localMatch.country}`,
    };
  }

  const mapboxToken = import.meta.env.VITE_MAPBOX_TOKEN;
  const geoapifyKey = import.meta.env.VITE_GEOAPIFY_API_KEY;

  // 2. Mapbox reverse geocode if token configured
  if (mapboxToken) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3500);
      const resp = await fetch(
        `https://api.mapbox.com/geocoding/v5/mapbox.places/${lng},${lat}.json?access_token=${mapboxToken}&types=place,locality,region,country&limit=1`,
        { signal: controller.signal },
      );
      clearTimeout(timeout);
      if (resp.ok) {
        const data = await resp.json();
        if (data.features && data.features.length > 0) {
          const feat = data.features[0];
          let city = "";
          let state = "";
          let country = "";
          for (const ctx of feat.context || []) {
            if (ctx.id.startsWith("place") || ctx.id.startsWith("locality")) city = ctx.text;
            if (ctx.id.startsWith("region")) state = ctx.text;
            if (ctx.id.startsWith("country")) country = ctx.text;
          }
          if (!city && feat.place_type?.includes("place")) city = feat.text;
          return {
            city,
            state,
            country,
            formatted: feat.place_name || [city, state, country].filter(Boolean).join(", "),
          };
        }
      }
    } catch {
      // fallback
    }
  }

  // 3. Geoapify reverse geocode if key configured
  if (geoapifyKey) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3500);
      const resp = await fetch(
        `https://api.geoapify.com/v1/geocode/reverse?lat=${lat}&lon=${lng}&apiKey=${geoapifyKey}`,
        { signal: controller.signal },
      );
      clearTimeout(timeout);
      if (resp.ok) {
        const data = await resp.json();
        if (data.features && data.features.length > 0) {
          const prop = data.features[0].properties;
          const city = prop.city || prop.county || prop.suburb || "";
          return {
            city,
            state: prop.state || "",
            country: prop.country || "",
            formatted: prop.formatted || [city, prop.state, prop.country].filter(Boolean).join(", "),
          };
        }
      }
    } catch {
      // fallback
    }
  }

  // 4. OpenStreetMap Nominatim with safety timeout
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3500);
    const resp = await fetch(
      `https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lng}&zoom=10&addressdetails=1`,
      {
        headers: { "Accept-Language": "en" },
        signal: controller.signal,
      },
    );
    clearTimeout(timeout);

    if (resp.ok) {
      const data = await resp.json();
      const addr = data.address || {};
      const city =
        addr.city ||
        addr.town ||
        addr.village ||
        addr.county ||
        addr.state_district ||
        addr.suburb ||
        "";
      const state = addr.state || "";
      const country = addr.country || "";

      return {
        city,
        state,
        country,
        formatted: [city, state, country].filter(Boolean).join(", ") || `${lat.toFixed(4)}, ${lng.toFixed(4)}`,
      };
    }
  } catch {
    // Graceful network or timeout failure
  }

  // Fallback to nearest hub regardless of distance
  const nearest = findNearestHub(lat, lng, 300);
  if (nearest) {
    return {
      city: nearest.name,
      state: nearest.state,
      country: nearest.country,
      formatted: `Near ${nearest.name}, ${nearest.country}`,
    };
  }

  return {
    city: "",
    state: "",
    country: "",
    formatted: `Location (${lat.toFixed(4)}, ${lng.toFixed(4)})`,
  };
}

/**
 * Search places globally using local gazetteer, with Mapbox / Geoapify / Nominatim live lookup
 */
export async function searchGlobalPlaces(query: string): Promise<GeoHub[]> {
  const q = query.trim().toLowerCase();
  if (!q) return [];

  // Match in local commercial gazetteer first
  const localMatches = MAJOR_GEO_HUBS.filter(
    (h) =>
      h.name.toLowerCase().includes(q) ||
      h.state.toLowerCase().includes(q) ||
      h.country.toLowerCase().includes(q) ||
      h.tag.toLowerCase().includes(q),
  );

  // If we already have 4+ accurate local trade hub matches or query is short, return them immediately
  if (localMatches.length >= 4 || q.length < 3) {
    return localMatches.slice(0, 6);
  }

  const mapboxToken = import.meta.env.VITE_MAPBOX_TOKEN;
  const geoapifyKey = import.meta.env.VITE_GEOAPIFY_API_KEY;
  const externalMatches: GeoHub[] = [];

  if (mapboxToken) {
    try {
      const resp = await fetch(
        `https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(query)}.json?access_token=${mapboxToken}&types=place,locality,region,country&limit=5`,
      );
      if (resp.ok) {
        const data = await resp.json();
        for (const feat of data.features || []) {
          const [lng, lat] = feat.center || [];
          if (lat != null && lng != null) {
            let city = feat.text || "";
            let country = "";
            let state = "";
            for (const ctx of feat.context || []) {
              if (ctx.id.startsWith("country")) country = ctx.text;
              if (ctx.id.startsWith("region")) state = ctx.text;
            }
            externalMatches.push({
              name: city,
              state: state,
              country: country,
              lat,
              lng,
              type: "metro",
              tag: feat.place_name || `${city}, ${country}`,
            });
          }
        }
      }
    } catch {
      // ignore
    }
  } else if (geoapifyKey) {
    try {
      const resp = await fetch(
        `https://api.geoapify.com/v1/geocode/autocomplete?text=${encodeURIComponent(query)}&apiKey=${geoapifyKey}&limit=5`,
      );
      if (resp.ok) {
        const data = await resp.json();
        for (const feat of data.features || []) {
          const prop = feat.properties || {};
          if (prop.lat != null && prop.lon != null) {
            externalMatches.push({
              name: prop.city || prop.name || query,
              state: prop.state || "",
              country: prop.country || "",
              lat: prop.lat,
              lng: prop.lon,
              type: "metro",
              tag: prop.formatted || `${prop.city}, ${prop.country}`,
            });
          }
        }
      }
    } catch {
      // ignore
    }
  } else {
    // Free Nominatim forward geocoding search fallback
    try {
      const resp = await fetch(
        `https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(query)}&limit=4&addressdetails=1`,
        { headers: { "Accept-Language": "en" } },
      );
      if (resp.ok) {
        const data = await resp.json();
        for (const item of data || []) {
          const lat = parseFloat(item.lat);
          const lon = parseFloat(item.lon);
          if (!isNaN(lat) && !isNaN(lon)) {
            const addr = item.address || {};
            const city = addr.city || addr.town || addr.county || item.display_name.split(",")[0];
            externalMatches.push({
              name: city,
              state: addr.state || "",
              country: addr.country || "",
              lat,
              lng: lon,
              type: "metro",
              tag: item.display_name,
            });
          }
        }
      }
    } catch {
      // ignore
    }
  }

  // Combine local matches and external matches without duplicates
  const combined = [...localMatches];
  for (const ext of externalMatches) {
    const exists = combined.some(
      (c) =>
        (c.name.toLowerCase() === ext.name.toLowerCase() && c.country.toLowerCase() === ext.country.toLowerCase()) ||
        calculateDistanceKm(c.lat, c.lng, ext.lat, ext.lng) < 25,
    );
    if (!exists) {
      combined.push(ext);
    }
  }

  return combined.slice(0, 6);
}


/**
 * Creates custom SVG HTML pin icon for Leaflet.
 * Eliminates Leaflet's missing PNG assets and allows glorious glowing effects.
 */
export function createCustomLocationPinIcon(title = "Selected Location", isDraggable = true): L.DivIcon {
  const html = `
    <div class="custom-map-pin location-picker-pin" title="${title}">
      <div class="pin-pulse"></div>
      <div class="pin-marker-head">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
          <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z"/>
        </svg>
      </div>
      ${isDraggable ? '<div class="pin-drag-hint">Drag</div>' : ""}
    </div>
  `;
  return L.divIcon({
    html,
    className: "leaflet-custom-div-icon",
    iconSize: [36, 42],
    iconAnchor: [18, 40],
    popupAnchor: [0, -36],
  });
}

/**
 * Creates rich marketplace pins showing Buyer (blue) vs Seller (emerald) with price badge
 */
export function createMarketplacePinIcon(item: CatalogItem, isSelected = false): L.DivIcon {
  const isSeller = item.role === "seller";
  const roleClass = isSeller ? "pin-role-seller" : "pin-role-buyer";
  const selectedClass = isSelected ? "pin-is-selected" : "";

  // Concise price format
  let priceStr = "";
  if (item.price_target) {
    const sym =
      item.price_target.currency === "INR"
        ? "₹"
        : item.price_target.currency === "USD"
          ? "$"
          : item.price_target.currency === "EUR"
            ? "€"
            : "";
    priceStr = `${sym}${item.price_target.amount >= 1000 ? `${(item.price_target.amount / 1000).toFixed(0)}k` : item.price_target.amount}`;
  }

  const html = `
    <div class="custom-map-pin marketplace-pin ${roleClass} ${selectedClass}" data-rfq-id="${item.id}">
      <div class="pin-pulse"></div>
      <div class="pin-content-box">
        <span class="pin-role-pill">${isSeller ? "SELL" : "BUY"}</span>
        ${priceStr ? `<span class="pin-price-pill">${priceStr}</span>` : ""}
      </div>
      <div class="pin-tip"></div>
    </div>
  `;

  return L.divIcon({
    html,
    className: "leaflet-custom-div-icon",
    iconSize: [64, 40],
    iconAnchor: [32, 38],
    popupAnchor: [0, -36],
  });
}

/**
 * Creates custom pin for logistics waypoints
 */
export function createShipmentWaypointIcon(
  type: "origin" | "checkpoint" | "destination",
  isCurrent = false,
  label = "",
): L.DivIcon {
  const typeClass = `waypoint-${type} ${isCurrent ? "waypoint-active-live" : ""}`;
  const icon =
    type === "origin"
      ? "🏭"
      : type === "destination"
        ? "🏢"
        : "📍";

  const html = `
    <div class="custom-map-pin shipment-waypoint-pin ${typeClass}">
      ${isCurrent ? '<div class="pin-pulse live-transit-pulse"></div>' : ""}
      <div class="waypoint-badge">
        <span class="waypoint-emoji">${icon}</span>
        ${label ? `<span class="waypoint-label">${label}</span>` : ""}
      </div>
    </div>
  `;

  return L.divIcon({
    html,
    className: "leaflet-custom-div-icon",
    iconSize: [38, 38],
    iconAnchor: [19, 36],
    popupAnchor: [0, -34],
  });
}

/**
 * Animated moving vehicle icon (truck, cargo ship, plane)
 */
export function createMovingVehicleIcon(mode: string): L.DivIcon {
  const icon =
    mode.toLowerCase().includes("air") || mode.toLowerCase().includes("plane")
      ? "✈️"
      : mode.toLowerCase().includes("sea") || mode.toLowerCase().includes("maritime") || mode.toLowerCase().includes("ship")
        ? "🚢"
        : "🚚";

  const html = `
    <div class="custom-map-pin moving-vehicle-pin">
      <div class="vehicle-halo"></div>
      <div class="vehicle-symbol">${icon}</div>
    </div>
  `;

  return L.divIcon({
    html,
    className: "leaflet-custom-div-icon",
    iconSize: [42, 42],
    iconAnchor: [21, 21],
  });
}

export type FreightMode = "road" | "ocean" | "air";

export interface RouteGeometryResult {
  coordinates: [number, number][]; // [lat, lng] array ready for Leaflet L.polyline
  distanceKm: number;
  durationMinutes: number;
  mode: FreightMode;
  isRoadRoute: boolean;
  isFeasible: boolean;
  unfeasibleReason?: string;
  recommendation?: string;
  viaSummary?: string;
  nauticalMiles?: number;
  transitDaysMin?: number;
  transitDaysMax?: number;
  emissionKgCO2?: number;
  originPortName?: string;
  destPortName?: string;
  drayageOriginKm?: number;
  drayageDestKm?: number;
}


/**
 * Catmull-Rom spline interpolation to smoothly curve maritime and air route lines
 */
function smoothSplinePath(waypoints: [number, number][], pointsPerSegment = 8): [number, number][] {
  if (waypoints.length <= 2) return waypoints;
  const result: [number, number][] = [];
  for (let i = 0; i < waypoints.length - 1; i++) {
    const p0 = waypoints[Math.max(0, i - 1)];
    const p1 = waypoints[i];
    const p2 = waypoints[i + 1];
    const p3 = waypoints[Math.min(waypoints.length - 1, i + 2)];
    for (let step = 0; step < pointsPerSegment; step++) {
      const t = step / pointsPerSegment;
      const t2 = t * t;
      const t3 = t2 * t;
      const lat =
        0.5 *
        (2 * p1[0] +
          (-p0[0] + p2[0]) * t +
          (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
          (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3);
      const lng =
        0.5 *
        (2 * p1[1] +
          (-p0[1] + p2[1]) * t +
          (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
          (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3);
      result.push([lat, lng]);
    }
  }
  result.push(waypoints[waypoints.length - 1]);
  return result;
}

/**
 * Find closest commercial seaport for intermodal maritime routing
 */
export function findNearestSeaport(lat: number, lng: number): GeoHub {
  const seaports = MAJOR_GEO_HUBS.filter((h) => h.type === "seaport");
  const extraPorts: GeoHub[] = [
    {
      name: "Hazira / Surat Port",
      state: "Gujarat",
      country: "India",
      lat: 21.1,
      lng: 72.65,
      type: "seaport",
      tag: "Industrial Deepwater Seaport",
    },
    {
      name: "Colombo Port",
      state: "Western",
      country: "Sri Lanka",
      lat: 6.95,
      lng: 79.85,
      type: "seaport",
      tag: "Transshipment Hub",
    },
    {
      name: "Santos / São Paulo Port",
      state: "São Paulo",
      country: "Brazil",
      lat: -23.9608,
      lng: -46.3336,
      type: "seaport",
      tag: "Latin America Container Hub",
    },
    {
      name: "Buenos Aires Port",
      state: "Buenos Aires",
      country: "Argentina",
      lat: -34.5833,
      lng: -58.3667,
      type: "seaport",
      tag: "Atlantic Gateway",
    },
    {
      name: "Sydney Port Botany",
      state: "New South Wales",
      country: "Australia",
      lat: -33.9744,
      lng: 151.2186,
      type: "seaport",
      tag: "Oceania Gateway",
    },
  ];
  const allPorts = [...seaports, ...extraPorts];

  let nearest = allPorts[0];
  let minD = Infinity;
  for (const p of allPorts) {
    const d = calculateDistanceKm(lat, lng, p.lat, p.lng);
    if (d < minD) {
      minD = d;
      nearest = p;
    }
  }
  return nearest;
}

export interface FeasibilityCheckResult {
  isFeasible: boolean;
  reason?: string;
  recommendation?: string;
}

/**
 * Determine whether ocean cargo shipping is geographically and commercially feasible
 * between two locations.
 * Flags unfeasible corridors (e.g. inland-to-inland contiguous overland routes like Surat to Indore,
 * Delhi to Jaipur, or routes where nearest ports require more land drayage than direct highway trucking).
 */
export function checkOceanFeasibility(
  startLat: number,
  startLng: number,
  endLat: number,
  endLng: number,
  startName?: string,
  endName?: string,
): FeasibilityCheckResult {
  const directDistanceKm = calculateDistanceKm(startLat, startLng, endLat, endLng);
  const originPort = findNearestSeaport(startLat, startLng);
  const destPort = findNearestSeaport(endLat, endLng);

  const drayageOriginKm = calculateDistanceKm(startLat, startLng, originPort.lat, originPort.lng);
  const drayageDestKm = calculateDistanceKm(destPort.lat, destPort.lng, endLat, endLng);
  const portToPortDist = calculateDistanceKm(originPort.lat, originPort.lng, destPort.lat, destPort.lng);

  const cleanStartName = (startName || "").split(",")[0].trim() || originPort.name.split("/")[0].trim();
  const cleanEndName = (endName || "").split(",")[0].trim() || destPort.name.split("/")[0].trim();

  // 1. Cross-border / international over water routes are feasible
  const isDifferentCountry = originPort.country !== destPort.country;
  if (isDifferentCountry && portToPortDist > 200) {
    return { isFeasible: true };
  }

  // 2. Both points map to the EXACT same seaport or within 120km of the same port cluster
  if (originPort.name === destPort.name || portToPortDist < 120) {
    return {
      isFeasible: false,
      reason: `There is no ocean or navigable sea route between ${cleanStartName} and ${cleanEndName}. Both locations rely on the same coastal gateway (${originPort.name}). Any maritime movement would require redundant trucking to the port and a loop back to the same coastline.`,
      recommendation: `Use Direct Road Freight (approx. ${Math.round(directDistanceKm)} km direct highway corridor) for door-to-door transit without maritime diversion.`,
    };
  }

  // 3. Inland cities on the same landmass (e.g. India) where land drayage dominates the direct route
  // If combined trucking to/from seaports is greater than 60% of direct distance, ocean makes no sense
  const totalDrayage = drayageOriginKm + drayageDestKm;
  if (totalDrayage >= directDistanceKm * 0.6 && directDistanceKm < 1500) {
    return {
      isFeasible: false,
      reason: `No ocean or navigable waterway separates ${cleanStartName} and ${cleanEndName}. The overland drayage to commercial seaports (${originPort.name} and ${destPort.name}, total ~${Math.round(totalDrayage)} km) is comparable to or exceeds the direct overland route (~${Math.round(directDistanceKm)} km).`,
      recommendation: `Use Direct Road Freight (${Math.round(directDistanceKm)} km direct highway transit) or Air Cargo for rapid express delivery.`,
    };
  }

  // 4. Same contiguous coast within short distance (< 650 km direct) where direct road is far superior
  if (
    directDistanceKm < 650 &&
    originPort.country === "India" &&
    destPort.country === "India" &&
    ((originPort.lng < 75 && destPort.lng < 75) || (originPort.lng > 78 && destPort.lng > 78))
  ) {
    return {
      isFeasible: false,
      reason: `Overland highway route between ${cleanStartName} and ${cleanEndName} (~${Math.round(directDistanceKm)} km) is direct and contiguous. Short-distance domestic coastal shipping introduces 2–4 days of port container dwell time with zero geographic necessity.`,
      recommendation: `Use Direct Road Freight (~${Math.round(directDistanceKm)} km direct highway route) for 1-day delivery.`,
    };
  }

  // Domestic peninsular coastal shipping (e.g. West Coast Mumbai/Kandla to East Coast Chennai/Kolkata > 1,500km)
  // or international maritime corridors are feasible
  return { isFeasible: true };
}

/**
 * Classify a coordinate or location string into its macro-geographic landmass
 */
export function detectGlobalLandmass(lat: number, lng: number, name?: string): string {
  const q = (name || "").toLowerCase();

  if (
    q.includes("brazil") ||
    q.includes("argentina") ||
    q.includes("chile") ||
    q.includes("colombia") ||
    q.includes("peru") ||
    q.includes("venezuela") ||
    q.includes("ecuador") ||
    q.includes("bolivia") ||
    q.includes("paraguay") ||
    q.includes("uruguay") ||
    q.includes("santos") ||
    q.includes("sao paulo") ||
    q.includes("buenos aires") ||
    q.includes("bogota") ||
    q.includes("lima") ||
    q.includes("santiago") ||
    q.includes("south america") ||
    q.includes("latin america")
  ) {
    return "south_america";
  }

  if (
    q.includes("united states") ||
    q.includes("usa") ||
    q.includes("canada") ||
    q.includes("mexico") ||
    q.includes("california") ||
    q.includes("los angeles") ||
    q.includes("new york") ||
    q.includes("chicago") ||
    q.includes("newark") ||
    q.includes("north america")
  ) {
    return "north_america";
  }

  if (
    q.includes("australia") ||
    q.includes("new zealand") ||
    q.includes("sydney") ||
    q.includes("melbourne") ||
    q.includes("perth") ||
    q.includes("brisbane") ||
    q.includes("auckland") ||
    q.includes("oceania")
  ) {
    return "oceania";
  }

  if (q.includes("sri lanka") || q.includes("colombo")) {
    return "sri_lanka";
  }

  if (q.includes("japan") || q.includes("tokyo") || q.includes("osaka")) {
    return "japan";
  }

  if (q.includes("united kingdom") || q.includes("london") || q.includes("britain") || q.includes("ireland")) {
    return "uk";
  }

  if (
    q.includes("india") ||
    q.includes("gujarat") ||
    q.includes("maharashtra") ||
    q.includes("delhi") ||
    q.includes("madhya pradesh") ||
    q.includes("tamil nadu") ||
    q.includes("karnataka") ||
    q.includes("bengaluru") ||
    q.includes("mumbai") ||
    q.includes("surat") ||
    q.includes("indore") ||
    q.includes("chennai") ||
    q.includes("kolkata") ||
    q.includes("hyderabad") ||
    q.includes("ahmedabad")
  ) {
    return "india";
  }

  // Geographic coordinate bounding box fallback:
  if (lat <= 14 && lat >= -56 && lng >= -82 && lng <= -34) {
    return "south_america";
  }
  if (lat > 14 && lat <= 72 && lng >= -168 && lng <= -50) {
    return "north_america";
  }
  if (lat <= -10 && lat >= -48 && lng >= 112 && lng <= 179) {
    return "oceania";
  }
  if (lat >= 5.8 && lat <= 9.9 && lng >= 79.5 && lng <= 82.0) {
    return "sri_lanka";
  }
  if (lat >= 30 && lat <= 46 && lng >= 128 && lng <= 146) {
    return "japan";
  }
  if (lat >= 6 && lat <= 36 && lng >= 68 && lng <= 98) {
    return "india";
  }
  if (lat >= 35 && lat <= 70 && lng >= -10 && lng <= 40) {
    return "europe";
  }
  if (lat <= 37 && lat >= -35 && lng >= -18 && lng <= 52 && (lat < 12 || lng < 34)) {
    return "africa";
  }
  if (lat >= 18 && lat <= 54 && lng >= 98 && lng <= 135) {
    return "east_asia";
  }

  return "other";
}

/**
 * Determine whether direct road freight (trucking) is physically and geographically feasible
 * between two locations.
 * Flags trans-oceanic and cross-continental routes (e.g. South America to India, North America to Europe/Asia,
 * Australia to India, Sri Lanka to India) where no contiguous road/bridge network exists.
 */
export function checkRoadFeasibility(
  startLat: number,
  startLng: number,
  endLat: number,
  endLng: number,
  startName?: string,
  endName?: string,
): FeasibilityCheckResult {
  const directDistanceKm = calculateDistanceKm(startLat, startLng, endLat, endLng);
  const landmassA = detectGlobalLandmass(startLat, startLng, startName);
  const landmassB = detectGlobalLandmass(endLat, endLng, endName);

  const cleanStartName = (startName || "").split(",")[0].trim() || "Origin";
  const cleanEndName = (endName || "").split(",")[0].trim() || "Destination";

  // 1. South America to anywhere outside South America (e.g. South America to India, Eurasia, Africa, Oceania)
  if (
    (landmassA === "south_america" && landmassB !== "south_america") ||
    (landmassB === "south_america" && landmassA !== "south_america")
  ) {
    return {
      isFeasible: false,
      reason: `There is no overland road or highway network connecting ${cleanStartName} and ${cleanEndName} across the Atlantic and Pacific oceans. Intercontinental ground trucking is physically impossible across oceanic boundaries.`,
      recommendation: `Use Ocean Cargo (🚢 Container Vessel via Cape of Good Hope or Atlantic Sea Lane) or Express Air Cargo (✈️ Freighter Flight) for trans-oceanic transport.`,
    };
  }

  // 2. North America to India, Eurasia, Africa, or Oceania
  if (
    (landmassA === "north_america" && (landmassB === "india" || landmassB === "africa" || landmassB === "oceania" || landmassB === "europe" || landmassB === "east_asia")) ||
    (landmassB === "north_america" && (landmassA === "india" || landmassA === "africa" || landmassA === "oceania" || landmassA === "europe" || landmassA === "east_asia"))
  ) {
    return {
      isFeasible: false,
      reason: `No contiguous road or land bridge exists between ${cleanStartName} and ${cleanEndName} across the Atlantic or Pacific oceans. Intercontinental trucking cannot cross oceanic boundaries.`,
      recommendation: `Use Ocean Cargo (🚢 Trans-Oceanic Container Vessel) or Express Air Cargo (✈️ Trans-Pacific / Trans-Atlantic Air Corridor).`,
    };
  }

  // 3. Australia / Oceania to India / Eurasia / Americas
  if (
    (landmassA === "oceania" && landmassB !== "oceania") ||
    (landmassB === "oceania" && landmassA !== "oceania")
  ) {
    return {
      isFeasible: false,
      reason: `No overland highway connection exists to ${cleanStartName} or ${cleanEndName} across the Indian and Pacific oceans. Australia/Oceania is separated by deep ocean waters.`,
      recommendation: `Use Ocean Cargo (🚢 Container Liner) or Express Air Cargo (✈️ Intercontinental Air Flight).`,
    };
  }

  // 4. Island nations (Sri Lanka, Japan, etc.) across open water
  if (
    (landmassA === "sri_lanka" && landmassB === "india") ||
    (landmassB === "sri_lanka" && landmassA === "india")
  ) {
    return {
      isFeasible: false,
      reason: `No road bridge exists across the Palk Strait between India and Sri Lanka. Ground trucking cannot cross the sea channel directly without marine roll-on/roll-off vessel or air freight.`,
      recommendation: `Use Coastal Feeder Ocean Cargo (🚢 Colombo to Indian Port) or Express Air Cargo (✈️ 1-hour flight).`,
    };
  }

  if (
    (landmassA === "japan" && landmassB !== "japan") ||
    (landmassB === "japan" && landmassA !== "japan")
  ) {
    return {
      isFeasible: false,
      reason: `Japan is an island nation separated by the Sea of Japan and East China Sea. Direct road trucking to mainland corridors is not physically connected.`,
      recommendation: `Use Ocean Cargo (🚢 Maritime Container Ship) or Express Air Cargo.`,
    };
  }

  // 5. Africa to India / Americas (across Indian Ocean / Atlantic Ocean)
  if (
    (landmassA === "africa" && landmassB === "india" && directDistanceKm > 3000) ||
    (landmassB === "africa" && landmassA === "india" && directDistanceKm > 3000)
  ) {
    return {
      isFeasible: false,
      reason: `There is no direct overland highway between ${cleanStartName} and ${cleanEndName}. They are separated by the Arabian Sea and Indian Ocean (~${Math.round(directDistanceKm)} km).`,
      recommendation: `Use Ocean Cargo (🚢 Arabian Sea / Indian Ocean Lane) or Express Air Cargo.`,
    };
  }

  // 6. Generic extreme trans-oceanic distance (> 5,500 km between different non-contiguous regions)
  if (directDistanceKm > 5500 && landmassA !== landmassB) {
    return {
      isFeasible: false,
      reason: `The direct distance of ~${Math.round(directDistanceKm).toLocaleString()} km spans multiple oceans and continents without a continuous commercial road network.`,
      recommendation: `Use Ocean Cargo (🚢 Container Vessel) or Express Air Cargo (✈️ Air Freight).`,
    };
  }

  return { isFeasible: true };
}

/**
 * Generate Great-Circle flight trajectory (Air Cargo Flight Route)
 * Uses true spherical trigonometry interpolation (Slerp) matching airline flight radar
 */
export function buildAirRouteGeometry(
  startLat: number,
  startLng: number,
  endLat: number,
  endLng: number,
  steps = 45,
): RouteGeometryResult {
  const directDistanceKm = calculateDistanceKm(startLat, startLng, endLat, endLng);
  const coords: [number, number][] = [];

  const lat1 = (startLat * Math.PI) / 180;
  const lon1 = (startLng * Math.PI) / 180;
  const lat2 = (endLat * Math.PI) / 180;
  const lon2 = (endLng * Math.PI) / 180;

  const d = 2 * Math.asin(
    Math.sqrt(
      Math.sin((lat2 - lat1) / 2) ** 2 +
        Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2,
    ),
  );

  if (d < 0.0001) {
    coords.push([startLat, startLng], [endLat, endLng]);
  } else {
    for (let i = 0; i <= steps; i++) {
      const f = i / steps;
      const A = Math.sin((1 - f) * d) / Math.sin(d);
      const B = Math.sin(f * d) / Math.sin(d);
      const x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
      const y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
      const z = A * Math.sin(lat1) + B * Math.sin(lat2);
      const lat = Math.atan2(z, Math.sqrt(x ** 2 + y ** 2));
      const lon = Math.atan2(y, x);
      coords.push([(lat * 180) / Math.PI, (lon * 180) / Math.PI]);
    }
  }

  // Flight duration: ~820 km/h cruising + 50 min runway/climb/approach
  const cruiseHours = directDistanceKm / 820;
  const totalMinutes = Math.round(cruiseHours * 60 + 50);

  return {
    coordinates: coords,
    distanceKm: directDistanceKm,
    durationMinutes: totalMinutes,
    mode: "air",
    isRoadRoute: false,
    isFeasible: true,
    viaSummary: directDistanceKm > 2000 ? "Great-Circle Air Corridor" : "Domestic Cargo Flight",
    transitDaysMin: 1,
    transitDaysMax: 2,
    emissionKgCO2: Math.round(directDistanceKm * 0.52),
  };
}

/**
 * Generate Ocean Cargo shipping lane route navigating maritime corridors & straits
 */
export function buildMaritimeRouteGeometry(
  startLat: number,
  startLng: number,
  endLat: number,
  endLng: number,
  startName?: string,
  endName?: string,
): RouteGeometryResult {
  const feasibility = checkOceanFeasibility(startLat, startLng, endLat, endLng, startName, endName);

  const originPort = findNearestSeaport(startLat, startLng);
  const destPort = findNearestSeaport(endLat, endLng);

  const drayageOriginKm = calculateDistanceKm(startLat, startLng, originPort.lat, originPort.lng);
  const drayageDestKm = calculateDistanceKm(destPort.lat, destPort.lng, endLat, endLng);
  const directDistanceKm = Math.round(calculateDistanceKm(startLat, startLng, endLat, endLng));

  // If ocean routing is unfeasible (e.g. Surat <-> Indore contiguous overland corridor),
  // return direct unfeasible result with clear commercial & geographic advisory
  if (!feasibility.isFeasible) {
    return {
      coordinates: [[startLat, startLng], [endLat, endLng]],
      distanceKm: directDistanceKm,
      durationMinutes: 0,
      mode: "ocean",
      isRoadRoute: false,
      isFeasible: false,
      unfeasibleReason: feasibility.reason,
      recommendation: feasibility.recommendation,
      viaSummary: "Ocean Route Not Feasible (Inland / Contiguous Corridor)",
      emissionKgCO2: 0,
      originPortName: originPort.name,
      destPortName: destPort.name,
      drayageOriginKm: Math.round(drayageOriginKm),
      drayageDestKm: Math.round(drayageDestKm),
    };
  }

  // Determine sea corridor waypoints between the two ports
  const seaWaypoints: [number, number][] = [[originPort.lat, originPort.lng]];
  let corridorSummary = "Coastal Feeder Shipping Lane";

  // Check spatial relationship between ports
  const pA = originPort;
  const pB = destPort;
  const portDist = calculateDistanceKm(pA.lat, pA.lng, pB.lat, pB.lng);

  if (pA.name === pB.name || portDist < 120) {
    // Both near same port/gulf (e.g. West Coast India coastal feeder between Mumbai & Hazira/Surat)
    seaWaypoints.push(
      [19.2, 72.4],
      [20.3, 72.1],
      [21.05, 72.6],
      [destPort.lat, destPort.lng],
    );
    corridorSummary = "Gulf of Khambhat / Coastal Sea Lane";
  } else if (pA.country === "India" && pB.country === "India") {
    // Domestic coastal shipping (e.g. Mumbai/Mundra to Chennai/Kolkata)
    if (pA.lng < 75 && pB.lng > 78) {
      // West Coast -> East Coast
      seaWaypoints.push(
        [15.0, 72.5],
        [8.5, 76.5],
        [5.8, 80.5], // Sri Lanka South Cape
        [9.5, 81.5],
        [destPort.lat, destPort.lng],
      );
      corridorSummary = "Indian Ocean Coastal Feeder";
    } else if (pA.lng > 78 && pB.lng < 75) {
      // East Coast -> West Coast
      seaWaypoints.push(
        [9.5, 81.5],
        [5.8, 80.5],
        [8.5, 76.5],
        [15.0, 72.5],
        [destPort.lat, destPort.lng],
      );
      corridorSummary = "Indian Ocean Coastal Feeder";
    } else {
      // Same coast feeder
      seaWaypoints.push(
        [(pA.lat + pB.lat) / 2, Math.min(pA.lng, pB.lng) - 1.2],
        [destPort.lat, destPort.lng],
      );
      corridorSummary = "West Coast Marine Corridor";
    }
  } else if (
    (pA.country === "India" && pB.name.includes("Dubai")) ||
    (pB.country === "India" && pA.name.includes("Dubai"))
  ) {
    // India <-> Middle East (Dubai / Jebel Ali)
    if (pA.country === "India") {
      seaWaypoints.push(
        [19.5, 68.5],
        [22.5, 63.5],
        [24.5, 59.0], // Gulf of Oman
        [26.56, 56.4], // Strait of Hormuz
        [destPort.lat, destPort.lng],
      );
    } else {
      seaWaypoints.push(
        [26.56, 56.4],
        [24.5, 59.0],
        [22.5, 63.5],
        [19.5, 68.5],
        [destPort.lat, destPort.lng],
      );
    }
    corridorSummary = "Arabian Sea & Strait of Hormuz Corridor";
  } else if (
    (pA.country === "India" && (pB.country === "Netherlands" || pB.country === "Germany")) ||
    ((pA.country === "Netherlands" || pA.country === "Germany") && pB.country === "India")
  ) {
    // India <-> Europe (Rotterdam/Hamburg via Suez Canal)
    if (pA.country === "India") {
      seaWaypoints.push(
        [15.0, 64.0],
        [12.5, 51.0], // Gulf of Aden
        [12.58, 43.33], // Bab-el-Mandeb
        [20.0, 38.5], // Red Sea
        [27.8, 34.0],
        [30.58, 32.26], // Suez Canal
        [33.5, 27.0], // Mediterranean East
        [37.0, 3.0], // Mediterranean West
        [35.98, -5.35], // Strait of Gibraltar
        [39.0, -9.8], // Portugal Coast
        [45.0, -7.5], // Bay of Biscay
        [50.2, -0.5], // English Channel
        [destPort.lat, destPort.lng],
      );
    } else {
      seaWaypoints.push(
        [50.2, -0.5],
        [45.0, -7.5],
        [39.0, -9.8],
        [35.98, -5.35],
        [37.0, 3.0],
        [33.5, 27.0],
        [30.58, 32.26],
        [27.8, 34.0],
        [20.0, 38.5],
        [12.58, 43.33],
        [12.5, 51.0],
        [15.0, 64.0],
        [destPort.lat, destPort.lng],
      );
    }
    corridorSummary = "Suez Canal & Mediterranean Mega-Corridor";
  } else if (
    (pA.country === "India" && (pB.country === "China" || pB.name.includes("Singapore"))) ||
    ((pA.country === "China" || pA.name.includes("Singapore")) && pB.country === "India")
  ) {
    // India <-> Southeast Asia / China (via Malacca Strait)
    if (pA.country === "India") {
      seaWaypoints.push(
        [13.0, 73.5],
        [7.5, 76.5],
        [5.7, 80.5], // South Sri Lanka
        [5.8, 88.0],
        [5.5, 96.0], // Malacca Northwest
        [2.5, 101.5], // Malacca Strait
        [1.25, 103.8], // Singapore Strait
      );
      if (pB.country === "China") {
        seaWaypoints.push(
          [7.0, 108.5],
          [14.0, 113.0],
          [21.5, 114.5], // Shenzhen / Hong Kong
        );
        if (pB.name.includes("Shanghai") || pB.name.includes("Ningbo")) {
          seaWaypoints.push([25.0, 120.5], [31.2, 122.0]);
        }
      }
      seaWaypoints.push([destPort.lat, destPort.lng]);
    } else {
      seaWaypoints.push(
        [1.25, 103.8],
        [2.5, 101.5],
        [5.5, 96.0],
        [5.8, 88.0],
        [5.7, 80.5],
        [7.5, 76.5],
        [13.0, 73.5],
        [destPort.lat, destPort.lng],
      );
    }
    corridorSummary = "Malacca Strait & South China Sea Lane";
  } else if (
    ((pA.country === "Brazil" || pA.country === "Argentina") && pB.country === "India") ||
    (pA.country === "India" && (pB.country === "Brazil" || pB.country === "Argentina"))
  ) {
    // South America (Santos / Buenos Aires) <-> India (via Cape of Good Hope & Indian Ocean)
    if (pA.country === "India") {
      seaWaypoints.push(
        [15.0, 70.0],
        [5.0, 65.0],
        [-12.0, 50.0], // East of Madagascar
        [-34.5, 26.0], // South Africa East Coast
        [-35.5, 18.5], // Cape of Good Hope
        [-30.0, -10.0], // South Atlantic
        [-25.0, -35.0],
        [destPort.lat, destPort.lng],
      );
    } else {
      seaWaypoints.push(
        [-25.0, -35.0],
        [-30.0, -10.0],
        [-35.5, 18.5], // Cape of Good Hope
        [-34.5, 26.0],
        [-12.0, 50.0],
        [5.0, 65.0],
        [15.0, 70.0],
        [destPort.lat, destPort.lng],
      );
    }
    corridorSummary = "South Atlantic & Cape of Good Hope Mega-Corridor";
  } else {
    // Inter-regional maritime arc
    const arc = generateGeodesicArc(pA.lat, pA.lng, pB.lat, pB.lng, 8);
    seaWaypoints.push(...arc.slice(1, -1), [destPort.lat, destPort.lng]);
    corridorSummary = "Trans-Oceanic Freight Corridor";
  }

  // Smooth sea route through waypoints
  const smoothedSea = smoothSplinePath(seaWaypoints, 6);

  // Combine full intermodal itinerary:
  // Origin Facility -> Drayage to Origin Port -> Ocean Voyage -> Drayage to Dest Facility
  const allCoordinates: [number, number][] = [];
  if (drayageOriginKm > 20) {
    allCoordinates.push([startLat, startLng]);
  }
  allCoordinates.push(...smoothedSea);
  if (drayageDestKm > 20) {
    allCoordinates.push([endLat, endLng]);
  }

  // Calculate cumulative distance
  let totalDistanceKm = 0;
  for (let i = 0; i < allCoordinates.length - 1; i++) {
    totalDistanceKm += calculateDistanceKm(
      allCoordinates[i][0],
      allCoordinates[i][1],
      allCoordinates[i + 1][0],
      allCoordinates[i + 1][1],
    );
  }
  totalDistanceKm = Math.round(totalDistanceKm);

  const nauticalMiles = Math.round(totalDistanceKm / 1.852);
  // Container ship cruise speed ~19 knots (~35 km/h) + 2 days port handling
  const voyageDays = Math.max(2, Math.ceil(totalDistanceKm / 750) + 2);
  const durationMinutes = voyageDays * 24 * 60;

  return {
    coordinates: allCoordinates,
    distanceKm: totalDistanceKm,
    durationMinutes,
    mode: "ocean",
    isRoadRoute: false,
    isFeasible: true,
    viaSummary: corridorSummary,
    nauticalMiles,
    transitDaysMin: voyageDays - 1,
    transitDaysMax: voyageDays + 3,
    emissionKgCO2: Math.round(totalDistanceKm * 0.016), // Maritime is ~16g CO2/tonne-km (greenest mode)
    originPortName: originPort.name,
    destPortName: destPort.name,
    drayageOriginKm: Math.round(drayageOriginKm),
    drayageDestKm: Math.round(drayageDestKm),
  };
}

/**
 * Generate smooth curved arc between two coordinates for aesthetic flight/shipping routes
 */
export function generateGeodesicArc(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number,
  steps = 24,
): [number, number][] {
  const points: [number, number][] = [];
  const dLat = lat2 - lat1;
  const dLon = lon2 - lon1;
  const dist = calculateDistanceKm(lat1, lon1, lat2, lon2);

  // Offset magnitude proportional to distance, max 1.0 deg
  const offset = Math.min(dist / 3500, 0.9);
  const midLat = (lat1 + lat2) / 2;
  const midLon = (lon1 + lon2) / 2;
  const perpLat = -dLon * 0.12 * offset;
  const perpLon = dLat * 0.12 * offset;

  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const lat = (1 - t) * (1 - t) * lat1 + 2 * (1 - t) * t * (midLat + perpLat) + t * t * lat2;
    const lon = (1 - t) * (1 - t) * lon1 + 2 * (1 - t) * t * (midLon + perpLon) + t * t * lon2;
    points.push([lat, lon]);
  }
  return points;
}

/**
 * Fetch real driving road route directions (Google Maps style) using OSRM / Mapbox.
 * Follows actual highways, streets, and road geometry instead of straight lines.
 * Falls back gracefully to smooth geodesic arc if across water or offline.
 */
export async function fetchRouteGeometry(
  points: [number, number][], // Array of [lat, lng] points (at least 2)
  startName?: string,
  endName?: string,
): Promise<RouteGeometryResult> {
  if (points.length < 2) {
    return {
      coordinates: points,
      distanceKm: 0,
      durationMinutes: 0,
      mode: "road",
      isRoadRoute: false,
      isFeasible: true,
    };
  }

  const start = points[0];
  const end = points[points.length - 1];
  const directDistanceKm = calculateDistanceKm(start[0], start[1], end[0], end[1]);

  // Check road feasibility first (e.g. South America to India trans-oceanic routes)
  const roadFeasibility = checkRoadFeasibility(start[0], start[1], end[0], end[1], startName, endName);
  if (!roadFeasibility.isFeasible) {
    return {
      coordinates: [start, end],
      distanceKm: directDistanceKm,
      durationMinutes: 0,
      mode: "road",
      isRoadRoute: false,
      isFeasible: false,
      unfeasibleReason: roadFeasibility.reason,
      recommendation: roadFeasibility.recommendation,
      viaSummary: "Road Transit Not Feasible (Trans-Oceanic Corridor)",
      transitDaysMin: 0,
      transitDaysMax: 0,
      emissionKgCO2: 0,
    };
  }

  // Coordinate string for Mapbox/OSRM: lon,lat;lon,lat;...
  const coordString = points.map(([lat, lng]) => `${lng},${lat}`).join(";");

  // 1. Try Mapbox Directions API if user configured VITE_MAPBOX_TOKEN
  const mapboxToken = import.meta.env.VITE_MAPBOX_TOKEN;
  if (mapboxToken) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 4000);
      const resp = await fetch(
        `https://api.mapbox.com/directions/v5/mapbox/driving/${coordString}?geometries=geojson&overview=full&access_token=${mapboxToken}`,
        { signal: controller.signal },
      );
      clearTimeout(timeout);
      if (resp.ok) {
        const data = await resp.json();
        if (data.routes && data.routes.length > 0) {
          const route = data.routes[0];
          const coords: [number, number][] = route.geometry.coordinates.map(
            ([lon, lat]: [number, number]) => [lat, lon],
          );
          return {
            coordinates: coords,
            distanceKm: Math.round((route.distance / 1000) * 10) / 10,
            durationMinutes: Math.round(route.duration / 60),
            mode: "road",
            isRoadRoute: true,
            isFeasible: true,
            viaSummary: route.legs?.[0]?.summary || route.legs?.[0]?.steps?.[0]?.name || "",
            transitDaysMin: 1,
            transitDaysMax: Math.max(1, Math.ceil(route.distance / 1000 / 600)),
            emissionKgCO2: Math.round((route.distance / 1000) * 0.105),
          };
        }
      }
    } catch {
      // Fallback to OSRM
    }
  }

  // 2. Try Open Source Routing Machine (OSRM) driving router (100% free, 0 config, real road data)
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4500);
    const resp = await fetch(
      `https://router.project-osrm.org/route/v1/driving/${coordString}?overview=full&geometries=geojson`,
      { signal: controller.signal },
    );
    clearTimeout(timeout);

    if (resp.ok) {
      const data = await resp.json();
      if (data.code === "Ok" && data.routes && data.routes.length > 0) {
        const route = data.routes[0];
        const coords: [number, number][] = route.geometry.coordinates.map(
          ([lon, lat]: [number, number]) => [lat, lon],
        );
        return {
          coordinates: coords,
          distanceKm: Math.round((route.distance / 1000) * 10) / 10,
          durationMinutes: Math.round(route.duration / 60),
          mode: "road",
          isRoadRoute: true,
          isFeasible: true,
          viaSummary: route.legs?.[0]?.summary || "",
          transitDaysMin: 1,
          transitDaysMax: Math.max(1, Math.ceil(route.distance / 1000 / 600)),
          emissionKgCO2: Math.round((route.distance / 1000) * 0.105),
        };
      }
    }
  } catch {
    // Graceful fallback to smooth curved path below
  }

  // 3. Fallback: Generate curved geodesic / shipping corridor points between consecutive points
  const curvedCoords: [number, number][] = [];
  for (let p = 0; p < points.length - 1; p++) {
    const p1 = points[p];
    const p2 = points[p + 1];
    const segment = generateGeodesicArc(p1[0], p1[1], p2[0], p2[1], 24);
    if (p > 0) segment.shift(); // avoid duplicate join point
    curvedCoords.push(...segment);
  }

  const isDistantWaterCrossing = directDistanceKm > 3500;

  return {
    coordinates: curvedCoords.length > 0 ? curvedCoords : points,
    distanceKm: directDistanceKm,
    durationMinutes: Math.round((directDistanceKm / 65) * 60), // estimated 65km/h average transit
    mode: "road",
    isRoadRoute: false,
    isFeasible: !isDistantWaterCrossing,
    unfeasibleReason: isDistantWaterCrossing
      ? `No overland road network connects these locations across ~${Math.round(directDistanceKm).toLocaleString()} km. Intercontinental freight requires maritime or air transport.`
      : undefined,
    recommendation: isDistantWaterCrossing
      ? "Use Ocean Cargo (🚢 Container Vessel) or Express Air Cargo (✈️ Freighter Flight)."
      : undefined,
    viaSummary: directDistanceKm > 2500 ? "Intermodal / Trans-Corridor" : "Direct Road Transit",
    transitDaysMin: 1,
    transitDaysMax: Math.max(1, Math.ceil(directDistanceKm / 500)),
    emissionKgCO2: Math.round(directDistanceKm * 0.105),
  };
}

/**
 * Universal Multi-Modal Route Resolver
 * Supports: "road" (Google Maps highway routing), "ocean" (Maritime sea lanes), and "air" (Great-Circle flight path)
 */
export async function fetchMultiModalRoute(
  points: [number, number][],
  mode: FreightMode = "road",
  startName?: string,
  endName?: string,
): Promise<RouteGeometryResult> {
  const start = points[0];
  const end = points[points.length - 1];

  if (mode === "air") {
    return buildAirRouteGeometry(start[0], start[1], end[0], end[1]);
  }
  if (mode === "ocean") {
    return buildMaritimeRouteGeometry(start[0], start[1], end[0], end[1], startName, endName);
  }
  return fetchRouteGeometry(points, startName, endName);
}

