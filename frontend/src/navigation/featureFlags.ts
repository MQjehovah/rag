import { ref } from 'vue'
import http from '../api/http'

export const featureFlags = ref<Record<string, boolean>>({})
let loadPromise: Promise<Record<string, boolean>> | null = null

export async function loadFeatureFlags(force = false): Promise<Record<string, boolean>> {
  if (!force && Object.keys(featureFlags.value).length) return featureFlags.value
  if (!force && loadPromise) return loadPromise
  loadPromise = http.get('/api/p7/flags')
    .then((res) => {
      featureFlags.value = res.data.flags || {}
      return featureFlags.value
    })
    .catch(() => {
      featureFlags.value = {}
      return featureFlags.value
    })
    .finally(() => { loadPromise = null })
  return loadPromise
}

export function setFeatureFlagValue(name: string, value: boolean) {
  featureFlags.value = { ...featureFlags.value, [name]: value }
}

export function clearFeatureFlags() {
  featureFlags.value = {}
  loadPromise = null
}

export function featureVisible(name?: string): boolean {
  return !name || featureFlags.value[name] === true
}
