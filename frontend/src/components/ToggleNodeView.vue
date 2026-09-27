<template>
  <node-view-wrapper class="toggle-block" :class="{ 'is-open': node.attrs.open }">
    <div class="toggle-head" contenteditable="false">
      <button class="toggle-caret" :class="{ 'is-open': node.attrs.open }" @click="toggleOpen" title="展开/收起">▸</button>
      <input
        class="toggle-title"
        :value="node.attrs.title"
        placeholder="折叠标题"
        @input="onTitle"
        @keydown.stop
      />
    </div>
    <div v-show="node.attrs.open" class="toggle-body">
      <node-view-content />
    </div>
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { NodeViewWrapper, NodeViewContent, nodeViewProps } from '@tiptap/vue-3'

const props = defineProps(nodeViewProps)

function toggleOpen() {
  props.updateAttributes?.({ open: !props.node.attrs.open })
}

function onTitle(e: Event) {
  props.updateAttributes?.({ title: (e.target as HTMLInputElement).value })
}
</script>

<style scoped>
.toggle-block {
  margin: 8px 0;
  border-radius: 8px;
}
.toggle-head {
  display: flex;
  align-items: center;
  gap: 4px;
}
.toggle-caret {
  border: none;
  background: transparent;
  cursor: pointer;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1;
  padding: 4px;
  border-radius: 5px;
  transition: transform 0.15s;
}
.toggle-caret:hover { background: #f1f5f9; color: #475569; }
.toggle-caret.is-open { transform: rotate(90deg); }
.toggle-title {
  flex: 1 1 auto;
  border: none;
  outline: none;
  background: transparent;
  font-size: 16px;
  font-weight: 600;
  color: #1f2937;
  padding: 4px 0;
}
.toggle-title::placeholder { color: #cbd5e1; }
.toggle-body {
  padding-left: 24px;
  border-left: 2px solid #eef2f7;
  margin-left: 9px;
}
</style>
