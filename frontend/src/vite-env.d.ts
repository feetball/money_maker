/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" in mock mode (`npm run dev:mock`, loaded from .env.mock). */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
