<template>
  <node-view-wrapper as="span" class="img-node" :class="{ selected }">
    <span class="img-wrap" :style="wrapStyle">
      <img
        :src="node.attrs.src"
        :alt="node.attrs.alt || ''"
        :title="node.attrs.title || ''"
        draggable="false"
      />
      <span v-if="selected" class="img-resize" contenteditable="false" @mousedown.stop.prevent="startResize"></span>
      <span v-if="selected && editable" class="img-tools" contenteditable="false">
        <input
          class="img-caption"
          :value="node.attrs.title || ''"
          placeholder="添加图注…"
          @input="onCaption"
          @keydown.stop
        />
      </span>
    </span>
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { NodeViewWrapper, nodeViewProps } from '@tiptap/vue-3'

const props = defineProps(nodeViewProps)

const editable = computed(() => props.editor?.isEditable ?? true)
const previewWidth = ref<number | null>(props.node.attrs.width || null)

const wrapStyle = computed(() => (previewWidth.value ? `width:${previewWidth.value}px` : ''))

function onCaption(e: Event) {
  props.updateAttributes?.({ title: (e.target as HTMLInputElement).value })
}

function startResize(e: MouseEvent) {
  const img = (e.currentTarget as HTMLElement).parentElement?.querySelector('img') as HTMLImageElement | null
  const startX = e.clientX
  const startW = img?.getBoundingClientRect().width || previewWidth.value || 0
  const onMove = (ev: MouseEvent) => {
    previewWidth.value = Math.max(60, Math.round(startW + (ev.clientX - startX)))
  }
  const onUp = () => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    props.updateAttributes?.({ width: previewWidth.value })
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
</script>

<style scoped>
.img-node {
  display: inline-block;
  vertical-align: bottom;
  max-width: 100%;
}
.img-wrap {
  position: relative;
  display: inline-block;
  max-width: 100%;
  line-height: 0;
}
.img-wrap img {
  max-width: 100%;
  height: auto;
  border-radius: 10px;
  display: block;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.06);
}
.img-node.selected .img-wrap img {
  outline: 2px solid #3b82f6;
  outline-offset: 1px;
}
.img-resize {
  position: absolute;
  right: -6px;
  bottom: -6px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #3b82f6;
  border: 2px solid #fff;
  cursor: nwse-resize;
  z-index: 3;
}
.img-tools {
  position: absolute;
  left: 50%;
  bottom: -34px;
  transform: translateX(-50%);
  z-index: 3;
}
.img-caption {
  width: 260px;
  max-width: 60vw;
  padding: 5px 10px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  font-size: 12px;
  color: #334155;
  background: #fff;
  outline: none;
  box-shadow: 0 6px 18px rgba(15, 23, 42, 0.12);
  line-height: 1.4;
}
</style>
