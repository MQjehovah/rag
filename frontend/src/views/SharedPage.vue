<template>
  <div class="shared-page">
    <div v-if="loading" class="shared-center">加载中…</div>
    <div v-else-if="error" class="shared-center">
      <div class="big">🔒</div>
      <h2>分享不存在或已取消</h2>
      <p>该链接可能已被撤销或删除。</p>
    </div>
    <article v-else class="shared-doc">
      <div v-if="page.cover" class="shared-cover" :style="coverStyle"></div>
      <div class="shared-body">
        <div v-if="page.icon" class="shared-icon">{{ page.icon }}</div>
        <h1 class="shared-title">{{ page.title }}</h1>
        <div class="shared-content markdown-body" v-html="html"></div>
        <div class="shared-foot">只读分享 · 最后更新 {{ fmt }}</div>
      </div>
    </article>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownIt from 'markdown-it'
import taskLists from 'markdown-it-task-lists'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import http from '../api/http'

const route = useRoute()

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  highlight(str: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      try { return (hljs.highlight(str, { language: lang }) as any).value } catch { /* ignore */ }
    }
    return (hljs.highlightAuto(str) as any).value
  },
}).use(taskLists, { enabled: false, label: true })

const loading = ref(true)
const error = ref(false)
const page = ref({ title: '', content: '', icon: '', cover: '', updated_at: '' })

const html = computed(() =>
  DOMPurify.sanitize(md.render(page.value.content || ''), {
    ADD_TAGS: ['details', 'summary'],
    ADD_ATTR: ['data-callout', 'data-toggle', 'open', 'target'],
  })
)

const GRAD: Record<string, string> = {
  sunset: 'linear-gradient(135deg, #f97316, #ec4899)',
  ocean: 'linear-gradient(135deg, #0ea5e9, #6366f1)',
  forest: 'linear-gradient(135deg, #10b981, #0ea5e9)',
  violet: 'linear-gradient(135deg, #8b5cf6, #6366f1)',
  rose: 'linear-gradient(135deg, #fb7185, #f59e0b)',
  mint: 'linear-gradient(135deg, #2dd4bf, #10b981)',
  peach: 'linear-gradient(135deg, #fdba74, #f472b6)',
  slate: 'linear-gradient(135deg, #334155, #0f172a)',
}
const coverStyle = computed(() => {
  const c = page.value.cover || ''
  if (c.startsWith('grad:')) return { background: GRAD[c.slice(5)] || GRAD.ocean }
  return { backgroundImage: `url(${c})`, backgroundSize: 'cover', backgroundPosition: 'center' }
})
const fmt = computed(() =>
  page.value.updated_at ? new Date(page.value.updated_at).toLocaleString('zh-CN', { hour12: false }) : ''
)

onMounted(async () => {
  try {
    const res = await http.get(`/api/public/pages/${route.params.token}`)
    page.value = res.data
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.shared-page { min-height: 100vh; background: #fff; }
.shared-center {
  min-height: 80vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #9b9a97;
  gap: 6px;
}
.shared-center .big { font-size: 48px; }
.shared-center h2 { font-size: 18px; color: #37352f; }
.shared-doc { max-width: 780px; margin: 0 auto; }
.shared-cover { height: 220px; width: 100%; background: #f1f5f9; }
.shared-body { padding: 40px 56px 120px; }
.shared-icon { font-size: 44px; line-height: 1; margin-bottom: 6px; }
.shared-title { font-size: 40px; font-weight: 700; color: #37352f; letter-spacing: -0.02em; margin: 0 0 18px; }
.shared-content { font-size: 16px; line-height: 1.78; color: #1f2430; }
.shared-content :deep(h1) { font-size: 28px; margin: 26px 0 12px; }
.shared-content :deep(h2) { font-size: 22px; margin: 22px 0 10px; }
.shared-content :deep(h3) { font-size: 18px; margin: 18px 0 8px; }
.shared-content :deep(p) { margin: 10px 0; }
.shared-content :deep(a) { color: #4f46e5; text-decoration: underline; }
.shared-content :deep(code) { background: #f3f4f6; padding: 2px 6px; border-radius: 5px; font-family: 'Fira Code', monospace; font-size: 0.9em; color: #db2777; }
.shared-content :deep(pre) { background: #282c34; color: #abb2bf; padding: 14px 18px; border-radius: 10px; overflow-x: auto; }
.shared-content :deep(pre code) { background: transparent; color: inherit; padding: 0; }
.shared-content :deep(blockquote) { border-left: 3px solid #93c5fd; padding-left: 14px; color: #4b5563; background: #f8fafc; margin: 12px 0; }
.shared-content :deep(table) { border-collapse: collapse; width: 100%; margin: 14px 0; }
.shared-content :deep(th), .shared-content :deep(td) { border: 1px solid #e5e7eb; padding: 8px 12px; }
.shared-content :deep(th) { background: #f8fafc; }
.shared-content :deep(img) { max-width: 100%; border-radius: 10px; }
.shared-content :deep(mark) { background: #fef08a; padding: 1px 3px; border-radius: 3px; }
.shared-content :deep(.callout) { border-radius: 10px; padding: 12px 16px; margin: 14px 0; border-left: 4px solid #3b82f6; background: #eff6ff; }
.shared-content :deep(ul.contains-task-list) { list-style: none; padding-left: 2px; }
.shared-content :deep(li.task-list-item) { display: flex; align-items: flex-start; gap: 6px; }
.shared-foot { margin-top: 40px; padding-top: 14px; border-top: 1px solid #f0f0ef; font-size: 12px; color: #b9b9b6; }
</style>
