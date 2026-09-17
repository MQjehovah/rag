<template>
  <el-select
    :model-value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :loading="loading"
    class="ws-select"
    @update:model-value="onChange"
  >
    <el-option
      v-for="ws in workspaces"
      :key="ws.id"
      :label="optionLabel(ws.id)"
      :value="ws.id"
    />
  </el-select>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { WikiWorkspaceSummary } from '../../api/wikiWorkspaces'
import { buildWorkspaceOptionLabels } from '../../utils/workspaceLabels'

/** Phase 8A：工作区选择器（纯展示层，只显示名称，不含 ACL/scope_id/key）。 */
const props = withDefaults(
  defineProps<{
    modelValue: string
    workspaces: WikiWorkspaceSummary[]
    disabled?: boolean
    loading?: boolean
    placeholder?: string
  }>(),
  { disabled: false, loading: false, placeholder: '选择工作区' },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

const optionLabels = computed(() => buildWorkspaceOptionLabels(props.workspaces))

function optionLabel(id: string): string {
  return optionLabels.value.get(id) || id
}

function onChange(value: unknown) {
  emit('update:modelValue', typeof value === 'string' ? value : '')
}
</script>

<style scoped>
.ws-select {
  width: 260px;
}
@media (max-width: 720px) {
  .ws-select {
    width: 180px;
  }
}
</style>
