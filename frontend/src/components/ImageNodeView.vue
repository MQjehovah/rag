<template>
  <node-view-wrapper as="span" class="img-node" :class="{ selected }" ref="wrapRef">
    <span class="img-wrap" :style="wrapStyle">
      <img
        :src="displaySrc"
        :alt="node.attrs.alt || ''"
        :title="node.attrs.title || ''"
        draggable="false"
        loading="lazy"
        decoding="async"
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
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { NodeViewWrapper, nodeViewProps } from '@tiptap/vue-3'
import { createLazyObserver, type LazyObserver } from '../utils/lazyRender'

const props = defineProps(nodeViewProps)

const editable = computed(() => props.editor?.isEditable ?? true)
const previewWidth = ref<number | null>(props.node.attrs.width || null)

const wrapStyle = computed(() => (previewWidth.value ? `width:${previewWidth.value}px` : ''))

// 图片惰性加载: 未进入视口前用 1x1 透明占位, 进入视口(含 300px 预加载边距)才真正请求
const PLACEHOLDER_SRC = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'
const visible = ref(false)
const wrapRef = ref<{ $el?: HTMLElement } | null>(null)
let observer: LazyObserver | null = null

const displaySrc = computed(() => {
  const src = String(props.node.attrs.src || '')
  if (visible.value || src.startsWith('data:')) return src
  return PLACEHOLDER_SRC
})

onMounted(() => {
  const el = wrapRef.value?.$el
  if (!el) {
    visible.value = true
    return
  }
  observer = createLazyObserver(() => { visible.value = true }, '300px 0px')
  observer.observe(el)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

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
