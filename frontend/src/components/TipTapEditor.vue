<template>
  <div class="tiptap-editor" :class="prefClasses" @mousemove="onEditorMouseMove" @mouseleave="onEditorMouseLeave">
    <!-- 工具栏 -->
    <div class="editor-toolbar" v-if="editor">
      <select class="tb-select" :value="headingValue" @change="setHeading" title="段落样式">
        <option value="p">正文</option>
        <option value="1">标题 1</option>
        <option value="2">标题 2</option>
        <option value="3">标题 3</option>
      </select>

      <span class="divider"></span>

      <button class="tb-btn" @click="editor.chain().focus().toggleBold().run()" :class="{ 'is-active': editor.isActive('bold') }" title="加粗 (Ctrl+B)"><Bold :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleItalic().run()" :class="{ 'is-active': editor.isActive('italic') }" title="斜体 (Ctrl+I)"><Italic :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleUnderline().run()" :class="{ 'is-active': editor.isActive('underline') }" title="下划线 (Ctrl+U)"><UnderlineIcon :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleStrike().run()" :class="{ 'is-active': editor.isActive('strike') }" title="删除线"><Strikethrough :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleHighlight().run()" :class="{ 'is-active': editor.isActive('highlight') }" title="高亮"><Highlighter :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleCode().run()" :class="{ 'is-active': editor.isActive('code') }" title="行内代码"><Code :size="16" /></button>
      <button class="tb-btn" @click="setLink" :class="{ 'is-active': editor.isActive('link') }" title="链接"><LinkIcon :size="16" /></button>

      <span class="divider"></span>

      <button class="tb-btn" @click="editor.chain().focus().toggleBulletList().run()" :class="{ 'is-active': editor.isActive('bulletList') }" title="无序列表"><List :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleOrderedList().run()" :class="{ 'is-active': editor.isActive('orderedList') }" title="有序列表"><ListOrdered :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleTaskList().run()" :class="{ 'is-active': editor.isActive('taskList') }" title="待办列表"><ListChecks :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleBlockquote().run()" :class="{ 'is-active': editor.isActive('blockquote') }" title="引用"><Quote :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().toggleCodeBlock().run()" :class="{ 'is-active': editor.isActive('codeBlock') }" title="代码块"><SquareCode :size="16" /></button>

      <span class="divider"></span>

      <button class="tb-btn" @click="editor.chain().focus().setTextAlign('left').run()" :class="{ 'is-active': editor.isActive({ textAlign: 'left' }) }" title="左对齐"><AlignLeft :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().setTextAlign('center').run()" :class="{ 'is-active': editor.isActive({ textAlign: 'center' }) }" title="居中"><AlignCenter :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().setTextAlign('right').run()" :class="{ 'is-active': editor.isActive({ textAlign: 'right' }) }" title="右对齐"><AlignRight :size="16" /></button>

      <span class="divider"></span>

      <button class="tb-btn" @click="handleImageUpload" title="插入图片"><ImageIcon :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()" title="插入表格"><TableIcon :size="16" /></button>
      <button class="tb-btn" @click="insertMermaid" title="插入图表"><Workflow :size="16" /></button>

      <span class="divider"></span>

      <el-dropdown trigger="click" @command="insertBlock">
        <button class="tb-btn" title="插入内容块"><Plus :size="16" /></button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="item in SLASH_ITEMS" :key="item.title" :command="item.title">
              <span class="dd-icon">{{ item.icon }}</span>{{ item.title }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>

      <span class="divider"></span>

      <button class="tb-btn" @click="editor.chain().focus().undo().run()" :disabled="!editor.can().undo()" title="撤销 (Ctrl+Z)"><Undo2 :size="16" /></button>
      <button class="tb-btn" @click="editor.chain().focus().redo().run()" :disabled="!editor.can().redo()" title="重做 (Ctrl+Shift+Z)"><Redo2 :size="16" /></button>

      <span class="tb-spacer"></span>

      <el-dropdown trigger="click" @command="applyPref">
        <button class="tb-btn" title="视图与字号"><Settings2 :size="16" /></button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="font-sm">字号 小</el-dropdown-item>
            <el-dropdown-item command="font-md">字号 标准</el-dropdown-item>
            <el-dropdown-item command="font-lg">字号 大</el-dropdown-item>
            <el-dropdown-item command="focus" divided>专注宽度：{{ prefs.focus ? '开' : '关' }}</el-dropdown-item>
            <el-dropdown-item command="typewriter">打字机模式：{{ prefs.typewriter ? '开' : '关' }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <button class="tb-btn" @click="helpOpen = true" title="快捷键"><Keyboard :size="16" /></button>
    </div>

    <!-- 表格上下文工具条 -->
    <div class="table-bar" v-if="editor && isInTable">
      <span class="table-label"><TableIcon :size="14" /> 表格</span>
      <button class="tb-btn sm" @click="editor.chain().focus().addRowBefore().run()">上方行</button>
      <button class="tb-btn sm" @click="editor.chain().focus().addRowAfter().run()">下方行</button>
      <button class="tb-btn sm" @click="editor.chain().focus().addColumnBefore().run()">左侧列</button>
      <button class="tb-btn sm" @click="editor.chain().focus().addColumnAfter().run()">右侧列</button>
      <span class="divider"></span>
      <button class="tb-btn sm" @click="editor.chain().focus().toggleHeaderRow().run()">表头行</button>
      <button class="tb-btn sm" @click="editor.chain().focus().mergeCells().run()">合并</button>
      <button class="tb-btn sm" @click="editor.chain().focus().splitCell().run()">拆分</button>
      <button class="tb-btn sm danger" @click="editor.chain().focus().deleteRow().run()">删行</button>
      <button class="tb-btn sm danger" @click="editor.chain().focus().deleteColumn().run()">删列</button>
      <button class="tb-btn sm danger" @click="editor.chain().focus().deleteTable().run()">删表</button>
    </div>

    <editor-content :editor="editor" class="editor-content" />

    <div v-if="editor" class="editor-status">
      <span>{{ charCount }} 字</span>
      <span class="hint">输入 “/” 插入标题、列表、表格、代码块等 · 拖动左侧 ⋮⋮ 调整块</span>
    </div>

    <!-- 块操作手柄 -->
    <div
      v-if="editor && handle.visible"
      class="block-handle"
      :style="{ top: handle.y + 'px', left: (handle.x - 34) + 'px' }"
      @mouseenter="handle.visible = true"
    >
      <button
        class="handle-btn"
        title="拖动排序 / 点击菜单"
        draggable="true"
        @click.stop="toggleBlockMenu"
        @dragstart="onHandleDragStart"
        @dragend="onHandleDragEnd"
      >
        <GripVertical :size="16" />
      </button>
      <div
        v-if="blockMenu.open"
        class="block-menu"
        :style="{ top: (handle.y + 26) + 'px', left: (handle.x - 30) + 'px' }"
        @mouseleave="blockMenu.open = false"
      >
        <button class="bm-item" @click="moveBlock('up')"><ArrowUp :size="15" /> 上移</button>
        <button class="bm-item" @click="moveBlock('down')"><ArrowDown :size="15" /> 下移</button>
        <button class="bm-item" @click="duplicateBlock"><Copy :size="15" /> 复制</button>
        <button class="bm-item" @click="copyBlockMarkdown"><FileText :size="15" /> 复制 Markdown</button>
        <button class="bm-item" @click="insertParagraphAfter"><BetweenHorizontalEnd :size="15" /> 在下方加段落</button>
        <div class="bm-sep"></div>
        <div class="bm-title">转换为</div>
        <button class="bm-item" @click="changeBlock('p')"><Pilcrow :size="15" /> 正文</button>
        <button class="bm-item" @click="changeBlock('1')"><Heading1 :size="15" /> 标题 1</button>
        <button class="bm-item" @click="changeBlock('2')"><Heading2 :size="15" /> 标题 2</button>
        <button class="bm-item" @click="changeBlock('3')"><Heading3 :size="15" /> 标题 3</button>
        <button class="bm-item" @click="changeBlock('bullet')"><List :size="15" /> 无序列表</button>
        <button class="bm-item" @click="changeBlock('ordered')"><ListOrdered :size="15" /> 有序列表</button>
        <button class="bm-item" @click="changeBlock('task')"><ListChecks :size="15" /> 待办列表</button>
        <button class="bm-item" @click="changeBlock('quote')"><Quote :size="15" /> 引用</button>
        <div class="bm-sep"></div>
        <button class="bm-item danger" @click="deleteBlock"><Trash2 :size="15" /> 删除</button>
      </div>
    </div>

    <!-- 选中文本浮动工具条 -->
    <bubble-menu v-if="editor" :editor="editor" :should-show="shouldShowBubble" :tippy-options="{ duration: 100, maxWidth: 'none' }">
      <div class="bubble-bar">
        <button @click="editor.chain().focus().toggleBold().run()" :class="{ 'is-active': editor.isActive('bold') }" title="加粗"><Bold :size="15" /></button>
        <button @click="editor.chain().focus().toggleItalic().run()" :class="{ 'is-active': editor.isActive('italic') }" title="斜体"><Italic :size="15" /></button>
        <button @click="editor.chain().focus().toggleUnderline().run()" :class="{ 'is-active': editor.isActive('underline') }" title="下划线"><UnderlineIcon :size="15" /></button>
        <button @click="editor.chain().focus().toggleStrike().run()" :class="{ 'is-active': editor.isActive('strike') }" title="删除线"><Strikethrough :size="15" /></button>
        <button @click="editor.chain().focus().toggleHighlight().run()" :class="{ 'is-active': editor.isActive('highlight') }" title="高亮"><Highlighter :size="15" /></button>
        <button @click="editor.chain().focus().toggleCode().run()" :class="{ 'is-active': editor.isActive('code') }" title="行内代码"><Code :size="15" /></button>
        <button @click="setLink" :class="{ 'is-active': editor.isActive('link') }" title="链接"><LinkIcon :size="15" /></button>
        <span class="bubble-sep"></span>
        <button @click="editor.chain().focus().unsetAllMarks().run()" title="清除格式"><Eraser :size="15" /></button>
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
        <div class="slash-header">插入内容块</div>
        <div
          v-for="(item, i) in slash.items"
          :key="item.title"
          class="slash-item"
          :class="{ active: i === slash.index }"
          @mouseenter="slash.index = i"
          @click="pick(i)"
        >
          <span class="slash-icon">{{ item.icon }}</span>
          <span class="slash-text">
            <span class="slash-title">{{ item.title }}</span>
            <span class="slash-desc">{{ item.desc }}</span>
          </span>
        </div>
      </div>
    </Teleport>

    <!-- 拖拽落点指示线 -->
    <div v-if="drag.active && drag.toIndex >= 0" class="drop-indicator" :style="dragIndicatorStyle"></div>

    <!-- 快捷键说明 -->
    <el-dialog v-model="helpOpen" title="快捷键" width="480px" append-to-body>
      <div class="shortcut-list">
        <div v-for="s in SHORTCUTS" :key="s.label" class="shortcut-row">
          <span class="sc-label">{{ s.label }}</span>
          <span class="sc-keys"><kbd v-for="k in s.keys" :key="k">{{ k }}</kbd></span>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { watch, ref, computed, reactive, onMounted, onBeforeUnmount, nextTick } from 'vue'
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
import {
  Bold, Italic, Underline as UnderlineIcon, Strikethrough, Highlighter, Code, Link as LinkIcon,
  List, ListOrdered, ListChecks, Quote, SquareCode, AlignLeft, AlignCenter, AlignRight,
  Image as ImageIcon, Table as TableIcon, Workflow, Undo2, Redo2, Plus, Trash2, Copy,
  ArrowUp, ArrowDown, Pilcrow, GripVertical, Heading1, Heading2, Heading3, Eraser,
  Settings2, Keyboard, FileText, BetweenHorizontalEnd,
} from 'lucide-vue-next'

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

let lastEmitted = props.modelValue
let applyingExternal = false

mermaid.initialize({ startOnLoad: false, theme: 'default' })

// ---------------- 斜杠(/)插入菜单 ----------------
interface SlashItem {
  title: string
  desc: string
  icon: string
  keywords: string[]
  action: (editor: Editor, range: { from: number; to: number }) => void
}

const slash = reactive({ open: false, items: [] as SlashItem[], index: 0, x: 0, y: 0 })
let slashCommand: ((item: SlashItem) => void) | null = null

const SLASH_ITEMS: SlashItem[] = [
  { title: '正文', desc: '普通文本段落', icon: '¶', keywords: ['text', 'paragraph', '正文', '文本'], action: (e, r) => { e.chain().focus().deleteRange(r).setParagraph().run() } },
  { title: '标题 1', desc: '大号章节标题', icon: 'H1', keywords: ['h1', 'heading', '标题', '一级'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleHeading({ level: 1 }).run() } },
  { title: '标题 2', desc: '中号章节标题', icon: 'H2', keywords: ['h2', 'heading', '标题', '二级'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleHeading({ level: 2 }).run() } },
  { title: '标题 3', desc: '小号章节标题', icon: 'H3', keywords: ['h3', 'heading', '标题', '三级'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleHeading({ level: 3 }).run() } },
  { title: '无序列表', desc: '项目符号列表', icon: '•', keywords: ['bullet', 'list', '无序', '列表'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleBulletList().run() } },
  { title: '有序列表', desc: '带编号的列表', icon: '1.', keywords: ['ordered', 'number', '有序', '列表', '编号'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleOrderedList().run() } },
  { title: '待办列表', desc: '可勾选的任务清单', icon: '☑', keywords: ['todo', 'task', '待办', '任务', '勾选'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleTaskList().run() } },
  { title: '引用', desc: '引用段落', icon: '❝', keywords: ['quote', 'blockquote', '引用'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleBlockquote().run() } },
  { title: '代码块', desc: '带语法高亮的代码', icon: '</>', keywords: ['code', '代码'], action: (e, r) => { e.chain().focus().deleteRange(r).toggleCodeBlock().run() } },
  { title: '表格', desc: '3×3 表格', icon: '▦', keywords: ['table', '表格'], action: (e, r) => { e.chain().focus().deleteRange(r).insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run() } },
  { title: '分割线', desc: '水平分隔线', icon: '―', keywords: ['hr', 'divider', '分割', '横线'], action: (e, r) => { e.chain().focus().deleteRange(r).setHorizontalRule().run() } },
  { title: '图片', desc: '上传或插入图片', icon: '▧', keywords: ['image', 'img', '图片', '照片'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); handleImageUpload() } },
  { title: '图表', desc: 'Mermaid 流程图/时序图', icon: '◈', keywords: ['mermaid', 'chart', 'diagram', '图表', '流程图'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); insertMermaid() } },
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
    StarterKit.configure({ codeBlock: false }),
    CodeBlockLowlight
      .extend({ addNodeView() { return VueNodeViewRenderer(CodeBlockComponent) } })
      .configure({ lowlight, defaultLanguage: 'plaintext' }),
    Placeholder.configure({ placeholder: '开始写笔记... 输入 “/” 插入内容块' }),
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
    }).configure({ inline: true, allowBase64: true }),
    Table.configure({ resizable: true }),
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
    Markdown.configure({ html: true, breaks: true, linkify: true }),
  ],
  content: props.modelValue,
  onUpdate: ({ editor }) => {
    const markdown = editor.storage.markdown.getMarkdown()
    lastEmitted = markdown
    if (!applyingExternal) emit('update:modelValue', markdown)
    nextTick(() => { scheduleMermaid(); disableSpellcheck() })
  },
  onCreate: () => {
    nextTick(() => { scheduleMermaid(); disableSpellcheck() })
  },
  onSelectionUpdate: () => {
    keepCaretCentered()
  },
  editorProps: {
    handleKeyDown: (_view, event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setLink()
        return true
      }
      return false
    },
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
const isInTable = computed(() => editor.value?.isActive('table') ?? false)

function setHeading(ev: Event) {
  const value = (ev.target as HTMLSelectElement).value
  const e = editor.value
  if (!e) return
  if (value === 'p') e.chain().focus().setParagraph().run()
  else e.chain().focus().toggleHeading({ level: Number(value) as 1 | 2 | 3 }).run()
}

function insertBlock(title: string) {
  const item = SLASH_ITEMS.find(i => i.title === title)
  const e = editor.value
  if (!item || !e) return
  const { from, to } = e.state.selection
  item.action(e, { from, to })
}

// ---------------- 块操作手柄 ----------------
const handle = reactive({ visible: false, x: 0, y: 0, pos: 0 })
const blockMenu = reactive({ open: false })

let leaveTimer: number | null = null

function onEditorMouseMove(ev: MouseEvent) {
  const target = ev.target as HTMLElement
  if (target.closest('.block-handle') || target.closest('.block-menu')) {
    if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null }
    return
  }
  if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null }
  const e = editor.value
  if (!e) return
  const el = target.closest('.ProseMirror > *') as HTMLElement | null
  if (!el || !el.parentElement || !el.parentElement.classList.contains('ProseMirror')) {
    handle.visible = false
    blockMenu.open = false
    return
  }
  const rect = el.getBoundingClientRect()
  handle.x = rect.left
  handle.y = rect.top
  try {
    handle.pos = e.view.posAtDOM(el, 0)
  } catch {
    handle.visible = false
    return
  }
  handle.visible = true
}

function onEditorMouseLeave() {
  if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null }
  leaveTimer = window.setTimeout(() => {
    leaveTimer = null
    if (!blockMenu.open) handle.visible = false
  }, 200)
}

function toggleBlockMenu() {
  blockMenu.open = !blockMenu.open
}

interface BlockRange { start: number; end: number; index: number; node: any }

function topRange(pos: number): BlockRange | null {
  const e = editor.value
  if (!e) return null
  const doc = e.state.doc
  const $pos = doc.resolve(Math.max(0, Math.min(pos, doc.content.size)))
  if ($pos.depth === 0) return null
  const start = $pos.before(1)
  const end = $pos.after(1)
  const index = $pos.index(0)
  const node = doc.child(index)
  return { start, end, index, node }
}

function moveBlock(dir: 'up' | 'down') {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  const doc = e.state.doc
  const tr = e.state.tr
  if (dir === 'up') {
    if (r.index === 0) return
    const prev = doc.child(r.index - 1)
    tr.delete(r.start, r.end)
    tr.insert(r.start - prev.nodeSize, r.node)
  } else {
    if (r.index >= doc.childCount - 1) return
    const next = doc.child(r.index + 1)
    tr.delete(r.start, r.end)
    tr.insert(r.start + next.nodeSize, r.node)
  }
  e.view.dispatch(tr)
  blockMenu.open = false
}

function duplicateBlock() {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  e.view.dispatch(e.state.tr.insert(r.end, r.node))
  blockMenu.open = false
}

function deleteBlock() {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  e.view.dispatch(e.state.tr.delete(r.start, r.end))
  blockMenu.open = false
  handle.visible = false
}

async function copyBlockMarkdown() {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  let text = ''
  try {
    text = e.storage.markdown.serializer.serialize(r.node)
  } catch {
    text = r.node.textContent || ''
  }
  await copyText(text.trim())
  ElMessage.success('已复制 Markdown')
  blockMenu.open = false
}

function insertParagraphAfter() {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  const para = e.schema.nodes.paragraph.create()
  const tr = e.state.tr.insert(r.end, para)
  e.view.dispatch(tr)
  e.commands.focus(r.end + 1)
  blockMenu.open = false
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

// ---------------- 拖拽排序 ----------------
const drag = reactive({ active: false, fromIndex: -1, toIndex: -1, indicatorY: 0, indicatorX: 0, indicatorW: 0 })
let dragNode: any = null
let dragStart = 0
let dragEnd = 0

const dragIndicatorStyle = computed(() => ({
  top: drag.indicatorY + 'px',
  left: drag.indicatorX + 'px',
  width: drag.indicatorW + 'px',
}))

function onHandleDragStart(ev: DragEvent) {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) { ev.preventDefault?.(); return }
  drag.active = true
  drag.fromIndex = r.index
  dragNode = r.node
  dragStart = r.start
  dragEnd = r.end
  drag.toIndex = -1
  if (ev.dataTransfer) {
    ev.dataTransfer.effectAllowed = 'move'
    ev.dataTransfer.setData('text/plain', r.node.textContent || '')
  }
}

function onHandleDragEnd() {
  drag.active = false
  drag.toIndex = -1
}

function onDocDragOver(ev: DragEvent) {
  const e = editor.value
  if (!drag.active || !e) return
  const pm = e.view.dom as HTMLElement
  const pmRect = pm.getBoundingClientRect()
  if (ev.clientY < pmRect.top - 4 || ev.clientY > pmRect.bottom + 4) {
    drag.toIndex = -1
    return
  }
  ev.preventDefault()
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move'
  const children = Array.from(pm.children) as HTMLElement[]
  let idx = children.length
  for (let i = 0; i < children.length; i++) {
    const r = children[i].getBoundingClientRect()
    if (ev.clientY < r.top + r.height / 2) { idx = i; break }
  }
  drag.toIndex = idx
  drag.indicatorX = pmRect.left
  drag.indicatorW = pmRect.width
  if (idx < children.length) {
    drag.indicatorY = children[idx].getBoundingClientRect().top - 2
  } else if (children.length) {
    drag.indicatorY = children[children.length - 1].getBoundingClientRect().bottom - 2
  }
}

function onDocDrop(ev: DragEvent) {
  const e = editor.value
  if (!drag.active || !e || drag.toIndex < 0 || !dragNode) {
    onHandleDragEnd()
    return
  }
  ev.preventDefault()
  const from = drag.fromIndex
  const to = drag.toIndex
  drag.active = false
  const doc = e.state.doc
  if (to === from || to === from + 1) { drag.toIndex = -1; return }
  const tr = e.state.tr
  tr.delete(dragStart, dragEnd)
  let insertPos = 0
  for (let i = 0; i < to; i++) insertPos += doc.child(i).nodeSize
  if (to > from) insertPos -= dragNode.nodeSize
  tr.insert(insertPos, dragNode)
  e.view.dispatch(tr)
  drag.toIndex = -1
}

// ---------------- 视图偏好 ----------------
interface Prefs { font: 'sm' | 'md' | 'lg'; focus: boolean; typewriter: boolean }
const prefs = reactive<Prefs>(loadPrefs())

function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem('rag-editor-prefs')
    if (raw) return { font: 'md', focus: false, typewriter: false, ...JSON.parse(raw) }
  } catch { /* ignore */ }
  return { font: 'md', focus: false, typewriter: false }
}

function savePrefs() {
  try { localStorage.setItem('rag-editor-prefs', JSON.stringify(prefs)) } catch { /* ignore */ }
}

const prefClasses = computed(() => [
  `pref-font-${prefs.font}`,
  prefs.focus ? 'pref-focus' : '',
  prefs.typewriter ? 'pref-typewriter' : '',
].filter(Boolean))

function applyPref(cmd: string) {
  if (cmd === 'font-sm') prefs.font = 'sm'
  else if (cmd === 'font-md') prefs.font = 'md'
  else if (cmd === 'font-lg') prefs.font = 'lg'
  else if (cmd === 'focus') prefs.focus = !prefs.focus
  else if (cmd === 'typewriter') prefs.typewriter = !prefs.typewriter
  savePrefs()
}

function keepCaretCentered() {
  if (!prefs.typewriter || !editor.value) return
  const view = editor.value.view
  const pos = view.state.selection.head
  try {
    const coords = view.coordsAtPos(pos)
    window.scrollTo({ top: window.scrollY + coords.top - window.innerHeight / 2, behavior: 'smooth' })
  } catch { /* ignore */ }
}

// ---------------- 快捷键说明 ----------------
const helpOpen = ref(false)
const SHORTCUTS = [
  { label: '加粗', keys: ['Ctrl', 'B'] },
  { label: '斜体', keys: ['Ctrl', 'I'] },
  { label: '下划线', keys: ['Ctrl', 'U'] },
  { label: '行内代码', keys: ['Ctrl', 'E'] },
  { label: '标题 1/2/3', keys: ['Ctrl', 'Alt', '1/2/3'] },
  { label: '无序列表', keys: ['Ctrl', 'Shift', '8'] },
  { label: '有序列表', keys: ['Ctrl', 'Shift', '7'] },
  { label: '引用', keys: ['Ctrl', 'Shift', 'B'] },
  { label: '代码块', keys: ['Ctrl', 'Alt', 'C'] },
  { label: '插入链接', keys: ['Ctrl', 'K'] },
  { label: '撤销 / 重做', keys: ['Ctrl', 'Z / Y'] },
  { label: '插入内容块', keys: ['/'] },
]

function changeBlock(kind: string) {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  const chain = e.chain().focus().setTextSelection({ from: r.start + 1, to: Math.max(r.start + 1, r.end - 1) })
  if (kind === 'p') chain.setParagraph().run()
  else if (kind === '1' || kind === '2' || kind === '3') chain.setHeading({ level: Number(kind) as 1 | 2 | 3 }).run()
  else if (kind === 'bullet') chain.toggleBulletList().run()
  else if (kind === 'ordered') chain.toggleOrderedList().run()
  else if (kind === 'task') chain.toggleTaskList().run()
  else if (kind === 'quote') chain.toggleBlockquote().run()
  blockMenu.open = false
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
    if (props.modelValue !== newValue) return
    if (lastEmitted === newValue) return
    applyingExternal = true
    editor.value.commands.setContent(newValue || '')
    applyingExternal = false
    mermaidCache.clear()
    nextTick(() => { scheduleMermaid(); disableSpellcheck() })
  }
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

onMounted(() => {
  document.addEventListener('dragover', onDocDragOver)
  document.addEventListener('drop', onDocDrop)
})

onBeforeUnmount(() => {
  document.removeEventListener('dragover', onDocDragOver)
  document.removeEventListener('drop', onDocDrop)
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
  position: relative;
}

.editor-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px 8px;
  background: rgba(248, 249, 251, 0.9);
  backdrop-filter: blur(6px);
  border: 1px solid #eceef2;
  border-radius: 12px;
  margin-bottom: 18px;
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 20;
}

.tb-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 6px;
  min-width: 30px;
  height: 30px;
  border: none;
  background: transparent;
  border-radius: 7px;
  cursor: pointer;
  color: #4b5563;
  transition: background 0.15s, color 0.15s;
  line-height: 1;
}

.tb-btn.sm {
  font-size: 12px;
  padding: 4px 8px;
  height: 26px;
  min-width: auto;
  color: #4b5563;
  background: #fff;
  border: 1px solid #e5e7eb;
}

.tb-btn:hover:not(:disabled) {
  background: #e9ecf1;
  color: #111827;
}

.tb-btn.sm:hover:not(:disabled) {
  background: #f3f4f6;
}

.tb-btn.is-active {
  background: #e0edff;
  color: #1d4ed8;
}

.tb-btn:disabled {
  opacity: 0.35;
  cursor: default;
}

.tb-btn.danger {
  color: #dc2626;
  border-color: #fecaca;
}

.tb-btn.danger:hover:not(:disabled) {
  background: #fef2f2;
}

.tb-select {
  padding: 5px 8px;
  height: 30px;
  border: 1px solid #e5e7eb;
  border-radius: 7px;
  background: #fff;
  font-size: 13px;
  color: #374151;
  cursor: pointer;
  outline: none;
}

.tb-spacer {
  flex: 1 1 auto;
}

.drop-indicator {
  position: fixed;
  z-index: 25;
  height: 2px;
  background: #3b82f6;
  border-radius: 2px;
  pointer-events: none;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.18);
}

.shortcut-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.shortcut-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 4px;
  border-bottom: 1px solid #f3f4f6;
  font-size: 13px;
  color: #374151;
}

.sc-keys kbd {
  display: inline-block;
  padding: 2px 7px;
  margin-left: 4px;
  background: #f3f4f6;
  border: 1px solid #e5e7eb;
  border-bottom-width: 2px;
  border-radius: 5px;
  font-family: inherit;
  font-size: 12px;
  color: #4b5563;
}

/* 视图偏好 */
.pref-font-sm :deep(.ProseMirror) { font-size: 14px; }
.pref-font-lg :deep(.ProseMirror) { font-size: 18px; }
.pref-focus :deep(.ProseMirror) {
  max-width: 720px;
  margin-left: auto;
  margin-right: auto;
}
.pref-typewriter :deep(.ProseMirror) { padding-bottom: 40vh; }

.editor-toolbar .divider {
  width: 1px;
  height: 18px;
  background: #e5e7eb;
  margin: 0 5px;
}

.table-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  padding: 6px 10px;
  margin: -8px 0 16px;
  background: #f0f7ff;
  border: 1px solid #d6e6ff;
  border-radius: 10px;
}

.table-bar .table-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #2563eb;
  font-weight: 600;
  margin-right: 6px;
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
  line-height: 1.78;
  color: #1f2937;
  caret-color: #2563eb;
}

.editor-content :deep(.ProseMirror ::selection) {
  background: #dbeafe;
}

.editor-content :deep(.ProseMirror > * + *) {
  margin-top: 0.55em;
}

.editor-content :deep(.ProseMirror p) {
  margin: 0.4em 0;
}

.editor-content :deep(.ProseMirror h1) {
  font-size: 30px;
  font-weight: 700;
  line-height: 1.3;
  margin: 1.1em 0 0.4em;
  letter-spacing: -0.01em;
}

.editor-content :deep(.ProseMirror h2) {
  font-size: 23px;
  font-weight: 650;
  margin: 1em 0 0.35em;
}

.editor-content :deep(.ProseMirror h3) {
  font-size: 19px;
  font-weight: 600;
  margin: 0.9em 0 0.3em;
}

.editor-content :deep(.ProseMirror code) {
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 5px;
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 0.88em;
  color: #db2777;
}

.editor-content :deep(.ProseMirror mark) {
  background: #fef08a;
  padding: 1px 2px;
  border-radius: 3px;
}

.editor-content :deep(.ProseMirror a) {
  color: #2563eb;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}

.editor-content :deep(.ProseMirror hr) {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 1.4em 0;
}

.editor-content :deep(.ProseMirror pre) {
  background: #282c34;
  color: #abb2bf;
  padding: 16px 20px;
  border-radius: 10px;
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
.editor-content :deep(.ProseMirror pre code .hljs-quote) { color: #5c6370; font-style: italic; }
.editor-content :deep(.ProseMirror pre code .hljs-doctag),
.editor-content :deep(.ProseMirror pre code .hljs-keyword),
.editor-content :deep(.ProseMirror pre code .hljs-formula) { color: #c678dd; }
.editor-content :deep(.ProseMirror pre code .hljs-section),
.editor-content :deep(.ProseMirror pre code .hljs-name),
.editor-content :deep(.ProseMirror pre code .hljs-tag),
.editor-content :deep(.ProseMirror pre code .hljs-selector-tag),
.editor-content :deep(.ProseMirror pre code .hljs-deletion),
.editor-content :deep(.ProseMirror pre code .hljs-subst) { color: #e06c75; }
.editor-content :deep(.ProseMirror pre code .hljs-literal) { color: #56b6c2; }
.editor-content :deep(.ProseMirror pre code .hljs-string),
.editor-content :deep(.ProseMirror pre code .hljs-regexp),
.editor-content :deep(.ProseMirror pre code .hljs-addition),
.editor-content :deep(.ProseMirror pre code .hljs-attribute),
.editor-content :deep(.ProseMirror pre code .hljs-meta .hljs-string) { color: #98c379; }
.editor-content :deep(.ProseMirror pre code .hljs-attr),
.editor-content :deep(.ProseMirror pre code .hljs-variable),
.editor-content :deep(.ProseMirror pre code .hljs-template-variable),
.editor-content :deep(.ProseMirror pre code .hljs-type),
.editor-content :deep(.ProseMirror pre code .hljs-selector-class),
.editor-content :deep(.ProseMirror pre code .hljs-selector-attr),
.editor-content :deep(.ProseMirror pre code .hljs-selector-pseudo),
.editor-content :deep(.ProseMirror pre code .hljs-number) { color: #d19a66; }
.editor-content :deep(.ProseMirror pre code .hljs-symbol),
.editor-content :deep(.ProseMirror pre code .hljs-bullet),
.editor-content :deep(.ProseMirror pre code .hljs-link),
.editor-content :deep(.ProseMirror pre code .hljs-meta),
.editor-content :deep(.ProseMirror pre code .hljs-selector-id),
.editor-content :deep(.ProseMirror pre code .hljs-title) { color: #61afef; }
.editor-content :deep(.ProseMirror pre code .hljs-built_in),
.editor-content :deep(.ProseMirror pre code .hljs-title.class_),
.editor-content :deep(.ProseMirror pre code .hljs-class .hljs-title) { color: #e6c07b; }
.editor-content :deep(.ProseMirror pre code .hljs-emphasis) { font-style: italic; }
.editor-content :deep(.ProseMirror pre code .hljs-strong) { font-weight: bold; }

.editor-content :deep(.ProseMirror ul),
.editor-content :deep(.ProseMirror ol) {
  padding-left: 24px;
}

.editor-content :deep(.ProseMirror li + li) {
  margin-top: 3px;
}

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
  accent-color: #2563eb;
}

.editor-content :deep(.ProseMirror blockquote) {
  border-left: 3px solid #93c5fd;
  padding: 2px 0 2px 14px;
  color: #4b5563;
  margin: 14px 0;
  background: #f8fafc;
  border-radius: 0 6px 6px 0;
}

.editor-content :deep(.ProseMirror img) {
  max-width: 100%;
  border-radius: 10px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.06);
}

.editor-content :deep(.ProseMirror table) {
  border-collapse: collapse;
  margin: 16px 0;
  width: 100%;
  table-layout: fixed;
  overflow: hidden;
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
  background: #f8fafc;
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
  border-radius: 10px;
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
  color: #b6bcc6;
  pointer-events: none;
  height: 0;
}
</style>

<style>
/* 浮动工具条、斜杠菜单、块手柄需全局样式(挂载在 body / 定位到视口) */
.bubble-bar {
  display: flex;
  align-items: center;
  gap: 1px;
  padding: 3px 4px;
  background: #1f2937;
  border-radius: 9px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
}

.bubble-bar button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: #e5e7eb;
  padding: 5px 6px;
  border-radius: 6px;
  cursor: pointer;
  line-height: 1;
}

.bubble-bar button:hover { background: #374151; }
.bubble-bar button.is-active { background: #3b82f6; color: #fff; }
.bubble-bar .bubble-sep { width: 1px; height: 16px; background: #4b5563; margin: 0 3px; }

.slash-menu {
  position: fixed;
  z-index: 9999;
  width: 260px;
  max-height: 340px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 12px;
  box-shadow: 0 16px 40px rgba(15, 23, 42, 0.18);
  padding: 6px;
}

.slash-header {
  font-size: 11px;
  color: #9ca3af;
  padding: 6px 10px 4px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.slash-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  border-radius: 8px;
  cursor: pointer;
  color: #374151;
}

.slash-item.active { background: #eff6ff; }

.slash-item .slash-icon {
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #f3f4f6;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 600;
  color: #4b5563;
}

.slash-item.active .slash-icon { background: #dbeafe; color: #1d4ed8; }

.slash-item .slash-text { display: flex; flex-direction: column; min-width: 0; }
.slash-item .slash-title { font-size: 14px; line-height: 1.3; }
.slash-item .slash-desc { font-size: 11px; color: #9ca3af; line-height: 1.3; }

.dd-icon { display: inline-block; width: 20px; }

.block-handle {
  position: fixed;
  z-index: 30;
  display: flex;
  align-items: flex-start;
}

.block-handle .handle-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: #cbd0d8;
  border-radius: 6px;
  cursor: grab;
}

.block-handle .handle-btn:hover { background: #eef1f5; color: #6b7280; }
.block-handle .handle-btn:active { cursor: grabbing; }

.block-menu {
  position: fixed;
  z-index: 40;
  min-width: 168px;
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 10px;
  box-shadow: 0 14px 36px rgba(15, 23, 42, 0.16);
  padding: 5px;
}

.block-menu .bm-title {
  font-size: 11px;
  color: #9ca3af;
  padding: 5px 10px 2px;
}

.block-menu .bm-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 7px 10px;
  border: none;
  background: transparent;
  border-radius: 7px;
  cursor: pointer;
  color: #374151;
  font-size: 13px;
  text-align: left;
}

.block-menu .bm-item:hover { background: #f3f4f6; }
.block-menu .bm-item.danger { color: #dc2626; }
.block-menu .bm-item.danger:hover { background: #fef2f2; }
.block-menu .bm-sep { height: 1px; background: #f0f1f4; margin: 4px 0; }
</style>
