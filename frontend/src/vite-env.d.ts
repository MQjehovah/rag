/// <reference types="vite/client" />

import 'vue-router'
import type { Permission } from './constants/perms'

interface ImportMetaEnv {
  /** 协同编辑开关; 'false'/'0' 关闭(默认开启)。运行期可用 ?collab=0 一键退回单人。 */
  readonly VITE_RAG_COLLAB_ENABLED?: string
}

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    perm?: Permission
  }
}
