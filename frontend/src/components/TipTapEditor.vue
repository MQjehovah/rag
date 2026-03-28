<template>
  <div class="tiptap-editor">
    <!-- 工具栏 -->
    <div class="editor-toolbar" v-if="editor">
      <button @click="editor.chain().focus().toggleBold().run()" :class="{ 'is-active': editor.isActive('bold') }">B</button>
      <button @click="editor.chain().focus().toggleItalic().run()" :class="{ 'is-active': editor.isActive('italic') }">I</button>
      <button @click="editor.chain().focus().toggleStrike().run()" :class="{ 'is-active': editor.isActive('strike') }">S</button>
      <span class="divider"></span>
      <button @click="editor.chain().focus().toggleHeading({ level: 1 }).run()" :class="{ 'is-active': editor.isActive('heading', { level: 1 }) }">H1</button>
      <button @click="editor.chain().focus().toggleHeading({ level: 2 }).run()" :class="{ 'is-active': editor.isActive('heading', { level: 2 }) }">H2</button>
      <button @click="editor.chain().focus().toggleHeading({ level: 3 }).run()" :class="{ 'is-active': editor.isActive('heading', { level: 3 }) }">H3</button>
      <span class="divider"></span>
      <button @click="editor.chain().focus().toggleBulletList().run()" :class="{ 'is-active': editor.isActive('bulletList') }">•</button>
      <button @click="editor.chain().focus().toggleOrderedList().run()" :class="{ 'is-active': editor.isActive('orderedList') }">1.</button>
      <button @click="editor.chain().focus().toggleBlockquote().run()" :class="{ 'is-active': editor.isActive('blockquote') }">"</button>
      <button @click="editor.chain().focus().toggleCode().run()" :class="{ 'is-active': editor.isActive('code') }">&lt;/&gt;</button>
      <button @click="editor.chain().focus().toggleCodeBlock().run()" :class="{ 'is-active': editor.isActive('codeBlock') }">```</button>
      <span class="divider"></span>
      <button @click="handleImageUpload" title="插入图片">🖼</button>
      <button @click="editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()" title="插入表格">📊</button>
      <button @click="insertMermaid" title="插入图表">🔀</button>
      <span class="divider"></span>
      <button @click="editor.chain().focus().undo().run()">↩</button>
      <button @click="editor.chain().focus().redo().run()">↪</button>
    </div>
    
    <editor-content :editor="editor" class="editor-content" />
  </div>
</template>

<script setup lang="ts">
import { watch, onBeforeUnmount } from 'vue'
import { useEditor, EditorContent } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Image from '@tiptap/extension-image'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import mermaid from 'mermaid'
import axios from 'axios'
import { ElMessage } from 'element-plus'
import { nextTick } from 'vue'

const props = defineProps<{
  modelValue: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
})

const editor = useEditor({
  extensions: [
    StarterKit,
    Placeholder.configure({
      placeholder: '开始写笔记...',
    }),
    Image.configure({
      inline: true,
      allowBase64: true,
    }),
    Table.configure({
      resizable: true,
    }),
    TableRow,
    TableCell,
    TableHeader,
  ],
  content: props.modelValue,
  onUpdate: ({ editor }) => {
    emit('update:modelValue', editor.getHTML())
    nextTick(() => renderMermaid())
  },
})

watch(() => props.modelValue, (newValue) => {
  if (editor.value && editor.value.getHTML() !== newValue) {
    editor.value.commands.setContent(newValue || '')
    nextTick(() => renderMermaid())
  }
})

const renderMermaid = async () => {
  await new Promise(resolve => setTimeout(resolve, 100))
  
  const editorEl = document.querySelector('.ProseMirror')
  if (!editorEl) return
  
  const diagramDivs = editorEl.querySelectorAll('.mermaid-diagram')
  
  for (const diagramDiv of diagramDivs) {
    if (diagramDiv.getAttribute('data-rendered') === 'true') continue
    
    const codeText = diagramDiv.getAttribute('data-code') || ''
    if (!codeText.trim()) continue
    
    diagramDiv.setAttribute('data-rendered', 'true')
    
    try {
      const id = `mermaid-${Math.random().toString(36).substr(2, 9)}`
      const { svg } = await mermaid.render(id, codeText)
      diagramDiv.innerHTML = svg
    } catch (e) {
      console.error('Mermaid error:', e)
    }
  }
}

const handleImageUpload = () => {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/*'
  input.onchange = async (e: Event) => {
    const file = (e.target as HTMLInputElement).files?.[0]
    if (!file || !editor.value) return
    
    try {
      const formData = new FormData()
      formData.append('file', file)
      const res = await axios.post('/api/upload/image', formData)
      editor.value.chain().focus().setImage({ src: res.data.url }).run()
    } catch {
      ElMessage.error('图片上传失败')
    }
  }
  input.click()
}

const insertMermaid = () => {
  const template = `graph TD
A[开始] --> B{判断}
B -->|Yes| C[成功]
B -->|No| D[失败]`
  
  const html = `<pre class="mermaid-block"><code class="language-mermaid">${template}</code></pre><div class="mermaid-diagram" data-code="${template}"></div><p></p>`
  editor.value?.chain().focus().insertContent(html).run()
  
  nextTick(() => renderMermaid())
}

onBeforeUnmount(() => {
  editor.value?.destroy()
})
</script>

<style scoped>
.tiptap-editor {
  width: 100%;
}

.editor-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 10px;
  background: #f8f9fa;
  border-radius: 6px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

.editor-toolbar button {
  padding: 6px 10px;
  border: none;
  background: transparent;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: #374151;
  transition: all 0.2s;
}

.editor-toolbar button:hover {
  background: #e5e7eb;
}

.editor-toolbar button.is-active {
  background: #3b82f6;
  color: #fff;
}

.editor-toolbar .divider {
  width: 1px;
  height: 20px;
  background: #e5e7eb;
  margin: 0 8px;
}

.editor-content {
  min-height: 400px;
}

.editor-content :deep(.ProseMirror) {
  outline: none;
  min-height: 400px;
  font-size: 16px;
  line-height: 1.7;
  color: #374151;
}

.editor-content :deep(.ProseMirror p) {
  margin: 12px 0;
}

.editor-content :deep(.ProseMirror h1) {
  font-size: 28px;
  font-weight: 700;
  margin: 24px 0 16px;
}

.editor-content :deep(.ProseMirror h2) {
  font-size: 22px;
  font-weight: 600;
  margin: 20px 0 12px;
}

.editor-content :deep(.ProseMirror h3) {
  font-size: 18px;
  font-weight: 600;
  margin: 16px 0 10px;
}

.editor-content :deep(.ProseMirror code) {
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: monospace;
}

.editor-content :deep(.ProseMirror pre) {
  background: #1f2937;
  color: #f3f4f6;
  padding: 16px;
  border-radius: 8px;
  overflow-x: auto;
}

.editor-content :deep(.ProseMirror ul),
.editor-content :deep(.ProseMirror ol) {
  padding-left: 24px;
}

.editor-content :deep(.ProseMirror blockquote) {
  border-left: 4px solid #3b82f6;
  padding-left: 16px;
  color: #6b7280;
}

.editor-content :deep(.ProseMirror img) {
  max-width: 100%;
  border-radius: 8px;
}

.editor-content :deep(.ProseMirror table) {
  border-collapse: collapse;
  margin: 16px 0;
  width: 100%;
}

.editor-content :deep(.ProseMirror th),
.editor-content :deep(.ProseMirror td) {
  border: 1px solid #e5e7eb;
  padding: 10px 14px;
  min-width: 100px;
}

.editor-content :deep(.ProseMirror th) {
  background: #f9fafb;
  font-weight: 600;
}

.editor-content :deep(.ProseMirror .mermaid-diagram) {
  background: #fff;
  padding: 20px;
  border-radius: 8px;
  margin: 16px 0;
  text-align: center;
}

.editor-content :deep(.ProseMirror pre.mermaid-block) {
  display: none;
}
</style>