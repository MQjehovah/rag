<template>
  <div class="tiptap-editor" :class="prefClasses" @mousemove="onEditorMouseMove" @mouseleave="onEditorMouseLeave" @mouseover="onEditorMouseOver" @mousedown="onEditorMouseDown" @contextmenu="onTableContextMenu">
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
      <button class="tb-btn" @click="handleAttachmentUpload" title="插入附件"><Paperclip :size="16" /></button>
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

      <button class="tb-btn" @click="openFind" :class="{ 'is-active': find.open }" title="查找/替换 (Ctrl+F)"><Search :size="16" /></button>

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

    <!-- 页内查找/替换 -->
    <div v-if="find.open" class="find-wrap">
      <div class="find-bar" @keydown.esc.stop.prevent="closeFind">
        <input
          ref="findInputEl"
          v-model="find.query"
          class="find-input"
          placeholder="查找"
          @input="onFindInput"
          @keydown.enter.exact.prevent="nextMatch"
          @keydown.shift.enter.prevent="prevMatch"
        />
        <input
          v-model="find.replacement"
          class="find-input"
          placeholder="替换为"
          @keydown.enter.prevent="replaceCurrent"
        />
        <span class="find-count">{{ findCountText }}</span>
        <button class="find-btn" :disabled="!find.matches.length" title="上一个 (Shift+Enter)" @click="prevMatch"><ChevronUp :size="15" /></button>
        <button class="find-btn" :disabled="!find.matches.length" title="下一个 (Enter)" @click="nextMatch"><ChevronDown :size="15" /></button>
        <button class="find-btn" :disabled="!find.query.trim() || !find.matches.length" @click="replaceCurrent">替换</button>
        <button class="find-btn" :disabled="!find.query.trim() || !find.matches.length" @click="replaceAll">全部替换</button>
        <button class="find-btn" title="关闭 (Esc)" @click="closeFind"><X :size="15" /></button>
      </div>
    </div>

    <!-- 表格上下文工具条 -->
    <div class="table-bar" v-if="editor && isInTable">
      <span class="table-label"><TableIcon :size="14" /> 表格</span>
      <button class="tb-btn sm" @click="tableCommand('addRowBefore')">上方行</button>
      <button class="tb-btn sm" @click="tableCommand('addRowAfter')">下方行</button>
      <button class="tb-btn sm" @click="tableCommand('addColumnBefore')">左侧列</button>
      <button class="tb-btn sm" @click="tableCommand('addColumnAfter')">右侧列</button>
      <span class="divider"></span>
      <button class="tb-btn sm" @click="tableCommand('toggleHeaderRow')">表头行</button>
      <button class="tb-btn sm" @click="tableCommand('mergeCells')">合并</button>
      <button class="tb-btn sm" @click="tableCommand('splitCell')">拆分</button>
      <button class="tb-btn sm danger" @click="tableCommand('deleteRow')">删行</button>
      <button class="tb-btn sm danger" @click="tableCommand('deleteColumn')">删列</button>
      <button class="tb-btn sm danger" @click="tableCommand('deleteTable')">删表</button>
    </div>

    <!-- 表格右键菜单 -->
    <Teleport to="body">
      <div
        v-if="tableMenu.open"
        class="table-ctx"
        :style="{ top: tableMenu.y + 'px', left: tableMenu.x + 'px' }"
        @mousedown.prevent
      >
        <div class="ctx-title">表格</div>
        <button class="ctx-item" @click="ctxTable('addRowBefore')"><ArrowUp :size="14" /> 上方插入行</button>
        <button class="ctx-item" @click="ctxTable('addRowAfter')"><ArrowDown :size="14" /> 下方插入行</button>
        <button class="ctx-item" @click="ctxTable('addColumnBefore')"><ArrowLeft :size="14" /> 左侧插入列</button>
        <button class="ctx-item" @click="ctxTable('addColumnAfter')"><ArrowRight :size="14" /> 右侧插入列</button>
        <div class="ctx-sep"></div>
        <button class="ctx-item" @click="ctxTable('toggleHeaderRow')"><TableIcon :size="14" /> 切换表头行</button>
        <button class="ctx-item" @click="ctxTable('mergeCells')"><Combine :size="14" /> 合并单元格</button>
        <button class="ctx-item" @click="ctxTable('splitCell')"><Split :size="14" /> 拆分单元格</button>
        <div class="ctx-sep"></div>
        <button class="ctx-item danger" @click="ctxTable('deleteRow')"><Trash2 :size="14" /> 删除本行</button>
        <button class="ctx-item danger" @click="ctxTable('deleteColumn')"><Trash2 :size="14" /> 删除本列</button>
        <button class="ctx-item danger" @click="ctxTable('deleteTable')"><Trash2 :size="14" /> 删除表格</button>
      </div>
    </Teleport>

    <editor-content :editor="editor" class="editor-content" />

    <div v-if="editor" class="editor-status">
      <span>{{ charCount }} 字</span>
      <span class="hint">输入 “/” 插入标题、列表、表格、代码块等 · 拖动左侧 ⋮⋮ 调整块</span>
    </div>

    <!-- 块操作手柄 -->
    <div
      v-if="editor && handle.visible"
      class="block-handle"
      :style="{ top: handle.y + 'px', left: (handle.x - 56) + 'px' }"
      @mouseenter="handle.visible = true"
    >
      <button class="handle-btn add" title="在下方插入块" @click.stop="insertParagraphAfter">
        <Plus :size="15" />
      </button>
      <button
        class="handle-btn"
        title="拖动排序 / 点击菜单 / Shift 点击或拖拽多选"
        :draggable="!rangeDrag.active"
        @click.stop="toggleBlockMenu"
        @mousedown="onHandleMouseDown"
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
        <template v-if="multi.active">
          <div class="bm-title">已选 {{ multi.count }} 个块</div>
          <button class="bm-item" @click="copySelectedBlocks"><Copy :size="15" /> 复制所选块</button>
          <button class="bm-item danger" @click="deleteSelectedBlocks"><Trash2 :size="15" /> 删除所选块</button>
          <div class="bm-sep"></div>
          <button class="bm-item" @click="clearMultiSelection"><X :size="15" /> 取消选择</button>
        </template>
        <template v-else>
        <button class="bm-item" @click="moveBlock('up')"><ArrowUp :size="15" /> 上移</button>
        <button class="bm-item" @click="moveBlock('down')"><ArrowDown :size="15" /> 下移</button>
        <button class="bm-item" @click="duplicateBlock"><Copy :size="15" /> 复制</button>
        <button class="bm-item" @click="copyBlockMarkdown"><FileText :size="15" /> 复制 Markdown</button>
        <button class="bm-item" @click="insertParagraphAfter"><BetweenHorizontalEnd :size="15" /> 在下方加段落</button>
        <div class="bm-sep"></div>
        <button class="bm-item" @click="menuIndent(1)"><IndentIncrease :size="15" /> 缩进</button>
        <button class="bm-item" @click="menuIndent(-1)"><IndentDecrease :size="15" /> 减少缩进</button>
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
        </template>
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
        <span class="bubble-sep"></span>
        <el-dropdown trigger="click" :disabled="aiDialog.loading" @command="runAiAction">
          <button title="AI 助手"><Sparkles :size="15" /></button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="polish">润色</el-dropdown-item>
              <el-dropdown-item command="fix">纠错</el-dropdown-item>
              <el-dropdown-item command="summarize">总结</el-dropdown-item>
              <el-dropdown-item command="expand">扩写</el-dropdown-item>
              <el-dropdown-item command="translate">翻译（英文）</el-dropdown-item>
              <el-dropdown-item command="to_table">转为表格</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
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

    <!-- [[ 页面提及 / 标题(锚点)补全 -->
    <Teleport to="body">
      <div
        v-if="mention.open && mention.items.length"
        class="slash-menu"
        :style="{ top: mention.y + 'px', left: mention.x + 'px' }"
        @mousedown.prevent
      >
        <div class="slash-header">{{ mention.mode === 'heading' ? '链接到标题' : '链接到页面' }}</div>
        <div
          v-for="(item, i) in mention.items"
          :key="item.id"
          class="slash-item"
          :class="{ active: i === mention.index }"
          @mouseenter="mention.index = i"
          @click="pickMention(i)"
        >
          <span class="slash-icon">{{ item.kind === 'heading' ? 'H' + item.level : '📄' }}</span>
          <span class="slash-text">
            <span class="slash-title">{{ item.title }}</span>
            <span v-if="item.kind === 'heading'" class="slash-desc">#{{ item.slug }}</span>
          </span>
        </div>
      </div>
    </Teleport>

    <!-- wiki-link 悬浮预览(页面标题 + 摘要/锚点段落) -->
    <el-popover
      :visible="wikiPreview.visible"
      :virtual-ref="wikiPreviewRef"
      virtual-triggering
      placement="top"
      :width="340"
      :show-arrow="true"
    >
      <div class="wiki-preview">
        <div class="wiki-preview-title">{{ wikiPreview.title }}</div>
        <div class="wiki-preview-snippet">{{ wikiPreview.snippet }}</div>
      </div>
    </el-popover>

    <!-- @ 用户提及 -->
    <Teleport to="body">
      <div
        v-if="userMention.open && userMention.items.length"
        class="slash-menu"
        :style="{ top: userMention.y + 'px', left: userMention.x + 'px' }"
        @mousedown.prevent
      >
        <div class="slash-header">提及用户</div>
        <div
          v-for="(item, i) in userMention.items"
          :key="item.id"
          class="slash-item"
          :class="{ active: i === userMention.index }"
          @mouseenter="userMention.index = i"
          @click="pickUser(i)"
        >
          <span class="slash-icon">@</span>
          <span class="slash-text"><span class="slash-title">{{ item.name }}</span></span>
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

    <!-- AI 编辑预览 -->
    <el-dialog v-model="aiDialog.open" :title="aiDialogTitle" width="760px" append-to-body>
      <div v-loading="aiDialog.loading" class="ai-preview">
        <div class="ai-col">
          <div class="ai-col-title">原文</div>
          <div class="ai-col-body">{{ aiDialog.original || '（无）' }}</div>
        </div>
        <div class="ai-col">
          <div class="ai-col-title">AI 结果</div>
          <div class="ai-col-body">
            <template v-if="aiDialog.error"><span class="ai-error">{{ aiDialog.error }}</span></template>
            <template v-else-if="aiDialog.result">{{ aiDialog.result }}</template>
            <template v-else-if="aiDialog.loading"><span class="ai-muted">生成中…</span></template>
            <template v-else><span class="ai-muted">—</span></template>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="aiDialog.open = false">放弃</el-button>
        <el-button :disabled="!aiDialog.result" @click="applyAiResult('replace')">替换</el-button>
        <el-button type="primary" :disabled="!aiDialog.result" @click="applyAiResult('below')">插入到下方</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { watch, ref, computed, reactive, onMounted, onBeforeUnmount, nextTick, shallowRef } from 'vue'
import { useEditor, EditorContent, VueNodeViewRenderer, BubbleMenu } from '@tiptap/vue-3'
import { Extension } from '@tiptap/core'
import Suggestion from '@tiptap/suggestion'
import StarterKit from '@tiptap/starter-kit'
import Text from '@tiptap/extension-text'
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
import ImageNodeView from './ImageNodeView.vue'
import ToggleNodeView from './ToggleNodeView.vue'
import AttachmentNodeView from './AttachmentNodeView.vue'
import MathNodeView from './MathNodeView.vue'
import { TableFold } from './editorTableFold'
import { createLazyObserver, type LazyObserver } from '../utils/lazyRender'
import FootnoteItemView from './FootnoteItemView.vue'
import FootnoteBlockView from './FootnoteBlockView.vue'
import { Attachment, Callout, IndentBlock, Toggle } from './editorExt'
import { MathBlock, MathInline } from './editorMath'
import { FootnoteItem, FootnoteRef, Footnotes } from './editorFootnotes'
import {
  createHeadingSlugger, extractHeadingSection, extractHeadings, findWikiSuggestionMatch,
  markdownSnippet, parseWikiMentionQuery, splitWikiTarget,
} from '../utils/wikiAnchors'
import { applyBlockIndent, isListBlock, topBlocksInRange, type TopBlock } from '../utils/editorBlocks'
import { Markdown } from 'tiptap-markdown'
import 'katex/dist/katex.min.css'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'
import Collaboration from '@tiptap/extension-collaboration'
import CollaborationCursor from '@tiptap/extension-collaboration-cursor'
import mermaid from 'mermaid'
import MarkdownIt from 'markdown-it'
import http from '../api/http'
import { findMatchesInDoc, type FindMatch } from '../utils/findReplace'
import { sanitizePastedHTML } from '../utils/sanitizePaste'
import { nextFootnoteLabel } from '../utils/markdownFootnotes'
import { serializeTextMarkdown } from '../utils/markdownText'
import { serializeTableMarkdown } from '../utils/markdownTable'
import type { CollabPeer } from '../utils/collab'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plugin, PluginKey, TextSelection } from 'prosemirror-state'
import { Decoration, DecorationSet } from 'prosemirror-view'
import type { Editor } from '@tiptap/core'
import {
  Bold, Italic, Underline as UnderlineIcon, Strikethrough, Highlighter, Code, Link as LinkIcon,
  List, ListOrdered, ListChecks, Quote, SquareCode, AlignLeft, AlignCenter, AlignRight,
  Image as ImageIcon, Table as TableIcon, Workflow, Undo2, Redo2, Plus, Trash2, Copy,
  ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Pilcrow, GripVertical, Heading1, Heading2, Heading3, Eraser,
  Settings2, Keyboard, FileText, BetweenHorizontalEnd, Search, ChevronUp, ChevronDown, X, Sparkles, Paperclip,
  Combine, Split, IndentIncrease, IndentDecrease,
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

// ---------------- 非图片附件上传 ----------------
const ATTACHMENT_UPLOAD_TIMEOUT = 300000

function findAttachmentPos(uploadId: string): number {
  const e = editor.value
  if (!e) return -1
  let pos = -1
  e.state.doc.descendants((node, p) => {
    if (pos === -1 && node.type.name === 'attachment' && node.attrs.uploadId === uploadId) {
      pos = p
      return false
    }
    return true
  })
  return pos
}

function updateAttachmentByUploadId(uploadId: string, attrs: Record<string, unknown>) {
  const e = editor.value
  const pos = findAttachmentPos(uploadId)
  if (!e || pos < 0) return
  const node = e.state.doc.nodeAt(pos)
  if (!node) return
  e.view.dispatch(e.state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, ...attrs }))
}

function removeAttachmentByUploadId(uploadId: string) {
  const e = editor.value
  const pos = findAttachmentPos(uploadId)
  if (!e || pos < 0) return
  const node = e.state.doc.nodeAt(pos)
  if (!node) return
  e.view.dispatch(e.state.tr.delete(pos, pos + node.nodeSize))
}

async function insertAttachmentFile(file: File) {
  const e = editor.value
  if (!e) return
  const uploadId = Math.random().toString(36).slice(2, 12)
  e.chain().focus().insertContent({
    type: 'attachment',
    attrs: { url: '', name: file.name, size: file.size, mime: file.type || '', uploading: true, uploadId },
  }).run()
  const formData = new FormData()
  formData.append('file', file)
  try {
    const res = await http.post('/api/upload/attachment', formData, { timeout: ATTACHMENT_UPLOAD_TIMEOUT })
    const data = res.data || {}
    updateAttachmentByUploadId(uploadId, {
      url: data.url || '',
      name: data.name || file.name,
      size: Number(data.size) || file.size,
      mime: data.mime || file.type || '',
      uploading: false,
    })
    ElMessage.success('附件已上传')
  } catch (err: any) {
    removeAttachmentByUploadId(uploadId)
    const detail = err?.response?.data?.detail
    ElMessage.error('附件上传失败' + (typeof detail === 'string' && detail ? `：${detail}` : ''))
  }
}

// 粘贴/拖拽非图片文件 → 附件卡片;图片交给 imagePasteDrop 既有流程
const attachmentPasteDrop = new Plugin({
  key: new PluginKey('attachmentPasteDrop'),
  props: {
    handlePaste(_view, event) {
      const items = event.clipboardData?.items
      if (!items) return false
      const files: File[] = []
      for (const item of items) {
        if (item.kind !== 'file') continue
        const f = item.getAsFile()
        if (f && !f.type.startsWith('image/')) files.push(f)
      }
      if (!files.length) return false
      event.preventDefault()
      files.forEach(f => void insertAttachmentFile(f))
      return true
    },
    handleDrop(_view, event) {
      const files = event.dataTransfer?.files
      if (!files?.length) return false
      const others = Array.from(files).filter(f => !f.type.startsWith('image/'))
      if (!others.length) return false
      event.preventDefault()
      others.forEach(f => void insertAttachmentFile(f))
      return true
    },
  },
})

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
  collab?: { url: string; room: string; user: { name: string; color: string } } | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'collab-users', users: CollabPeer[]): void
  (e: 'collab-status', connected: boolean): void
  (e: 'collab-unavailable'): void
  (e: 'wiki-link', payload: { pageId: string; title: string; anchor: string }): void
}>()

let lastEmitted = props.modelValue
let applyingExternal = false

// 协同(Yjs): 每个页面一个房间; 未启用时为普通单机编辑
let ydoc: Y.Doc | null = null
let provider: WebsocketProvider | null = null
let collabSynced = false
let collabSeedDone = false
let collabSyncTimer: number | null = null
/** 同步完成前收到的外部内容(恢复版本/草稿), 同步后应用 */
let pendingExternal: string | null = null
const collabExtensions: any[] = []
if (props.collab) {
  ydoc = new Y.Doc()
  provider = new WebsocketProvider(props.collab.url, props.collab.room, ydoc)
  provider.awareness.setLocalStateField('user', props.collab.user)
  collabExtensions.push(
    Collaboration.configure({ document: ydoc }),
    CollaborationCursor.configure({ provider, user: props.collab.user }),
  )
}

function emitCollabUsers() {
  if (!provider) return
  const users: CollabPeer[] = []
  const seen = new Set<string>()
  provider.awareness.getStates().forEach((state: any) => {
    const u = state?.user
    if (!u?.name) return
    const key = `${u.name}|${u.color}`
    if (seen.has(key)) return
    seen.add(key)
    users.push({ name: u.name, color: u.color })
  })
  emit('collab-users', users)
}

if (provider) {
  const p = provider
  const onSynced = () => {
    if (collabSynced) return
    collabSynced = true
    emit('collab-status', true)
    if (pendingExternal !== null) {
      const markdown = pendingExternal
      pendingExternal = null
      nextTick(() => applyExternalContentNow(markdown))
    } else {
      nextTick(() => seedCollabIfEmpty())
    }
  }
  // 仅在与服务器初始同步完成后才允许播种/外部覆盖, 避免"本地插入 + 服务器内容"并发合并导致重复
  p.on('sync', onSynced)
  p.on('status', (event: { status: string }) => {
    emit('collab-status', event.status === 'connected')
  })
  p.awareness.on('change', emitCollabUsers)
  emitCollabUsers()
  if (p.synced) onSynced()
  // 探活通过但实际长时间无法同步 → 通知父级回退单人编辑(不破坏本地编辑)
  collabSyncTimer = window.setTimeout(() => {
    if (!collabSynced) emit('collab-unavailable')
  }, 5000)
}

function seedCollabIfEmpty() {
  if (!props.collab || !ydoc || !editor.value || !collabSynced || collabSeedDone) return
  try {
    if (ydoc.getXmlFragment('default').length > 0) {
      collabSeedDone = true
      return
    }
    const markdown = props.modelValue
    if (!markdown) return
    collabSeedDone = true
    editor.value.commands.setContent(markdown, false)
  } catch { /* ignore */ }
}

/**
 * 外部内容替换(恢复历史版本 / 恢复离线草稿):
 * - 单机模式立即应用;
 * - 协同模式同步前先挂起(避免本地写入与服务器内容并发合并), 同步后写入 Yjs 并广播。
 */
function applyExternalContent(markdown: string) {
  if (props.collab && !collabSynced) {
    pendingExternal = markdown || ''
    return
  }
  applyExternalContentNow(markdown || '')
}

function applyExternalContentNow(markdown: string) {
  const e = editor.value
  if (!e) return
  collabSeedDone = true
  lastEmitted = markdown
  applyingExternal = true
  e.commands.setContent(markdown, false)
  applyingExternal = false
  mermaidCache.clear()
  nextTick(() => { scheduleMermaid(); disableSpellcheck() })
}

defineExpose({ applyExternalContent })

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
  { title: '提示框', desc: '信息提示块', icon: '💡', keywords: ['callout', 'info', '提示', '信息', '框'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'callout', attrs: { type: 'info' }, content: [{ type: 'paragraph' }] }).run() } },
  { title: '成功框', desc: '成功/完成提示块', icon: '✅', keywords: ['success', '成功', '完成', '框'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'callout', attrs: { type: 'success' }, content: [{ type: 'paragraph' }] }).run() } },
  { title: '警告框', desc: '警告/注意提示块', icon: '⚠️', keywords: ['warn', 'warning', '警告', '注意', '框'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'callout', attrs: { type: 'warn' }, content: [{ type: 'paragraph' }] }).run() } },
  { title: '危险框', desc: '严重风险提示块', icon: '⛔', keywords: ['danger', 'error', '危险', '错误', '框'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'callout', attrs: { type: 'danger' }, content: [{ type: 'paragraph' }] }).run() } },
  { title: '折叠块', desc: '可展开/收起的内容', icon: '▸', keywords: ['toggle', 'collapse', '折叠', '收起', '展开'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'toggle', attrs: { open: true, title: '折叠块' }, content: [{ type: 'paragraph' }] }).run() } },
  { title: '行内公式', desc: 'KaTeX 行内公式 $…$', icon: '∑', keywords: ['math', 'katex', 'latex', '公式', '行内', '数学'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'mathInline', attrs: { latex: '' } }).run() } },
  { title: '公式', desc: 'KaTeX 块级公式 $$…$$', icon: 'ƒ', keywords: ['math', 'katex', 'latex', 'formula', '公式', '块级', '数学'], action: (e, r) => { e.chain().focus().deleteRange(r).insertContent({ type: 'mathBlock', attrs: { latex: '' } }).run() } },
  { title: '脚注', desc: '行内脚注引用，编号自动递增', icon: '¹', keywords: ['footnote', '脚注', '引用', '注释', '注解'], action: (e, r) => {
    const label = nextFootnoteLabel(e.state.doc)
    e.chain().focus().deleteRange(r).insertContent({ type: 'footnoteRef', attrs: { id: `fn-ref-${label}`, label } }).run()
  } },
  { title: '脚注区块', desc: '页面底部脚注条目区', icon: '⁂', keywords: ['footnote', 'notes', '脚注', '条目', '注释', '注解'], action: (e, r) => {
    let exists = false
    e.state.doc.descendants((node) => { if (node.type.name === 'footnotes') exists = true })
    if (exists) {
      ElMessage.warning('已存在脚注区块，请直接在其中编辑')
      e.chain().focus().deleteRange(r).run()
      return
    }
    const label = nextFootnoteLabel(e.state.doc)
    e.chain().focus().deleteRange(r).insertContent({ type: 'footnotes', content: [{ type: 'footnoteItem', attrs: { label }, content: [] }] }).run()
  } },
  { title: '图片', desc: '上传或插入图片', icon: '▧', keywords: ['image', 'img', '图片', '照片'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); handleImageUpload() } },
  { title: '附件', desc: '上传文件附件卡片', icon: '📎', keywords: ['attachment', 'file', '附件', '文件'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); handleAttachmentUpload() } },
  { title: '图表', desc: 'Mermaid 流程图/时序图', icon: '◈', keywords: ['mermaid', 'chart', 'diagram', '图表', '流程图'], action: (e, r) => { e.chain().focus().deleteRange(r).run(); insertMermaid() } },
  { title: 'AI 续写', desc: '根据光标前文继续写作', icon: '✨', keywords: ['ai', 'continue', '续写', '生成', '写作'], action: (e, r) => { void aiContinue(e, r) } },
  { title: 'AI 总结本页', desc: '生成整页摘要并插入提示框', icon: '🤖', keywords: ['ai', 'summary', '总结', '摘要'], action: (e, r) => { void aiSummarize(e, r) } },
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
        pluginKey: new PluginKey('slashSuggestion'),
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

// ---------------- [[ 页面提及 / [[页面#标题]] 标题补全 ----------------
interface MentionItem {
  id: string
  title: string
  kind: 'page' | 'heading'
  level?: number
  pageId?: string
  pageTitle?: string
  slug?: string
}

const mention = reactive({
  open: false,
  items: [] as MentionItem[],
  index: 0,
  x: 0,
  y: 0,
  mode: 'page' as 'page' | 'heading',
})
let mentionCommand: ((item: MentionItem) => void) | null = null
let mentionPages: { id: string; title: string }[] = []
let mentionLoaded = false
/** 标题按需拉取(选中页面后输入 # 才请求), 按页面 id 缓存 */
const headingCache = new Map<string, { level: number; text: string; slug: string }[]>()
/** 标题 -> 页面 id(用于 wiki-link 装饰与点击解析, 标题重复取第一个) */
const mentionPageByTitle = new Map<string, string>()

async function ensureMentionPages() {
  if (mentionLoaded) return
  try {
    const res = await http.get('/api/pages', { params: { page: 1, page_size: 500 } })
    mentionPages = (res.data.items || []).map((p: any) => ({ id: p.id, title: p.title || '无标题' }))
    mentionPageByTitle.clear()
    for (const p of mentionPages) {
      const key = p.title.toLowerCase()
      if (!mentionPageByTitle.has(key)) mentionPageByTitle.set(key, p.id)
    }
    mentionLoaded = true
    refreshWikiLinkDecorations()
  } catch { /* ignore */ }
}

function findMentionPage(title: string): { id: string; title: string } | null {
  const q = String(title || '').trim().toLowerCase()
  if (!q) return null
  const exact = mentionPages.find(p => p.title.toLowerCase() === q)
  if (exact) return exact
  const prefixed = mentionPages.filter(p => p.title.toLowerCase().startsWith(q))
  return prefixed.length === 1 ? prefixed[0] : null
}

async function loadPageHeadings(pageId: string) {
  const cached = headingCache.get(pageId)
  if (cached) return cached
  try {
    const res = await http.get(`/api/pages/${pageId}`)
    const list = extractHeadings(String(res.data?.content || ''))
    headingCache.set(pageId, list)
    return list
  } catch {
    return []
  }
}

async function mentionItems(query: string): Promise<MentionItem[]> {
  await ensureMentionPages()
  const parsed = parseWikiMentionQuery(query)
  if (parsed.anchorPart !== null) {
    const page = findMentionPage(parsed.pagePart)
    if (!page) return []
    const headings = await loadPageHeadings(page.id)
    const q = parsed.anchorPart.toLowerCase()
    // 选择/输入完成后(链接已闭合且锚点精确命中)关闭菜单
    if (parsed.complete && q && headings.some(h => h.slug === q)) return []
    return headings
      .filter(h => !q || h.text.toLowerCase().includes(q) || h.slug.includes(q))
      .slice(0, 20)
      .map(h => ({
        id: `${page.id}#${h.slug}`,
        title: h.text,
        kind: 'heading' as const,
        level: h.level,
        pageId: page.id,
        pageTitle: page.title,
        slug: h.slug,
      }))
  }
  const q = parsed.pagePart.toLowerCase()
  if (parsed.complete && q && mentionPages.some(p => p.title.toLowerCase() === q)) return []
  return mentionPages
    .filter(p => !q || p.title.toLowerCase().includes(q))
    .slice(0, 20)
    .map(p => ({ id: p.id, title: p.title, kind: 'page' as const }))
}

function mentionModeOf(query: string): 'page' | 'heading' {
  return parseWikiMentionQuery(query).anchorPart !== null ? 'heading' : 'page'
}

function pickMention(i: number) {
  const item = mention.items[i]
  if (item && mentionCommand) mentionCommand(item)
  mention.open = false
}

const PageMention = Extension.create({
  name: 'pageMention',
  addProseMirrorPlugins() {
    return [
      Suggestion({
        editor: this.editor,
        pluginKey: new PluginKey('mentionSuggestion'),
        char: '[[',
        startOfLine: false,
        allowSpaces: false,
        findSuggestionMatch: findWikiSuggestionMatch,
        items: async ({ query }: { query: string }) => mentionItems(query),
        command: ({ editor, range, props: item }: any) => {
          if (item.kind === 'heading') {
            editor.chain().focus().deleteRange(range)
              .insertContent({ type: 'text', text: `[[${item.pageTitle}#${item.slug}]]` })
              .run()
            return
          }
          // 插入完整链接, 光标停在 ]] 之前: 继续输入 # 即可进入标题列表
          editor.chain().focus().deleteRange(range)
            .insertContent({ type: 'text', text: `[[${item.title}]]` })
            .setTextSelection(range.from + 2 + String(item.title).length)
            .run()
        },
        render: () => ({
          onStart: (p: any) => {
            mention.items = p.items || []
            mention.index = 0
            mention.mode = mentionModeOf(p.query || '')
            mention.open = mention.items.length > 0
            mentionCommand = (it: any) => p.command(it)
            const rect = p.clientRect?.()
            if (rect) { mention.x = rect.left; mention.y = rect.bottom + 6 }
          },
          onUpdate: (p: any) => {
            mention.items = p.items || []
            mention.index = 0
            mention.mode = mentionModeOf(p.query || '')
            mention.open = mention.items.length > 0
            mentionCommand = (it: any) => p.command(it)
            const rect = p.clientRect?.()
            if (rect) { mention.x = rect.left; mention.y = rect.bottom + 6 }
          },
          onKeyDown: (p: any) => {
            if (!mention.open || !mention.items.length) return false
            if (p.event.key === 'ArrowDown') { mention.index = (mention.index + 1) % mention.items.length; return true }
            if (p.event.key === 'ArrowUp') { mention.index = (mention.index - 1 + mention.items.length) % mention.items.length; return true }
            if (p.event.key === 'Enter') { pickMention(mention.index); return true }
            if (p.event.key === 'Escape') { mention.open = false; return true }
            return false
          },
          onExit: () => { mention.open = false },
        }),
      }),
    ]
  },
})

// ---------------- 编辑器内 wiki-link: 标题 id / [[链接]] 装饰 / 点击跳转 / 悬浮预览 ----------------
const wikiLinkKey = new PluginKey('ragWikiLinks')
const headingAnchorKey = new PluginKey('ragHeadingAnchors')
const WIKI_LINK_RE = /\[\[([^[\]\n]+?)\]\]/g

/** 顶层 h1-h6 生成稳定 id(slug + 序号去重), 供 [[页面#锚点]] 滚动定位 */
const headingAnchorPlugin = new Plugin({
  key: headingAnchorKey,
  props: {
    decorations(state) {
      const decos: Decoration[] = []
      const slugger = createHeadingSlugger()
      state.doc.forEach((node, offset) => {
        if (node.type.name !== 'heading' || !node.textContent.trim()) return
        decos.push(Decoration.node(offset, offset + node.nodeSize, { id: slugger(node.textContent) }))
      })
      return decos.length ? DecorationSet.create(state.doc, decos) : null
    },
  },
})

/** [[页面]] / [[页面#锚点]] 装饰为可点击的 .wiki-link(内容保持纯文本, 往返不变) */
const wikiLinkPlugin = new Plugin({
  key: wikiLinkKey,
  props: {
    decorations(state) {
      const decos: Decoration[] = []
      state.doc.descendants((node, pos) => {
        if (!node.isText || !node.text) return
        WIKI_LINK_RE.lastIndex = 0
        let m: RegExpExecArray | null
        while ((m = WIKI_LINK_RE.exec(node.text))) {
          const { title, anchor } = splitWikiTarget(m[1])
          if (!title) continue
          const id = mentionPageByTitle.get(title.toLowerCase()) || ''
          const from = pos + m.index
          decos.push(Decoration.inline(from, from + m[0].length, {
            class: id ? 'wiki-link' : 'wiki-link wiki-link-missing',
            'data-id': id,
            'data-anchor': anchor,
            'data-wiki-title': title,
          }))
        }
      })
      return decos.length ? DecorationSet.create(state.doc, decos) : null
    },
  },
})

/** 页面列表异步到达后触发一次无步骤事务, 刷新装饰里的 标题→id 映射 */
function refreshWikiLinkDecorations() {
  const e = editor.value
  if (!e) return
  e.view.dispatch(e.state.tr.setMeta(wikiLinkKey, Date.now()))
}

const wikiPreview = reactive({ visible: false, title: '', snippet: '' })
const wikiPreviewRef = shallowRef<HTMLElement>()
const wikiPageCache = new Map<string, { title: string; content: string }>()
let wikiPreviewSeq = 0

async function resolveWikiPageId(el: HTMLElement, title: string): Promise<string> {
  let id = el.getAttribute('data-id') || ''
  if (!id) {
    await ensureMentionPages()
    id = mentionPageByTitle.get(title.toLowerCase()) || ''
  }
  return id
}

async function showWikiPreview(el: HTMLElement) {
  const title = el.getAttribute('data-wiki-title') || el.textContent || ''
  const anchor = el.getAttribute('data-anchor') || ''
  wikiPreviewRef.value = el
  wikiPreview.title = title
  wikiPreview.snippet = '加载中…'
  wikiPreview.visible = true
  const seq = ++wikiPreviewSeq
  try {
    const id = await resolveWikiPageId(el, title)
    if (!id) throw new Error('missing')
    let data = wikiPageCache.get(id)
    if (!data) {
      const res = await http.get(`/api/pages/${id}`)
      data = { title: res.data?.title || title, content: String(res.data?.content || '') }
      wikiPageCache.set(id, data)
    }
    if (seq !== wikiPreviewSeq || !wikiPreview.visible) return
    wikiPreview.title = data.title
    wikiPreview.snippet = anchor
      ? (extractHeadingSection(data.content, anchor) || markdownSnippet(data.content))
      : markdownSnippet(data.content)
  } catch {
    // 加载失败静默关闭
    if (seq === wikiPreviewSeq) wikiPreview.visible = false
  }
}

function hideWikiPreview() {
  wikiPreviewSeq++
  wikiPreview.visible = false
}

function onEditorMouseOver(ev: MouseEvent) {
  const link = (ev.target as HTMLElement | null)?.closest?.('.wiki-link') as HTMLElement | null
  if (!link || !editor.value?.view.dom.contains(link)) return
  if (wikiPreview.visible && wikiPreviewRef.value === link) return
  void showWikiPreview(link)
}

async function openWikiLink(el: HTMLElement) {
  const title = el.getAttribute('data-wiki-title') || ''
  const anchor = el.getAttribute('data-anchor') || ''
  const id = await resolveWikiPageId(el, title)
  if (!id) {
    ElMessage.warning(`未找到页面「${title}」`)
    return
  }
  emit('wiki-link', { pageId: id, title, anchor })
}

// ---------------- @ 用户提及 ----------------
const userMention = reactive({ open: false, items: [] as { id: string; name: string }[], index: 0, x: 0, y: 0 })
let userMentionCommand: ((item: { id: string; name: string }) => void) | null = null
let userList: { id: string; name: string }[] = []
let userLoaded = false

async function ensureUsers() {
  if (userLoaded) return
  try {
    const res = await http.get('/api/auth/users')
    userList = (res.data || []).map((u: any) => ({ id: u.id, name: u.name || u.username }))
    userLoaded = true
  } catch { /* ignore */ }
}
function pickUser(i: number) {
  const item = userMention.items[i]
  if (item && userMentionCommand) userMentionCommand(item)
  userMention.open = false
}

const UserMention = Extension.create({
  name: 'userMention',
  addProseMirrorPlugins() {
    return [
      Suggestion({
        editor: this.editor,
        pluginKey: new PluginKey('userMentionSuggestion'),
        char: '@',
        startOfLine: false,
        allowSpaces: false,
        items: async ({ query }: { query: string }) => {
          await ensureUsers()
          const q = (query || '').trim().toLowerCase()
          if (!q) return userList.slice(0, 20)
          return userList.filter(u => u.name.toLowerCase().includes(q)).slice(0, 20)
        },
        command: ({ editor, range, props: item }: any) => {
          editor.chain().focus().deleteRange(range).insertContent({ type: 'text', text: `@${item.name}` }).run()
        },
        render: () => ({
          onStart: (p: any) => {
            userMention.items = p.items || []
            userMention.index = 0
            userMention.open = userMention.items.length > 0
            userMentionCommand = (it: any) => p.command(it)
            const rect = p.clientRect?.()
            if (rect) { userMention.x = rect.left; userMention.y = rect.bottom + 6 }
          },
          onUpdate: (p: any) => {
            userMention.items = p.items || []
            userMention.index = 0
            userMention.open = userMention.items.length > 0
            userMentionCommand = (it: any) => p.command(it)
            const rect = p.clientRect?.()
            if (rect) { userMention.x = rect.left; userMention.y = rect.bottom + 6 }
          },
          onKeyDown: (p: any) => {
            if (!userMention.open || !userMention.items.length) return false
            if (p.event.key === 'ArrowDown') { userMention.index = (userMention.index + 1) % userMention.items.length; return true }
            if (p.event.key === 'ArrowUp') { userMention.index = (userMention.index - 1 + userMention.items.length) % userMention.items.length; return true }
            if (p.event.key === 'Enter') { pickUser(userMention.index); return true }
            if (p.event.key === 'Escape') { userMention.open = false; return true }
            return false
          },
          onExit: () => { userMention.open = false },
        }),
      }),
    ]
  },
})

// ---------------- 页内查找/替换 ----------------
const find = reactive({
  open: false,
  query: '',
  replacement: '',
  matches: [] as FindMatch[],
  current: 0,
})

const findInputEl = ref<HTMLInputElement>()

const findKey = new PluginKey('ragFindReplace')

const findPlugin = new Plugin({
  key: findKey,
  props: {
    decorations(state) {
      if (!find.open || !find.query.trim()) return null
      const decos: Decoration[] = []
      find.matches.forEach((m, i) => {
        decos.push(Decoration.inline(m.from, m.to, {
          class: i === find.current ? 'find-hit find-hit-current' : 'find-hit',
        }))
      })
      return DecorationSet.create(state.doc, decos)
    },
  },
})

const findCountText = computed(() => {
  if (!find.query.trim()) return ''
  if (!find.matches.length) return '无结果'
  return `第 ${find.current + 1}/${find.matches.length} 个`
})

function collectFindMatches(): FindMatch[] {
  const e = editor.value
  if (!e) return []
  return findMatchesInDoc(e.state.doc, find.query)
}

function redrawFind() {
  const e = editor.value
  if (!e) return
  // 仅带 meta 的事务不会进入 undo 栈
  e.view.dispatch(e.state.tr.setMeta('ragFind', Date.now()))
}

function scrollToFindMatch() {
  const e = editor.value
  const m = find.matches[find.current]
  if (!e || !m) return
  try {
    const domAt = e.view.domAtPos(m.from)
    const el = domAt.node.nodeType === 3 ? domAt.node.parentElement : (domAt.node as Element)
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  } catch { /* ignore */ }
}

function refreshFind(resetIndex = false) {
  find.matches = collectFindMatches()
  if (resetIndex) find.current = 0
  if (find.matches.length === 0) find.current = 0
  else if (find.current >= find.matches.length) find.current = find.matches.length - 1
  redrawFind()
}

function openFind() {
  find.open = true
  if (find.query.trim()) refreshFind()
  nextTick(() => {
    findInputEl.value?.focus()
    findInputEl.value?.select()
  })
}

function closeFind() {
  find.open = false
  redrawFind()
}

function onFindInput() {
  refreshFind(true)
  scrollToFindMatch()
}

function nextMatch() {
  if (!find.matches.length) return
  find.current = (find.current + 1) % find.matches.length
  redrawFind()
  scrollToFindMatch()
}

function prevMatch() {
  if (!find.matches.length) return
  find.current = (find.current - 1 + find.matches.length) % find.matches.length
  redrawFind()
  scrollToFindMatch()
}

function replaceCurrent() {
  const e = editor.value
  const m = find.matches[find.current]
  if (!e || !m) return
  const chain = e.chain()
  if (find.replacement) {
    chain.insertContentAt({ from: m.from, to: m.to }, { type: 'text', text: find.replacement })
  } else {
    chain.deleteRange({ from: m.from, to: m.to })
  }
  chain.run()
  refreshFind()
  scrollToFindMatch()
}

function replaceAll() {
  const e = editor.value
  if (!e || !find.matches.length) return
  const chain = e.chain()
  // 从后往前替换, 单条链单事务, undo 一步可回退
  for (let i = find.matches.length - 1; i >= 0; i--) {
    const m = find.matches[i]
    if (find.replacement) {
      chain.insertContentAt({ from: m.from, to: m.to }, { type: 'text', text: find.replacement })
    } else {
      chain.deleteRange({ from: m.from, to: m.to })
    }
  }
  chain.run()
  ElMessage.success(`已替换 ${find.matches.length} 处`)
  refreshFind()
}

// ---------------- AI 编辑 ----------------
const AI_MD = new MarkdownIt({ html: false, breaks: true, linkify: true })

const AI_LABELS: Record<string, string> = {
  polish: '润色',
  fix: '纠错',
  summarize: '总结',
  expand: '扩写',
  translate: '翻译（英文）',
  to_table: '转为表格',
  continue: '续写',
}

const aiDialog = reactive({
  open: false,
  loading: false,
  action: '',
  original: '',
  result: '',
  error: '',
  from: 0,
  to: 0,
})

const aiDialogTitle = computed(() => `AI ${AI_LABELS[aiDialog.action] || '编辑'}`)

let lastAiSelection: { from: number; to: number } | null = null

function renderAiMarkdown(text: string) {
  return AI_MD.render(text)
}

function showAiLoading(text: string) {
  const tip = ElMessage({ message: text, duration: 0, type: 'info' })
  return () => tip.close()
}

async function requestAi(payload: Record<string, unknown>) {
  const res = await http.post('/api/editor/ai', payload, { timeout: 120000 })
  return String(res.data?.result ?? '').trim()
}

async function runAiAction(action: string) {
  const e = editor.value
  if (!e) return
  const sel = e.state.selection
  const range = sel.from !== sel.to ? { from: sel.from, to: sel.to } : lastAiSelection
  if (!range) {
    ElMessage.warning('请先选择文本')
    return
  }
  const from = Math.min(range.from, range.to)
  const to = Math.max(range.from, range.to)
  const text = e.state.doc.textBetween(from, to, '\n', ' ')
  if (!text.trim()) {
    ElMessage.warning('请先选择文本')
    return
  }
  if (text.length > 8000) {
    ElMessage.warning('选中文本超过 8000 字，请缩小选择范围')
    return
  }
  const ctxFrom = Math.max(0, from - 500)
  const ctxTo = Math.min(e.state.doc.content.size, to + 500)
  const before = e.state.doc.textBetween(ctxFrom, from, '\n', ' ')
  const after = e.state.doc.textBetween(to, ctxTo, '\n', ' ')
  const context = (before + after).trim()

  aiDialog.open = true
  aiDialog.loading = true
  aiDialog.action = action
  aiDialog.original = text
  aiDialog.result = ''
  aiDialog.error = ''
  aiDialog.from = from
  aiDialog.to = to
  try {
    const payload: Record<string, unknown> = { action, text }
    if (context) payload.context = context.slice(0, 2000)
    if (action === 'translate') payload.target_lang = 'en'
    aiDialog.result = await requestAi(payload)
    if (!aiDialog.result) aiDialog.error = 'AI 未返回内容'
  } catch (err: any) {
    aiDialog.error = err?.response?.data?.detail || err?.message || '请求失败'
    ElMessage.error('AI 处理失败：' + aiDialog.error)
  } finally {
    aiDialog.loading = false
  }
}

function applyAiResult(mode: 'replace' | 'below') {
  const e = editor.value
  if (!e || !aiDialog.result) return
  const html = renderAiMarkdown(aiDialog.result)
  if (mode === 'replace') {
    e.chain().focus().insertContentAt({ from: aiDialog.from, to: aiDialog.to }, html).run()
  } else {
    e.chain().focus().insertContentAt(aiDialog.to, html).run()
  }
  aiDialog.open = false
  ElMessage.success(mode === 'replace' ? '已替换' : '已插入')
}

async function aiContinue(e: Editor, range: { from: number; to: number }) {
  const before = e.state.doc.textBetween(0, range.from, '\n', ' ')
  const context = before.slice(-4000)
  if (!context.trim()) {
    ElMessage.warning('光标前没有内容，无法续写')
    return
  }
  e.chain().focus().deleteRange(range).run()
  const pos = range.from
  const closeTip = showAiLoading('AI 续写中…')
  try {
    const result = await requestAi({ action: 'continue', text: context.slice(-500), context })
    if (!result) throw new Error('未返回内容')
    e.chain().focus().insertContentAt(pos, renderAiMarkdown(result)).run()
  } catch (err: any) {
    ElMessage.error('AI 续写失败：' + (err?.response?.data?.detail || err?.message || '请求失败'))
  } finally {
    closeTip()
  }
}

async function aiSummarize(e: Editor, range: { from: number; to: number }) {
  const plain = e.state.doc.textBetween(0, e.state.doc.content.size, '\n', ' ').slice(0, 8000)
  e.chain().focus().deleteRange(range).run()
  const pos = range.from
  const closeTip = showAiLoading('AI 总结中…')
  try {
    const result = await requestAi({ action: 'summarize', text: plain })
    if (!result) throw new Error('未返回内容')
    const html = `<div data-callout="info">${renderAiMarkdown(result)}</div>`
    e.chain().focus().insertContentAt(pos, html).run()
  } catch (err: any) {
    ElMessage.error('AI 总结失败：' + (err?.response?.data?.detail || err?.message || '请求失败'))
  } finally {
    closeTip()
  }
}

const editor = useEditor({
  extensions: [
    StarterKit.configure({ codeBlock: false, text: false, history: props.collab ? false : undefined }),
    // 自定义文本序列化: 保留 [[wiki 链接]] 字面量、行内代码内容原样输出
    Text.extend({
      addStorage() {
        return { markdown: { serialize: serializeTextMarkdown } }
      },
    }),
    CodeBlockLowlight
      .extend({ addNodeView() { return VueNodeViewRenderer(CodeBlockComponent) } })
      .configure({ lowlight, defaultLanguage: 'plaintext' }),
    Placeholder.configure({ placeholder: '开始写笔记... 输入 “/” 插入内容块' }),
    Image.extend({
      addAttributes() {
        return {
          ...(this.parent?.() ?? {}),
          width: {
            default: null,
            parseHTML: (el: HTMLElement) => {
              const w = el.getAttribute('width')
              return w ? parseInt(w, 10) || null : null
            },
            renderHTML: (attrs: Record<string, any>) => (attrs.width ? { width: attrs.width } : {}),
          },
        }
      },
      addNodeView() {
        return VueNodeViewRenderer(ImageNodeView)
      },
      addStorage() {
        return {
          markdown: {
            serialize(state: any, node: any) {
              const { src, alt, title, width } = node.attrs
              if (width) {
                const a = String(alt || '').replace(/"/g, '&quot;')
                const t = title ? ` title="${String(title).replace(/"/g, '&quot;')}"` : ''
                state.write(`<img src="${src}" alt="${a}"${t} width="${width}">`)
              } else {
                const t = title ? ` "${String(title).replace(/"/g, '\\"')}"` : ''
                state.write(`![${alt || ''}](${src}${t})`)
              }
            },
          },
        }
      },
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
    // 自定义表格序列化: 单元格转义 `|`、硬换行转 <br>、多段落/跨行列回退 HTML
    Table.extend({
      addStorage() {
        return { markdown: { serialize: serializeTableMarkdown } }
      },
    }).configure({ resizable: true }),
    TableFold,
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
    Callout,
    IndentBlock,
    Toggle.extend({ addNodeView() { return VueNodeViewRenderer(ToggleNodeView) } }),
    Attachment.extend({ addNodeView() { return VueNodeViewRenderer(AttachmentNodeView) } }),
    MathInline.extend({ addNodeView() { return VueNodeViewRenderer(MathNodeView) } }),
    MathBlock.extend({ addNodeView() { return VueNodeViewRenderer(MathNodeView) } }),
    FootnoteRef,
    Footnotes.extend({ addNodeView() { return VueNodeViewRenderer(FootnoteBlockView) } }),
    FootnoteItem.extend({ addNodeView() { return VueNodeViewRenderer(FootnoteItemView) } }),
    ...collabExtensions,
    SlashCommand,
    PageMention,
    UserMention,
    attachmentPasteDrop,
    findPlugin,
    headingAnchorPlugin,
    wikiLinkPlugin,
    Markdown.configure({ html: true, breaks: true, linkify: true }),
  ],
  // 协同模式绝不把 content 交给编辑器初始文档(会在 Yjs 同步前写入本地并和服务器内容合并 → 重复);
  // 改为同步完成后由 seedCollabIfEmpty 从 Markdown 播种空文档。
  content: props.collab ? '' : props.modelValue,
  onUpdate: ({ editor }) => {
    const markdown = editor.storage.markdown.getMarkdown()
    lastEmitted = markdown
    if (!applyingExternal) emit('update:modelValue', markdown)
    if (find.open) refreshFind()
    nextTick(() => { scheduleMermaid(); disableSpellcheck() })
  },
  onCreate: () => {
    nextTick(() => { scheduleMermaid(); disableSpellcheck() })
  },
  onSelectionUpdate: ({ editor }) => {
    const { from, to } = editor.state.selection
    if (from !== to) lastAiSelection = { from, to }
    syncMultiSelection()
    keepCaretCentered()
  },
  editorProps: {
    transformPastedHTML: (html: string) => sanitizePastedHTML(html),
    handleClick: (_view, _pos, event) => {
      const link = (event.target as HTMLElement | null)?.closest?.('.wiki-link') as HTMLElement | null
      if (!link) return false
      event.preventDefault()
      void openWikiLink(link)
      return true
    },
    handleKeyDown: (_view, event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setLink()
        return true
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        openFind()
        return true
      }
      if (event.key === 'Tab' && !event.altKey && !event.ctrlKey && !event.metaKey) {
        const e = editor.value
        if (!e) return false
        // 列表用原生 sink/lift, 代码块/表格保留各自的 Tab 行为
        if (e.isActive('codeBlock') || e.isActive('table') || e.isActive('listItem') || e.isActive('taskItem')) return false
        event.preventDefault()
        applyIndentToBlocks(selectedTopBlocks(), event.shiftKey ? -1 : 1)
        return true
      }
      if (event.key === 'Escape' && multi.active) {
        event.preventDefault()
        clearMultiSelection()
        return true
      }
      if (event.key === 'Escape' && find.open) {
        event.preventDefault()
        closeFind()
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

// ---------------- 表格操作(工具条 + 右键菜单) ----------------
const tableMenu = reactive({ open: false, x: 0, y: 0 })

/**
 * 安全执行表格命令: 空表格/无表格选区/合并拆分不适用等边界下 can() 为 false,
 * 直接跳过而不是抛错, 保证编辑器不中断。
 */
function tableCommand(name: string) {
  const e = editor.value
  if (!e) return
  try {
    const can: any = e.can().chain().focus()
    if (typeof can[name] !== 'function') return
    if (!can[name]().run()) return
    const chain: any = e.chain().focus()
    chain[name]().run()
  } catch { /* 边界情况静默忽略 */ }
}

function onTableContextMenu(ev: MouseEvent) {
  const e = editor.value
  if (!e) return
  const target = ev.target as HTMLElement | null
  const cell = target?.closest('td, th') as HTMLElement | null
  if (!cell || !e.view.dom.contains(cell)) return
  ev.preventDefault()
  try {
    const pos = e.view.posAtDOM(cell, 0)
    e.chain().focus().setTextSelection(Math.min(pos, e.state.doc.content.size)).run()
  } catch { /* ignore */ }
  tableMenu.open = true
  tableMenu.x = Math.max(8, Math.min(ev.clientX, window.innerWidth - 196))
  tableMenu.y = Math.max(8, Math.min(ev.clientY, window.innerHeight - 360))
}

function closeTableMenu() {
  tableMenu.open = false
}

function ctxTable(name: string) {
  tableMenu.open = false
  tableCommand(name)
}

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
/** 多块选区(Shift+点手柄 / Shift+拖拽手柄): 块菜单切换为批量操作 */
const multi = reactive({ active: false, count: 0 })
const rangeDrag = reactive({ active: false, anchorIndex: -1, lastIndex: -1 })

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
  hideWikiPreview()
  leaveTimer = window.setTimeout(() => {
    leaveTimer = null
    if (!blockMenu.open) handle.visible = false
  }, 200)
}

function toggleBlockMenu() {
  blockMenu.open = !blockMenu.open
}

interface BlockRange { pos: number; end: number; index: number; node: any }

function topRange(pos: number): BlockRange | null {
  const e = editor.value
  if (!e) return null
  const doc = e.state.doc
  const clamped = Math.max(0, Math.min(pos, doc.content.size))
  return topBlocksInRange(doc, clamped, clamped)[0] ?? null
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
    tr.delete(r.pos, r.end)
    tr.insert(r.pos - prev.nodeSize, r.node)
  } else {
    if (r.index >= doc.childCount - 1) return
    const next = doc.child(r.index + 1)
    tr.delete(r.pos, r.end)
    tr.insert(r.pos + next.nodeSize, r.node)
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
  e.view.dispatch(e.state.tr.delete(r.pos, r.end))
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

// ---------------- 块缩进 / 多块选区 ----------------
function allTopBlocks(): TopBlock[] {
  const e = editor.value
  if (!e) return []
  return topBlocksInRange(e.state.doc, 0, e.state.doc.content.size)
}

function selectedTopBlocks(): TopBlock[] {
  const e = editor.value
  if (!e) return []
  const { from, to } = e.state.selection
  return topBlocksInRange(e.state.doc, from, to)
}

/** 同步多块选区状态: 跨 ≥2 个顶层块的非折叠选区视为多块选区 */
function syncMultiSelection() {
  const e = editor.value
  if (!e) { multi.active = false; multi.count = 0; return }
  const sel = e.state.selection
  if (sel.from === sel.to) {
    multi.active = false
    multi.count = 0
    rangeDrag.anchorIndex = -1
    return
  }
  const blocks = topBlocksInRange(e.state.doc, sel.from, sel.to)
  multi.count = blocks.length
  multi.active = blocks.length >= 2
}

/** 从「锚点块到目标块」建立 TextSelection(NodeSelection 无法跨块, 用文本选区表达) */
function selectBlockRange(anchorIndex: number, targetIndex: number) {
  const e = editor.value
  if (!e) return
  const blocks = allTopBlocks()
  if (!blocks.length) return
  const lo = Math.max(0, Math.min(anchorIndex, targetIndex, blocks.length - 1))
  const hi = Math.max(0, Math.min(Math.max(anchorIndex, targetIndex), blocks.length - 1))
  const doc = e.state.doc
  const $from = doc.resolve(Math.max(0, Math.min(blocks[lo].pos + 1, doc.content.size)))
  const $to = doc.resolve(Math.max(0, Math.min(blocks[hi].end - 1, doc.content.size)))
  e.view.dispatch(e.state.tr.setSelection(TextSelection.between($from, $to)))
}

function clearMultiSelection() {
  const e = editor.value
  if (!e) return
  const blocks = selectedTopBlocks()
  const last = blocks[blocks.length - 1]
  const pos = last ? Math.max(0, Math.min(last.end - 1, e.state.doc.content.size)) : 0
  e.view.dispatch(e.state.tr.setSelection(TextSelection.near(e.state.doc.resolve(pos))))
  blockMenu.open = false
}

/** Shift+点/拖手柄: 记录锚点块并扩展选区; 普通按下清空锚点(保留原有拖拽排序) */
function onHandleMouseDown(ev: MouseEvent) {
  if (!ev.shiftKey) {
    rangeDrag.anchorIndex = -1
    return
  }
  const r = topRange(handle.pos)
  if (!r) return
  ev.preventDefault()
  rangeDrag.active = true
  if (rangeDrag.anchorIndex < 0) rangeDrag.anchorIndex = r.index
  rangeDrag.lastIndex = r.index
  selectBlockRange(rangeDrag.anchorIndex, r.index)
}

function onDocMouseMove(ev: MouseEvent) {
  if (!rangeDrag.active) return
  const el = (document.elementFromPoint(ev.clientX, ev.clientY) as HTMLElement | null)
    ?.closest('.ProseMirror > *') as HTMLElement | null
  const e = editor.value
  if (!el || !e) return
  let pos = 0
  try { pos = e.view.posAtDOM(el, 0) } catch { return }
  const r = topRange(pos)
  if (!r || r.index === rangeDrag.lastIndex) return
  rangeDrag.lastIndex = r.index
  selectBlockRange(rangeDrag.anchorIndex, r.index)
}

function onDocMouseUp() {
  if (!rangeDrag.active) return
  rangeDrag.active = false
  syncMultiSelection()
}

/** 在正文中普通点击/选择文本: 结束多块选择语义, 清掉锚点 */
function onEditorMouseDown(ev: MouseEvent) {
  const el = ev.target as HTMLElement | null
  if (!el?.closest('.ProseMirror')) return
  if (!ev.shiftKey) rangeDrag.anchorIndex = -1
}

/** 列表缩进: TipTap 原生 sinkListItem/liftListItem(taskList 用 taskItem) */
function sinkOrLiftList(block: TopBlock, delta: number): boolean {
  const e = editor.value
  if (!e) return false
  const itemType = block.node.type.name === 'taskList' ? 'taskItem' : 'listItem'
  const chain = e.chain().focus()
  const sel = e.state.selection
  if (!(sel.from > block.pos && sel.to < block.end)) {
    const $pos = e.state.doc.resolve(Math.min(block.pos + 2, e.state.doc.content.size))
    chain.setTextSelection(TextSelection.near($pos).from)
  }
  return delta > 0 ? chain.sinkListItem(itemType).run() : chain.liftListItem(itemType).run()
}

/** 对若干顶层块应用缩进(列表原生 sink/lift, 其余包一层 indentBlock); 倒序处理保持位置有效 */
function applyIndentToBlocks(blocks: TopBlock[], delta: number): boolean {
  const e = editor.value
  if (!e || !blocks.length) return false
  if (blocks.length === 1 && isListBlock(blocks[0].node)) return sinkOrLiftList(blocks[0], delta)
  const tr = e.state.tr
  let changed = false
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (isListBlock(b.node)) continue
    if (applyBlockIndent(tr, e.state.schema, b, delta)) changed = true
  }
  if (changed) e.view.dispatch(tr)
  return changed
}

function menuIndent(delta: number) {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  blockMenu.open = false
  if (multi.active) {
    applyIndentToBlocks(selectedTopBlocks(), delta)
    return
  }
  const sel = e.state.selection
  if (!(sel.from > r.pos && sel.to < r.end)) {
    const $pos = e.state.doc.resolve(Math.min(r.pos + 1, e.state.doc.content.size))
    e.commands.setTextSelection(TextSelection.near($pos).from)
  }
  applyIndentToBlocks([r], delta)
}

async function copySelectedBlocks() {
  const e = editor.value
  if (!e) return
  const blocks = selectedTopBlocks()
  if (blocks.length < 2) return
  const slice = e.state.doc.slice(blocks[0].pos, blocks[blocks.length - 1].end)
  const docCopy = e.state.schema.topNodeType.create(null, slice.content)
  let text = ''
  try { text = e.storage.markdown.serializer.serialize(docCopy) } catch { text = blocks.map(b => b.node.textContent).join('\n\n') }
  await copyText(text.trim())
  ElMessage.success(`已复制 ${blocks.length} 个块`)
  blockMenu.open = false
}

function deleteSelectedBlocks() {
  const e = editor.value
  if (!e) return
  const blocks = selectedTopBlocks()
  if (blocks.length < 2) return
  const from = blocks[0].pos
  const to = blocks[blocks.length - 1].end
  const tr = e.state.tr
  if (from === 0 && to === e.state.doc.content.size) {
    tr.replaceWith(0, to, e.state.schema.nodes.paragraph.create())
  } else {
    tr.delete(from, to)
  }
  e.view.dispatch(tr)
  blockMenu.open = false
  handle.visible = false
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
  if (rangeDrag.active) { ev.preventDefault(); return }
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) { ev.preventDefault?.(); return }
  drag.active = true
  drag.fromIndex = r.index
  dragNode = r.node
  dragStart = r.pos
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
  { label: '缩进 / 减少缩进', keys: ['Tab / Shift', 'Tab'] },
  { label: '撤销 / 重做', keys: ['Ctrl', 'Z / Y'] },
  { label: '插入内容块', keys: ['/'] },
]

function changeBlock(kind: string) {
  const e = editor.value
  const r = topRange(handle.pos)
  if (!e || !r) return
  const chain = e.chain().focus().setTextSelection({ from: r.pos + 1, to: Math.max(r.pos + 1, r.end - 1) })
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
  if (props.collab) {
    // 协同模式内容由 Yjs 驱动; 仅在"已完成初始同步且从未播种过"时用最新 Markdown 播种
    if (collabSynced && !collabSeedDone) nextTick(() => seedCollabIfEmpty())
    return
  }
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
let mermaidObserver: LazyObserver | null = null

const scheduleMermaid = () => {
  if (mermaidTimer) clearTimeout(mermaidTimer)
  mermaidTimer = window.setTimeout(() => { renderMermaid() }, 300)
}

/** 渲染单个 mermaid 代码块(缓存命中时直接回填 SVG)。 */
const renderMermaidBlock = async (block: Element) => {
  const codeBlock = block.querySelector('code')
  if (!codeBlock) return
  const codeText = codeBlock.textContent || ''
  if (!codeText.trim()) return
  const diagramDiv = block.querySelector('.mermaid-diagram')
  const cachedSvg = mermaidCache.get(codeText)
  if (diagramDiv && cachedSvg && diagramDiv.innerHTML === cachedSvg) return
  try {
    let svg = cachedSvg
    if (!svg) {
      const id = `mermaid-${Math.random().toString(36).substr(2, 9)}`
      const rendered = await mermaid.render(id, codeText)
      svg = rendered.svg
      mermaidCache.set(codeText, svg)
    }
    let targetDiv = block.querySelector('.mermaid-diagram')
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

/**
 * 扫描 mermaid 代码块并交给 IntersectionObserver: 进入视口才 render,
 * 长文中的大量图表不再一次性全部渲染(定时器仅做扫描)。
 */
const renderMermaid = () => {
  if (mermaidTimer) {
    clearTimeout(mermaidTimer)
    mermaidTimer = null
  }
  const editorEl = document.querySelector('.ProseMirror')
  if (!editorEl) return
  if (!mermaidObserver) {
    mermaidObserver = createLazyObserver((el) => { void renderMermaidBlock(el) }, '300px 0px')
  }
  const mermaidBlocks = editorEl.querySelectorAll('.language-mermaid')
  for (const block of mermaidBlocks) {
    const codeBlock = block.querySelector('code')
    const codeText = codeBlock?.textContent || ''
    const diagramDiv = block.querySelector('.mermaid-diagram')
    const cachedSvg = mermaidCache.get(codeText)
    // 已渲染且内容未变: 跳过; 否则(重新)进入观察队列, 靠近视口时才渲染
    if (diagramDiv && cachedSvg && diagramDiv.innerHTML === cachedSvg) continue
    mermaidObserver.observe(block)
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

function handleAttachmentUpload() {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = true
  input.onchange = (e: Event) => {
    const files = (e.target as HTMLInputElement).files
    if (!files?.length) return
    Array.from(files).forEach(file => void insertAttachmentFile(file))
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
  document.addEventListener('click', closeTableMenu)
  document.addEventListener('scroll', closeTableMenu, true)
  document.addEventListener('mousemove', onDocMouseMove)
  document.addEventListener('mouseup', onDocMouseUp)
})

onBeforeUnmount(() => {
  document.removeEventListener('dragover', onDocDragOver)
  document.removeEventListener('drop', onDocDrop)
  document.removeEventListener('click', closeTableMenu)
  document.removeEventListener('scroll', closeTableMenu, true)
  document.removeEventListener('mousemove', onDocMouseMove)
  document.removeEventListener('mouseup', onDocMouseUp)
  if (mermaidTimer) {
    clearTimeout(mermaidTimer)
    mermaidTimer = null
  }
  mermaidObserver?.disconnect()
  mermaidObserver = null
  if (collabSyncTimer) {
    clearTimeout(collabSyncTimer)
    collabSyncTimer = null
  }
  provider?.awareness.off('change', emitCollabUsers)
  editor.value?.destroy()
  provider?.destroy()
  ydoc?.destroy()
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
  background: var(--primary-weak);
  color: var(--primary);
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
  background: var(--primary);
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
  color: var(--primary);
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
  caret-color: var(--primary);
}

.editor-content :deep(.ProseMirror ::selection) {
  background: var(--primary-weak-2);
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
  color: var(--primary);
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
  accent-color: var(--primary);
}

.editor-content :deep(.ProseMirror blockquote) {
  border-left: 3px solid #93c5fd;
  padding: 2px 0 2px 14px;
  color: #4b5563;
  margin: 14px 0;
  background: #f8fafc;
  border-radius: 0 6px 6px 0;
}

.editor-content :deep(.ProseMirror .callout) {
  border-radius: 10px;
  padding: 12px 16px;
  margin: 14px 0;
  border-left: 4px solid var(--primary);
  background: var(--primary-weak);
}
.editor-content :deep(.ProseMirror .callout > *:first-child) { margin-top: 0; }
.editor-content :deep(.ProseMirror .callout > *:last-child) { margin-bottom: 0; }
.editor-content :deep(.ProseMirror .callout-tip) { border-left-color: #8b5cf6; background: #f5f3ff; }
.editor-content :deep(.ProseMirror .callout-success) { border-left-color: #10b981; background: #ecfdf5; }
.editor-content :deep(.ProseMirror .callout-warn) { border-left-color: #f59e0b; background: #fffbeb; }
.editor-content :deep(.ProseMirror .callout-danger) { border-left-color: #ef4444; background: #fef2f2; }

/* 脚注引用与锚点高亮 */
.editor-content :deep(.ProseMirror sup.fn-ref) {
  cursor: pointer;
  color: var(--primary);
  background: var(--primary-weak);
  border-radius: 4px;
  padding: 0 3px;
  font-size: 0.76em;
  font-weight: 600;
  user-select: none;
}

.editor-content :deep(.ProseMirror sup.fn-ref:hover) {
  background: var(--primary-weak-2);
}

.editor-content :deep(.ProseMirror .fn-flash) {
  animation: fn-anchor-flash 1.3s ease;
}

@keyframes fn-anchor-flash {
  0%, 100% { box-shadow: 0 0 0 0 rgba(59, 130, 246, 0); }
  25%, 75% { box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.45); }
}

/* wiki-link(纯文本 [[页面#锚点]] 的装饰) */
.editor-content :deep(.ProseMirror .wiki-link) {
  color: var(--primary);
  border-bottom: 1px dashed rgba(79, 70, 229, 0.45);
  cursor: pointer;
}

.editor-content :deep(.ProseMirror .wiki-link:hover) {
  border-bottom-style: solid;
}

.editor-content :deep(.ProseMirror .wiki-link-missing) {
  color: #9ca3af;
  border-bottom-color: #d1d5db;
}

/* 跳转锚点后的标题闪烁 */
.editor-content :deep(.ProseMirror .wiki-anchor-flash) {
  animation: wiki-anchor-flash 1.6s ease;
}

@keyframes wiki-anchor-flash {
  0%, 100% { background: transparent; }
  30%, 70% { background: rgba(250, 204, 21, 0.35); }
}

/* 块缩进(容器节点, 与 Markdown HTML 兜底的 class 对应) */
.editor-content :deep(.ProseMirror .indent-block.indent-1) { padding-left: 24px; }
.editor-content :deep(.ProseMirror .indent-block.indent-2) { padding-left: 48px; }
.editor-content :deep(.ProseMirror .indent-block.indent-3) { padding-left: 72px; }
.editor-content :deep(.ProseMirror .indent-block.indent-4) { padding-left: 96px; }

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

.editor-content :deep(.ProseMirror .tableWrapper) {
  overflow-x: auto;
}

.editor-content :deep(.ProseMirror.resize-cursor) {
  cursor: col-resize;
}

.editor-content :deep(.ProseMirror th),
.editor-content :deep(.ProseMirror td) {
  border: 1px solid #e5e7eb;
  padding: 8px 12px;
  min-width: 60px;
  vertical-align: top;
  position: relative;
  height: auto;
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
  background: var(--primary);
  pointer-events: none;
}

.editor-content :deep(.ProseMirror .mermaid-diagram) {
  background: #fff;
  padding: 20px;
  border-radius: 10px;
  margin-top: 16px;
  text-align: center;
}

/* 超大表格折叠: 默认只显示表头 + 前 20 行正文, 由装饰加类 */
.editor-content :deep(.ProseMirror table.table-folded-head tr:nth-child(n+22)) {
  display: none;
}

.editor-content :deep(.ProseMirror table.table-folded:not(.table-folded-head) tr:nth-child(n+21)) {
  display: none;
}

.editor-content :deep(.ProseMirror .table-fold-toggle) {
  display: block;
  margin: 4px 0 12px;
  padding: 2px 10px;
  font-size: 12px;
  color: #2563eb;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 6px;
  cursor: pointer;
}

.editor-content :deep(.ProseMirror .table-fold-toggle:hover) {
  background: #dbeafe;
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

/* 查找/替换 */
.find-wrap {
  position: sticky;
  top: 46px;
  z-index: 19;
  height: 0;
  display: flex;
  justify-content: flex-end;
  pointer-events: none;
}

.find-bar {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 4px;
  padding: 5px 8px;
  background: #fff;
  border: 1px solid #e6e8f0;
  border-radius: 10px;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.14);
}

.find-input {
  width: 120px;
  height: 28px;
  padding: 4px 9px;
  border: 1px solid #e5e7eb;
  border-radius: 7px;
  font-size: 13px;
  color: #374151;
  outline: none;
}

.find-input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-weak);
}

.find-count {
  min-width: 62px;
  text-align: center;
  font-size: 12px;
  color: #6b7280;
}

.find-btn {
  height: 28px;
  padding: 0 9px;
  border: 1px solid #e5e7eb;
  background: #fff;
  border-radius: 7px;
  font-size: 12px;
  color: #374151;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.find-btn:hover:not(:disabled) { background: #f3f4f6; }
.find-btn:disabled { opacity: 0.4; cursor: default; }

.editor-content :deep(.find-hit) {
  background: rgba(250, 204, 21, 0.45);
  border-radius: 2px;
}

.editor-content :deep(.find-hit-current) {
  background: rgba(249, 115, 22, 0.6);
}

/* AI 编辑预览 */
.ai-preview {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  min-height: 220px;
}

.ai-col {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 1px solid #e6e8f0;
  border-radius: 10px;
  overflow: hidden;
}

.ai-col-title {
  padding: 6px 12px;
  font-size: 12px;
  color: #6b7280;
  background: #f8fafc;
  border-bottom: 1px solid #e6e8f0;
}

.ai-col-body {
  flex: 1;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.7;
  color: #1f2937;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-y: auto;
  max-height: 46vh;
}

.ai-error { color: #dc2626; }
.ai-muted { color: #9ca3af; }
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
.bubble-bar button.is-active { background: var(--primary); color: #fff; }
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

.slash-item.active { background: var(--primary-weak); }

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

.slash-item.active .slash-icon { background: var(--primary-weak-2); color: var(--primary); }

.slash-item .slash-text { display: flex; flex-direction: column; min-width: 0; }
.slash-item .slash-title { font-size: 14px; line-height: 1.3; }
.slash-item .slash-desc { font-size: 11px; color: #9ca3af; line-height: 1.3; }

.dd-icon { display: inline-block; width: 20px; }

/* wiki-link 悬浮预览(teleport 到 body, 需全局样式) */
.wiki-preview {
  max-width: 340px;
}

.wiki-preview .wiki-preview-title {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wiki-preview .wiki-preview-snippet {
  font-size: 12px;
  line-height: 1.6;
  color: #6b7280;
  max-height: 96px;
  overflow: hidden;
  word-break: break-word;
}

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
  color: var(--text-3);
  border-radius: 6px;
  cursor: grab;
}

.block-handle .handle-btn:hover { background: var(--surface-2); color: var(--text-2); }
.block-handle .handle-btn:active { cursor: grabbing; }
.block-handle .handle-btn.add { cursor: pointer; }
.block-handle .handle-btn.add:active { cursor: pointer; }

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

/* 表格右键菜单 */
.table-ctx {
  position: fixed;
  z-index: 9999;
  width: 188px;
  background: #fff;
  border: 1px solid #eceef2;
  border-radius: 10px;
  box-shadow: 0 14px 36px rgba(15, 23, 42, 0.16);
  padding: 5px;
}

.table-ctx .ctx-title {
  font-size: 11px;
  color: #9ca3af;
  padding: 5px 10px 2px;
}

.table-ctx .ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  border: none;
  background: transparent;
  border-radius: 7px;
  cursor: pointer;
  color: #374151;
  font-size: 13px;
  text-align: left;
}

.table-ctx .ctx-item:hover { background: #f3f4f6; }
.table-ctx .ctx-item.danger { color: #dc2626; }
.table-ctx .ctx-item.danger:hover { background: #fef2f2; }
.table-ctx .ctx-sep { height: 1px; background: #f0f1f4; margin: 4px 0; }

/* Yjs 协同光标 */
.collaboration-cursor__caret {
  position: relative;
  margin-left: -1px;
  margin-right: -1px;
  border-left: 1px solid #0d0d0d;
  border-right: 1px solid #0d0d0d;
  word-break: normal;
  pointer-events: none;
}

.collaboration-cursor__label {
  position: absolute;
  top: -1.5em;
  left: -1px;
  font-size: 11px;
  font-style: normal;
  font-weight: 600;
  line-height: normal;
  color: #fff;
  padding: 1px 5px;
  border-radius: 4px 4px 4px 0;
  white-space: nowrap;
  user-select: none;
}
</style>
