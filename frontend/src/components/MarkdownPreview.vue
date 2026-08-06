<template>
  <article class="markdown-preview" v-html="renderedContent"></article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { renderMarkdownForDisplay } from '../utils/markdownDisplay'

const props = defineProps<{
  content: string
}>()

const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

const renderedContent = computed(() => renderMarkdownForDisplay(
  props.content || '',
  (value) => markdown.render(value),
))
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
</style>
