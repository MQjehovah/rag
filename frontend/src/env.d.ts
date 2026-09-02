/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEV_PORT?: string
  readonly VITE_API_PROXY_TARGET?: string
  readonly VITE_ACCEPTANCE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
