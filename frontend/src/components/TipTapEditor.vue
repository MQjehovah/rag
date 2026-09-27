<template>
  <div class="tiptap-editor">
    <!-- 工具栏 -->
    <div class="editor-toolbar" v-if="editor">
      <select class="tb-select" :value="headingValue" @change="setHeading" title="段落样式">
        <option value="p">正文</option>
        <option value="1">标题 1</option>
        <option value="2">标题 2</option>
        <option value="3">标题 3</option>
      </select>
      <span class="divider"></span>
      <button @click="editor.chain().focus().toggleBold().run()" :class="{ 'is-active': editor.isActive('bold') }" title="加粗">B</button>
      <button @click="editor.chain().focus().toggleItalic().run()" :class="{ 'is-active': editor.isActive('italic') }" title="斜体"><i>I</i></button>
      <button @click="editor.chain().focus().toggleUnderline().run()" :class="{ 'is-active': editor.isActive('underline') }" title="下划线"><u>U</u></button>
      <button @click="editor.chain().focus().toggleStrike().run()" :class="{ 'is-active': editor.isActive('strike') }" title="删除线"><s>S</s></button>
      <button @click="editor.chain().focus().toggleHighlight().run()" :class="{ 'is-active': editor.isActive('highlight') }" title="高亮">▨</button>
      <button @click="editor.chain().focus().toggleCode().run()" :class="{ 'is-active': editor.isActive('code') }" title="行内代码">&lt;/&gt;</button>
      <button @click="setLink" :class="{ 'is-active': editor.isActive('link') }" title="链接">🔗</button>
      <span class="divider"></span>
      <button @click="editor.chain().focus().toggleBulletList().run()" :class="{ 'is-active': editor.isActive('bulletList') }" title="无序列表">•</button>
      <button @click="editor.chain().focus().toggleOrderedList().run()" :class="{ 'is-active': editor.isActive('orderedList') }" title="有序列表">1.</button>
      <button @click="editor.chain().focus().toggleTaskList().run()" :class="{ 'is-active': editor.isActive('taskList') }" title="待办列表">☑</button>
      <button @click="editor.chain().focus().toggleBlockquote().run()" :class="{ 'is-active': editor.isActive('blockquote') }" title="引用">❝</button>
      <button @click="editor.chain().focus().toggleCodeBlock().run()" :class="{ 'is-active': editor.isActive('codeBlock') }" title="代码块">```</button>
      <span class="divider"></span>
      <button @click="editor.chain().focus().setTextAlign('left').run()" :class="{ 'is-active': editor.isActive({ textAlign: 'left' }) }" title="左对齐">⬅</button>
      <button @click="editor.chain().focus().setTextAlign('center').run()" :class="{ 'is-active': editor.isActive({ textAlign: 'center' }) }" title="居中">↔</button>
      <button @click="editor.chain().focus().setTextAlign('right').run()" :class="{ 'is-active': editor.isActive({ textAlign: 'right' }) }" title="右对齐">➡</button>
      <span class="divider"></span>
      <button @click="handleImageUpload" title="插入图片">🖼</button>
      <button @click="editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()" title="插入表格">▦</button>
      <button @click="insertMermaid" title="插入图表">◈</button>
      <span class="divider"></span>
      <button @click="editor.chain().focus().undo().run()" :disabled="!editor.can().undo()" title="撤销">↩</button>
      <button @click="editor.chain().focus().redo().run()" :disabled="!editor.can().redo()" title="重做">↪</button>
    </div>

    <editor-content :editor="editor" class="editor-content" />

    <div v-if="editor" class="editor-status">
      <span>{{ charCount }} 字</span>
      <span class="hint">输入 “/” 可插入标题、列表、表格、代码块等</span>
    </div>

    <!-- 选中文本浮动工具条 -->
    <bubble-menu v-if="editor" :editor="editor" :should-show="shouldShowBubble" :tippy-options="{ duration: 100, maxWidth: 'none' }">
      <div class="bubble-bar">
        <button @click="editor.chain().focus().toggleBold().run()" :class="{ 'is-active': editor.isActive('bold') }">B</button>
        <button @click="editor.chain().focus().toggleItalic().run()" :class="{ 'is-active': editor.isActive('italic') }"><i>I</i></button>
        <button @click="editor.chain().focus().toggleUnderline().run()" :class="{ 'is-active': editor.isActive('underline') }"><u>U</u></button>
        <button @click="editor.chain().focus().toggleStrike().run()" :class="{ 'is-active': editor.isActive('strike') }"><s>S</s></button>
        <button @click="editor.chain().focus().toggleHighlight().run()" :class="{ 'is-active': editor.isActive('highlight') }">▨</button>
        <button @click="editor.chain().focus().toggleCode().run()" :class="{ 'is-active': editor.isActive('code') }">&lt;/&gt;</button>
        <button @click="setLink" :class="{ 'is-active': editor.isActive('link') }">🔗</button>
        <button @click="editor.chain().focus().unsetAllMarks().run()" title="清除格式">⌫</button>
      </div>
    </bubble-menu>

    <!-- “/” 斜杠插入菜单 -->
    <Teleport to="body">
      <div
        v-if="slash.open && slash.items.length"
        class="slash-menu"
        :style="{ top: slash.y + 'px', left: slash.x + 'px' }"
        @mousedown.prevent
      >
        <div
          v-for="(item, i) in slash.items"
          :key="item.title"
          class="slash-item"
          :class="{ active: i === slash.index }"
          @mouseenter="slash.index = i"
          @click="pick(i)"
        >
          <span class="slash-icon">{{ item.icon }}</span>
          <span class="slash-title">{{ item.title }}</span>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { watch, computed, reactive, onBeforeUnmount, nextTick } from 'vue'
import { useEditor, EditorContent, VueNodeViewRenderer, BubbleMenu } from '@tiptap/vue-3'
import { Extension } from '@tiptap/core'
import Suggestion from '@tiptap/suggestion'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Image from '@tiptap/extension-image'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import Link from '@tiptap/extension-link'
import Underline from '@tiptap/extension-underline'
import Highlight from '@tiptap/extension-highlight'
import TextAlign from '@tiptap/extension-text-align'
import Typography from '@tiptap/extension-typography'
import CharacterCount from '@tiptap/extension-character-count'
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plugin, PluginKey } from 'prosemirror-state'
import type { Editor } from '@tiptap/core'

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
// True while we are applying an external content load (note switch), so the
// resulting onUpdate is not emitted back as a user edit -> no phantom saves.
let applyingExternal = false

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
})

// ---------------- 斜杠(/)插入菜单 ----------------
interface SlashItem {
  title: string
  icon: string
  keywords: string[]
  action: (editor: Editor, range: { from: number; to: number }) => void
}

const slash = reactive({ open: false, items: [] as SlashItem[], index: 0, x: 0, y: 0 })
let slashCommand: ((item: SlashItem) => void) | null = null

const SLASH_ITEMS: SlashItem[] = [
  { title: '正文', icon: 'T', keywords: ['text', 'paragraph', '正文', '文本'], action: (e, r) => { e.chain().focus().deleteRange(r).setParagraph().run() } },
  { title: '标题 1', icon: 'H1', keywords: ['h1', 'heading', '标题', '一级'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleHeading({ level: 1 }).run() } },
  { title: '标题 2', icon: 'H2', keywords: ['h2', 'heading', '标题', '二级'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleHeading({ level: 2 }).run() } },
  { title: '标题 3', icon: 'H3', keywords: ['h3', 'heading', '标题', '三级'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleHeading({ level: 3 }).run() } },
  { title: '无序列表', icon: '•', keywords: ['bullet', 'list', '无序', '列表'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleBulletList().run() } },
  { title: '有序列表', icon: '1.', keywords: ['ordered', 'number', '有序', '列表', '编号'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleOrderedList().run() } },
  { title: '待办列表', icon: '☑', keywords: ['todo', 'task', '待办', '任务', '勾选'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleTaskList().run() } },
  { title: '引用', icon: '❝', keywords: ['quote', 'blockquote', '引用'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleBlockquote().run() } },
  { title: '代码块', icon: '</>', keywords: ['code', '代码'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleCodeBlock().run() } },
  { title: '表格', icon: '▦', keywords: ['table', '表格'], action: (e, r) => { e.chain().focus().deleteRange(r).insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run() } },
  { title: '分割线', icon: '―', keywords: ['hr', 'divider', '分割', '横线'], action: (e, r) => { e.chain().focus().deleteRange(r).setHorizontalRule().run() } },
  { title: '图片', icon: '🖼', keywords: ['image', 'img', '图片', '照片'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); handleImageUpload() } },
  { title: '图表', icon: '◈', keywords: ['mermaid', 'chart', 'diagram', '图表', '流程图'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); insertMermaid() } },
]

function filterSlash(query: string): SlashItem[] {
  const q = (query || '').trim().toLowerCase()
  if (!q) return SLASH_ITEMS
  return SLASH_ITEMS.filter(i =>
    i.title.toLowerCase().includes(q) || i.keywords.some(k => k.toLowerCase().includes(q))
  )
}

function pick(i: number) {
  const item = slash.items[i]
  if (item && slashCommand) slashCommand(item)
  slash.open = false
}

const SlashCommand = Extension.create({
  name: 'slashCommand',
  addOptions() {
    return { suggestion: { char: '/' } }
  },
  addProseMirrorPlugins() {
    return [
      Suggestion({
        editor: this.editor,
        char: '/',
        startOfLine: false,
        allowSpaces: false,
        items: ({ query }: { query: string }) => filterSlash(query),
        command: ({ editor, range, props: item }: any) => item.action(editor, range),
        render: () => ({
          onStart: (p: any) => {
            slash.items = p.items
            slash.index = 0
            slash.open = p.items.length > 0
            slashCommand = (item: SlashItem) => p.command(item)
            const rect = p.clientRect?.()
            if (rect) { slash.x = rect.left; slash.y = rect.bottom + 6 }
          },
          onUpdate: (p: any) => {
            slash.items = p.items
            slash.index = 0
            slash.open = p.items.length > 0
            slashCommand = (item: SlashItem) => p.command(item)
            const rect = p.clientRect?.()
            if (rect) { slash.x = rect.left; slash.y = rect.bottom + 6 }
          },
          onKeyDown: (p: any) => {
            if (!slash.open || !slash.items.length) return false
            if (p.event.key === 'ArrowDown') { slash.index = (slash.index + 1) % slash.items.length; return true }
            if (p.event.key === 'ArrowUp') { slash.index = (slash.index - 1 + slash.items.length) % slash.items.length; return true }
            if (p.event.key === 'Enter') { pick(slash.index); return true }
            if (p.event.key === 'Escape') { slash.open = false; return true }
            return false
          },
          onExit: () => { slash.open = false },
        }),
      }),
    ]
  },
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
      placeholder: '开始写笔记... 输入 “/” 插入内容块',
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
    TaskList,
    TaskItem.configure({ nested: true }),
    Link.configure({
      openOnClick: false,
      autolink: true,
      linkOnPaste: true,
      HTMLAttributes: { rel: 'noopener noreferrer nofollow', target: '_blank' },
    }),
    Underline,
    Highlight,
    TextAlign.configure({ types: ['heading', 'paragraph'] }),
    Typography,
    CharacterCount,
    SlashCommand,
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
    if (!applyingExternal) emit('update:modelValue', markdown)
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

const headingValue = computed(() => {
  const e = editor.value
  if (!e) return 'p'
  for (const level of [1, 2, 3] as const) {
    if (e.isActive('heading', { level })) return String(level)
  }
  return 'p'
})

const charCount = computed(() => editor.value?.storage.characterCount.characters() ?? 0)

function setHeading(ev: Event) {
  const value = (ev.target as HTMLSelectElement).value
  const e = editor.value
  if (!e) return
  if (value === 'p') e.chain().focus().setParagraph().run()
  else e.chain().focus().toggleHeading({ level: Number(value) as 1 | 2 | 3 }).run()
}

const shouldShowBubble = ({ editor: e, from, to }: any) => {
  if (from === to) return false
  if (e.isActive('codeBlock')) return false
  return true
}

async function setLink() {
  const e = editor.value
  if (!e) return
  const previous = e.getAttributes('link').href || ''
  let url: string | null = null
  try {
    const res = await ElMessageBox.prompt('输入链接地址（留空可移除链接）', '链接', {
      inputValue: previous || 'https://',
      confirmButtonText: '确定',
      cancelButtonText: '取消',
    })
    url = res.value
  } catch {
    return
  }
  if (url === null) return
  if (!url.trim()) {
    e.chain().focus().extendMarkRange('link').unsetLink().run()
    return
  }
  e.chain().focus().extendMarkRange('link').setLink({ href: url.trim() }).run()
}

watch(() => props.modelValue, (newValue) => {
  if (!editor.value || lastEmitted === newValue) return
  const apply = () => {
    if (!editor.value) return
    if (props.modelValue !== newValue) return // a newer note arrived meanwhile
    if (lastEmitted === newValue) return
    applyingExternal = true
    editor.value.commands.setContent(newValue || '')
    applyingExternal = false
    mermaidCache.clear()
    nextTick(() => {
      scheduleMermaid()
      disableSpellcheck()
    })
  }
  // Parsing large markdown is synchronous and can take tens of ms; defer it
  // to the next idle slot so switching notes never blocks the UI.
  if (typeof requestIdleCallback !== 'undefined') {
    requestIdleCallback(apply, { timeout: 300 })
  } else {
    window.setTimeout(apply, 0)
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

function handleImageUpload() {
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

function insertMermaid() {
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
  padding: 8px 10px;
  background: #f8f9fa;
  border: 1px solid #eef0f3;
  border-radius: 10px;
  margin-bottom: 18px;
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 5;
}

.editor-toolbar button {
  padding: 6px 9px;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: #374151;
  transition: all 0.15s;
  line-height: 1;
}

.editor-toolbar button:hover:not(:disabled) {
  background: #e5e7eb;
}

.editor-toolbar button.is-active {
  background: #3b82f6;
  color: #fff;
}

.editor-toolbar button:disabled {
  opacity: 0.35;
  cursor: default;
}

.tb-select {
  padding: 5px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fff;
  font-size: 13px;
  color: #374151;
  cursor: pointer;
}

.editor-toolbar .divider {
  width: 1px;
  height: 20px;
  background: #e5e7eb;
  margin: 0 6px;
}

.editor-status {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 4px 0;
  font-size: 12px;
  color: #9ca3af;
}

.editor-status .hint {
  color: #c0c4cc;
}

.editor-content {
  min-height: 400px;
}

.editor-content :deep(.ProseMirror) {
  outline: none;
  min-height: 400px;
  font-size: 16px;
  line-height: 1.75;
  color: #1f2937;
}

.editor-content :deep(.ProseMirror p) {
  margin: 10px 0;
}

.editor-content :deep(.ProseMirror h1) {
  font-size: 28px;
  font-weight: 700;
  margin: 26px 0 14px;
}

.editor-content :deep(.ProseMirror h2) {
  font-size: 22px;
  font-weight: 600;
  margin: 22px 0 12px;
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
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 0.9em;
  color: #e11d48;
}

.editor-content :deep(.ProseMirror mark) {
  background: #fef08a;
  padding: 1px 2px;
  border-radius: 3px;
}

.editor-content :deep(.ProseMirror a) {
  color: #2563eb;
  text-decoration: underline;
  cursor: pointer;
}

.editor-content :deep(.ProseMirror hr) {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 22px 0;
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

/* 待办列表 */
.editor-content :deep(.ProseMirror ul[data-type='taskList']) {
  list-style: none;
  padding-left: 4px;
}

.editor-content :deep(.ProseMirror ul[data-type='taskList'] li) {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.editor-content :deep(.ProseMirror ul[data-type='taskList'] li > label) {
  flex: 0 0 auto;
  margin-top: 4px;
  user-select: none;
}

.editor-content :deep(.ProseMirror ul[data-type='taskList'] li > div) {
  flex: 1 1 auto;
}

.editor-content :deep(.ProseMirror ul[data-type='taskList'] input[type='checkbox']) {
  width: 16px;
  height: 16px;
  cursor: pointer;
}

.editor-content :deep(.ProseMirror blockquote) {
  border-left: 4px solid #3b82f6;
  padding-left: 16px;
  color: #6b7280;
  margin: 14px 0;
}

.editor-content :deep(.ProseMirror img) {
  max-width: 100%;
  border-radius: 8px;
}

.editor-content :deep(.ProseMirror table) {
  border-collapse: collapse;
  margin: 16px 0;
  width: 100%;
  table-layout: fixed;
}

.editor-content :deep(.ProseMirror th),
.editor-content :deep(.ProseMirror td) {
  border: 1px solid #e5e7eb;
  padding: 8px 12px;
  min-width: 60px;
  vertical-align: top;
  position: relative;
}

.editor-content :deep(.ProseMirror th) {
  background: #f9fafb;
  font-weight: 600;
}

.editor-content :deep(.ProseMirror .selectedCell::after) {
  content: '';
  position: absolute;
  inset: 0;
  background: rgba(59, 130, 246, 0.12);
  pointer-events: none;
}

.editor-content :deep(.ProseMirror .column-resize-handle) {
  position: absolute;
  right: -2px;
  top: 0;
  bottom: 0;
  width: 4px;
  background: #3b82f6;
  pointer-events: none;
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

.editor-content :deep(.ProseMirror p.is-editor-empty:first-child::before) {
  content: attr(data-placeholder);
  float: left;
  color: #adb5bd;
  pointer-events: none;
  height: 0;
}
</style>

<style>
/* 浮动工具条与斜杠菜单需为全局样式(挂在 body 上,scoped 无法命中) */
.bubble-bar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 4px 6px;
  background: #1f2937;
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
}

.bubble-bar button {
  border: none;
  background: transparent;
  color: #e5e7eb;
  padding: 5px 8px;
  border-radius: 5px;
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
}

.bubble-bar button:hover {
  background: #374151;
}

.bubble-bar button.is-active {
  background: #3b82f6;
  color: #fff;
}

.slash-menu {
  position: fixed;
  z-index: 9999;
  width: 220px;
  max-height: 320px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.16);
  padding: 6px;
}

.slash-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 7px;
  cursor: pointer;
  color: #374151;
}

.slash-item.active {
  background: #eff6ff;
  color: #1d4ed8;
}

.slash-item .slash-icon {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #f3f4f6;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
}

.slash-item .slash-title {
  font-size: 14px;
}
</style>
