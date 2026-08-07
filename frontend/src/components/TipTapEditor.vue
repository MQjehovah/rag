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
import { watch, onBeforeUnmount, nextTick } from 'vue'
import { useEditor, EditorContent, VueNodeViewRenderer } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Image from '@tiptap/extension-image'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import CodeBlockLowlight from '@tiptap/extension-code-block-lowlight'
import { createLowlight } from 'lowlight'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import java from 'highlight.js/lib/languages/java'
import go from 'highlight.js/lib/languages/go'
import rust from 'highlight.js/lib/languages/rust'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'
import sql from 'highlight.js/lib/languages/sql'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import yaml from 'highlight.js/lib/languages/yaml'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import markdown from 'highlight.js/lib/languages/markdown'
import CodeBlockComponent from './CodeBlockComponent.vue'
import { Markdown } from 'tiptap-markdown'
import mermaid from 'mermaid'
import http from '../api/http'
import { ElMessage } from 'element-plus'
import { Plugin, PluginKey } from 'prosemirror-state'

const uploadAndInsert = (view: any, file: File) => {
  const formData = new FormData()
  formData.append('file', file)
  http.post('/api/upload/image', formData).then(res => {
    view.dispatch(view.state.tr.replaceSelectionWith(
      view.state.schema.nodes.image.create({ src: res.data.url })
    ))
  }).catch(() => {
    ElMessage.error('图片上传失败')
  })
}

const lowlight = createLowlight()
lowlight.register('javascript', javascript)
lowlight.register('typescript', typescript)
lowlight.register('python', python)
lowlight.register('java', java)
lowlight.register('go', go)
lowlight.register('rust', rust)
lowlight.register('c', c)
lowlight.register('cpp', cpp)
lowlight.register('sql', sql)
lowlight.register('bash', bash)
lowlight.register('json', json)
lowlight.register('yaml', yaml)
lowlight.register('xml', xml)
lowlight.register('html', xml)
lowlight.register('css', css)
lowlight.register('markdown', markdown)

const props = defineProps<{
  modelValue: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

// Last markdown this component emitted back to the parent.  Comparing against
// this is much cheaper than serializing the whole document on every prop
// change, and it prevents feedback loops without re-parsing content.
let lastEmitted = props.modelValue

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
})

const editor = useEditor({
  extensions: [
    StarterKit.configure({
      codeBlock: false,
    }),
    CodeBlockLowlight
      .extend({
        addNodeView() {
          return VueNodeViewRenderer(CodeBlockComponent)
        },
      })
      .configure({
        lowlight,
        defaultLanguage: 'plaintext',
      }),
    Placeholder.configure({
      placeholder: '开始写笔记...',
    }),
    Image.extend({
      addProseMirrorPlugins() {
        return [
          new Plugin({
            key: new PluginKey('imagePasteDrop'),
            props: {
              handlePaste(view, event) {
                const items = event.clipboardData?.items
                if (!items) return false
                for (const item of items) {
                  if (item.type.startsWith('image/')) {
                    event.preventDefault()
                    const file = item.getAsFile()
                    if (file) uploadAndInsert(view, file)
                    return true
                  }
                }
                return false
              },
              handleDrop(view, event) {
                const files = event.dataTransfer?.files
                if (!files) return false
                for (const file of files) {
                  if (file.type.startsWith('image/')) {
                    event.preventDefault()
                    uploadAndInsert(view, file)
                    return true
                  }
                }
                return false
              },
            },
          }),
        ]
      },
    }).configure({
      inline: true,
      allowBase64: true,
    }),
    Table.configure({
      resizable: true,
    }),
    TableRow,
    TableCell,
    TableHeader,
    Markdown.configure({
      html: true,
      breaks: true,
      linkify: true,
    }),
  ],
  content: props.modelValue,
  onUpdate: ({ editor }) => {
    const markdown = editor.storage.markdown.getMarkdown()
    lastEmitted = markdown
    emit('update:modelValue', markdown)
    nextTick(() => {
      scheduleMermaid()
      disableSpellcheck()
    })
  },
  onCreate: () => {
    nextTick(() => {
      scheduleMermaid()
      disableSpellcheck()
    })
  },
})

watch(() => props.modelValue, (newValue) => {
  if (editor.value && lastEmitted !== newValue) {
    editor.value.commands.setContent(newValue || '')
    mermaidCache.clear()
    nextTick(() => {
      scheduleMermaid()
      disableSpellcheck()
    })
  }
})

let mermaidTimer: number | null = null
const mermaidCache = new Map<string, string>()

const scheduleMermaid = () => {
  if (mermaidTimer) clearTimeout(mermaidTimer)
  mermaidTimer = window.setTimeout(() => { renderMermaid() }, 300)
}

const renderMermaid = async () => {
  if (mermaidTimer) {
    clearTimeout(mermaidTimer)
    mermaidTimer = null
  }

  const editorEl = document.querySelector('.ProseMirror')
  if (!editorEl) return

  const mermaidBlocks = editorEl.querySelectorAll('.language-mermaid')

  for (const block of mermaidBlocks) {
    const codeBlock = block.querySelector('code')
    if (!codeBlock) continue

    const codeText = codeBlock.textContent || ''
    if (!codeText.trim()) continue

    const diagramDiv = block.querySelector('.mermaid-diagram')
    const cachedSvg = mermaidCache.get(codeText)
    if (diagramDiv && cachedSvg && diagramDiv.innerHTML === cachedSvg) continue

    try {
      let svg = cachedSvg
      if (!svg) {
        const id = `mermaid-${Math.random().toString(36).substr(2, 9)}`
        const rendered = await mermaid.render(id, codeText)
        svg = rendered.svg
        mermaidCache.set(codeText, svg)
      }

      let targetDiv = diagramDiv
      if (!targetDiv) {
        targetDiv = document.createElement('div')
        targetDiv.className = 'mermaid-diagram'
        block.appendChild(targetDiv)
      }
      targetDiv.innerHTML = svg
    } catch (e) {
      console.error('Mermaid error:', e)
    }
  }
}

const disableSpellcheck = () => {
  const editorEl = document.querySelector('.ProseMirror')
  if (!editorEl) return
  
  editorEl.setAttribute('spellcheck', 'false')
  editorEl.setAttribute('autocorrect', 'off')
  editorEl.setAttribute('autocomplete', 'off')
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
      const res = await http.post('/api/upload/image', formData)
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
  
  editor.value?.chain().focus().toggleCodeBlock().run()
  
  const { $from } = editor.value!.state.selection
  const node = $from.node()
  if (node.type.name === 'codeBlock') {
    editor.value?.chain().focus().updateAttributes('codeBlock', { language: 'mermaid' }).run()
    editor.value?.chain().focus().insertContent(template).run()
  }
  
  scheduleMermaid()
}

onBeforeUnmount(() => {
  if (mermaidTimer) {
    clearTimeout(mermaidTimer)
    mermaidTimer = null
  }
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
  background: #282c34;
  color: #abb2bf;
  padding: 16px 20px;
  border-radius: 8px;
  overflow-x: auto;
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 14px;
  line-height: 1.6;
  margin: 16px 0;
}

.editor-content :deep(.ProseMirror pre code) {
  background: transparent;
  padding: 0;
  color: inherit;
  font-size: inherit;
  font-family: inherit;
}

.editor-content :deep(.ProseMirror pre code .hljs-comment),
.editor-content :deep(.ProseMirror pre code .hljs-quote) {
  color: #5c6370;
  font-style: italic;
}

.editor-content :deep(.ProseMirror pre code .hljs-doctag),
.editor-content :deep(.ProseMirror pre code .hljs-keyword),
.editor-content :deep(.ProseMirror pre code .hljs-formula) {
  color: #c678dd;
}

.editor-content :deep(.ProseMirror pre code .hljs-section),
.editor-content :deep(.ProseMirror pre code .hljs-name),
.editor-content :deep(.ProseMirror pre code .hljs-tag),
.editor-content :deep(.ProseMirror pre code .hljs-selector-tag),
.editor-content :deep(.ProseMirror pre code .hljs-deletion),
.editor-content :deep(.ProseMirror pre code .hljs-subst) {
  color: #e06c75;
}

.editor-content :deep(.ProseMirror pre code .hljs-literal) {
  color: #56b6c2;
}

.editor-content :deep(.ProseMirror pre code .hljs-string),
.editor-content :deep(.ProseMirror pre code .hljs-regexp),
.editor-content :deep(.ProseMirror pre code .hljs-addition),
.editor-content :deep(.ProseMirror pre code .hljs-attribute),
.editor-content :deep(.ProseMirror pre code .hljs-meta .hljs-string) {
  color: #98c379;
}

.editor-content :deep(.ProseMirror pre code .hljs-attr),
.editor-content :deep(.ProseMirror pre code .hljs-variable),
.editor-content :deep(.ProseMirror pre code .hljs-template-variable),
.editor-content :deep(.ProseMirror pre code .hljs-type),
.editor-content :deep(.ProseMirror pre code .hljs-selector-class),
.editor-content :deep(.ProseMirror pre code .hljs-selector-attr),
.editor-content :deep(.ProseMirror pre code .hljs-selector-pseudo),
.editor-content :deep(.ProseMirror pre code .hljs-number) {
  color: #d19a66;
}

.editor-content :deep(.ProseMirror pre code .hljs-symbol),
.editor-content :deep(.ProseMirror pre code .hljs-bullet),
.editor-content :deep(.ProseMirror pre code .hljs-link),
.editor-content :deep(.ProseMirror pre code .hljs-meta),
.editor-content :deep(.ProseMirror pre code .hljs-selector-id),
.editor-content :deep(.ProseMirror pre code .hljs-title) {
  color: #61afef;
}

.editor-content :deep(.ProseMirror pre code .hljs-built_in),
.editor-content :deep(.ProseMirror pre code .hljs-title.class_),
.editor-content :deep(.ProseMirror pre code .hljs-class .hljs-title) {
  color: #e6c07b;
}

.editor-content :deep(.ProseMirror pre code .hljs-emphasis) {
  font-style: italic;
}

.editor-content :deep(.ProseMirror pre code .hljs-strong) {
  font-weight: bold;
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
  margin-top: 16px;
  text-align: center;
}

.editor-content :deep(.ProseMirror .language-mermaid) {
  border: 2px solid #89b4fa;
}

.editor-content :deep(.ProseMirror pre.mermaid-block) {
  display: none;
}
</style>
