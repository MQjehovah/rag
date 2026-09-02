<template>
  <article ref="rootRef" class="markdown-preview" v-html="renderedContent"></article>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import { renderMarkdownForDisplay } from '../utils/markdownDisplay'

const props = defineProps<{
  content: string
}>()

const rootRef = ref<HTMLElement>()

// 图片加载失败占位：1x1 透明 SVG（data URI），避免执行任何脚本。
const IMG_PLACEHOLDER =
  'data:image/svg+xml;charset=utf-8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">' +
    '<rect width="16" height="16" fill="#e5e7eb"/>' +
    '<text x="8" y="12" font-size="9" text-anchor="middle" fill="#9ca3af">图片</text>' +
    '</svg>',
  )

// 危险协议白名单：只放行安全协议与站内相对路径，拒绝 javascript:/vbscript:/data:(非图片) 等。
function isSafeUrl(url: string): boolean {
  const trimmed = (url || '').trim().toLowerCase()
  if (trimmed.startsWith('/') || trimmed.startsWith('#') || trimmed.startsWith('./') || trimmed.startsWith('../')) {
    return true
  }
  if (trimmed.startsWith('data:')) {
    return trimmed.startsWith('data:image/')
  }
  return /^(https?:|mailto:)/.test(trimmed)
}

const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

// 覆盖链接校验：危险协议（javascript:/vbscript: 等）一律拒绝。
const defaultValidate = markdown.validateLink.bind(markdown)
markdown.validateLink = (url: string) => isSafeUrl(url) && defaultValidate(url)

const renderedContent = computed(() => renderMarkdownForDisplay(
  props.content || '',
  (value) => markdown.render(value),
))

/** 渲染后为所有 <img> 绑定加载失败占位。 */
function attachImageFallback() {
  const root = rootRef.value
  if (!root) return
  const images = root.querySelectorAll('img')
  images.forEach((img) => {
    const onError = () => {
      if (img.getAttribute('src') !== IMG_PLACEHOLDER) {
        img.setAttribute('src', IMG_PLACEHOLDER)
        img.classList.add('img-fallback')
      }
    }
    img.removeEventListener('error', onError)
    img.addEventListener('error', onError)
    // 已加载完成但 src 为空/非法时同样降级。
    if (!img.getAttribute('src')) onError()
  })
}

onMounted(() => {
  attachImageFallback()
})

watch(renderedContent, async () => {
  await nextTick()
  attachImageFallback()
})

onBeforeUnmount(() => {
  const root = rootRef.value
  if (!root) return
  root.querySelectorAll('img').forEach((img) => img.removeEventListener('error', () => {}))
})
</script>

<style scoped>
.markdown-preview {
  min-height: 400px;
  color: #374151;
  font-size: 16px;
  line-height: 1.75;
  overflow-wrap: anywhere;
}

.markdown-preview :deep(h1) { font-size: 28px; margin: 24px 0 16px; }
.markdown-preview :deep(h2) { font-size: 22px; margin: 22px 0 12px; }
.markdown-preview :deep(h3) { font-size: 18px; margin: 18px 0 10px; }
.markdown-preview :deep(p) { margin: 10px 0; }
.markdown-preview :deep(ul),
.markdown-preview :deep(ol) { padding-left: 28px; }
.markdown-preview :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 14px;
  border-left: 4px solid #3b82f6;
  background: #f8fafc;
  color: #64748b;
}
.markdown-preview :deep(table) {
  width: 100%;
  margin: 16px 0;
  border-collapse: collapse;
}
.markdown-preview :deep(.merged-table-wrapper) {
  width: 100%;
  overflow-x: auto;
}
.markdown-preview :deep(th),
.markdown-preview :deep(td) {
  padding: 9px 12px;
  border: 1px solid #dbe2ea;
  text-align: left;
  vertical-align: top;
}
.markdown-preview :deep(th) { background: #f1f5f9; }
.markdown-preview :deep(img) {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 16px auto;
  border-radius: 8px;
}
.markdown-preview :deep(img.img-fallback) {
  width: 48px;
  height: 48px;
  padding: 8px;
  background: #f3f4f6;
  border: 1px dashed #d1d5db;
}
.markdown-preview :deep(pre) {
  padding: 14px 16px;
  overflow-x: auto;
  border-radius: 8px;
  background: #1f2937;
  color: #e5e7eb;
}
.markdown-preview :deep(code) {
  padding: 2px 5px;
  border-radius: 4px;
  background: #f1f5f9;
}
.markdown-preview :deep(pre code) { padding: 0; background: transparent; }
.markdown-preview :deep(a) { color: #2563eb; }
</style>
