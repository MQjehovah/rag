<template>
  <node-view-wrapper as="div" class="footnotes-block">
    <div class="fn-head" contenteditable="false">
      <span class="fn-title">脚注</span>
      <span class="fn-hint">删除引用不会自动清理条目，可手动编辑/删除</span>
      <button class="fn-add" title="新增脚注条目" @mousedown.stop.prevent @click.stop.prevent="addItem">＋ 条目</button>
    </div>
    <node-view-content class="fn-list" />
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { NodeViewWrapper, NodeViewContent, nodeViewProps } from '@tiptap/vue-3'
import { nextFootnoteLabel } from '../utils/markdownFootnotes'

const props = defineProps(nodeViewProps)

function addItem() {
  const editor = props.editor
  const pos = props.getPos?.()
  if (!editor || typeof pos !== 'number') return
  const label = nextFootnoteLabel(editor.state.doc)
  const insertPos = pos + props.node.nodeSize - 1
  editor.chain().insertContentAt(insertPos, {
    type: 'footnoteItem',
    attrs: { label },
    content: [],
  }).focus(insertPos + 1).run()
}
</script>

<style scoped>
.footnotes-block {
  margin: 22px 0 8px;
  padding: 12px 14px 6px;
  border-top: 1px solid #e8ebf0;
  background: #fafbfc;
  border-radius: 10px;
}

.fn-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
  user-select: none;
}

.fn-title {
  font-size: 12px;
  font-weight: 700;
  color: #64748b;
  letter-spacing: 0.08em;
}

.fn-hint {
  flex: 1 1 auto;
  font-size: 11px;
  color: #a8b0bd;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fn-add {
  flex: 0 0 auto;
  padding: 3px 10px;
  border: 1px solid #dbeafe;
  border-radius: 7px;
  background: #fff;
  color: #2563eb;
  font-size: 12px;
  cursor: pointer;
}

.fn-add:hover {
  background: #eff6ff;
}

.fn-list {
  font-size: 0.94em;
}
</style>
