/// <reference types="vite/client" />

import 'vue-router'
import type { Permission } from './constants/perms'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    perm?: Permission
  }
}
