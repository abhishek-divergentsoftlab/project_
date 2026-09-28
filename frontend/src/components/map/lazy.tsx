/**
 * Lazy entry points for the Leaflet map components.
 *
 * Leaflet plus the routing/geo helpers are ~150 kB of JS that most visits never
 * use (list view, forms without the map picker open, a deal room without a
 * shipment). Importing the maps from here keeps them — and Leaflet — in their
 * own chunk that is fetched only when a map actually renders. The props are the
 * real components' props, so call sites do not change.
 */
import { lazy, Suspense, type ComponentProps } from "react";

const MarketplaceMapImpl = lazy(() =>
  import("@/components/map/MarketplaceMap").then((m) => ({ default: m.MarketplaceMap })),
);
const LocationMapPickerImpl = lazy(() =>
  import("@/components/map/LocationMapPicker").then((m) => ({ default: m.LocationMapPicker })),
);
const ShipmentRouteMapImpl = lazy(() =>
  import("@/components/map/ShipmentRouteMap").then((m) => ({ default: m.ShipmentRouteMap })),
);
const MatchRouteModalImpl = lazy(() =>
  import("@/components/map/MatchRouteModal").then((m) => ({ default: m.MatchRouteModal })),
);

function MapFallback() {
  return (
    <div className="loading-state" role="status" aria-live="polite" style={{ minHeight: 160 }}>
      <span className="spinner" />
      Loading map…
    </div>
  );
}

export function MarketplaceMap(props: ComponentProps<typeof MarketplaceMapImpl>) {
  return (
    <Suspense fallback={<MapFallback />}>
      <MarketplaceMapImpl {...props} />
    </Suspense>
  );
}

export function LocationMapPicker(props: ComponentProps<typeof LocationMapPickerImpl>) {
  return (
    <Suspense fallback={<MapFallback />}>
      <LocationMapPickerImpl {...props} />
    </Suspense>
  );
}

export function ShipmentRouteMap(props: ComponentProps<typeof ShipmentRouteMapImpl>) {
  return (
    <Suspense fallback={<MapFallback />}>
      <ShipmentRouteMapImpl {...props} />
    </Suspense>
  );
}

/** The modal is always mounted by Matches; only fetch the chunk once it opens. */
export function MatchRouteModal(props: ComponentProps<typeof MatchRouteModalImpl>) {
  if (!props.isOpen || !props.candidate) return null;
  return (
    <Suspense fallback={null}>
      <MatchRouteModalImpl {...props} />
    </Suspense>
  );
}
