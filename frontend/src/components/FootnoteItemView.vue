<template>
  <node-view-wrapper as="div" class="footnote-item" :class="{ selected }" :data-footnote="label">
    <button
      class="fn-label"
      contenteditable="false"
      title="跳转到引用位置"
      @mousedown.stop.prevent
      @click.stop.prevent="jump"
    >[^{{ label }}]</button>
    <node-view-content class="fn-text" />
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NodeViewWrapper, NodeViewContent, nodeViewProps } from '@tiptap/vue-3'
import { findFootnoteRef, flashAndScroll } from './editorFootnotes'

const props = defineProps(nodeViewProps)

const label = computed(() => String(props.node.attrs.label ?? ''))

function jump() {
  const dom = props.editor?.view?.dom as HTMLElement | undefined
  if (!dom) return
  flashAndScroll(findFootnoteRef(dom, label.value))
}
</script>

<style scoped>
.footnote-item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 3px 4px;
  border-radius: 6px;
}

.footnote-item.selected {
  background: #f0f7ff;
  box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.3);
}

.fn-label {
  flex: 0 0 auto;
  margin-top: 3px;
  padding: 0 5px;
  border: none;
  border-radius: 5px;
  background: #eef2f7;
  color: #2563eb;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.6;
  cursor: pointer;
}

.fn-label:hover {
  background: #dbeafe;
}

.fn-text {
  flex: 1 1 auto;
  min-width: 0;
}

.fn-text :deep(p) {
  margin: 0;
}
</style>
