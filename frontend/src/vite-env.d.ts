/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_PROXY_TARGET?: string;
  readonly VITE_MAPBOX_TOKEN?: string;
  readonly VITE_GEOAPIFY_API_KEY?: string;
  readonly VITE_LOCATIONIQ_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
