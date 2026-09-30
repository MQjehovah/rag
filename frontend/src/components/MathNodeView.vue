<template>
  <node-view-wrapper
    :as="isBlock ? 'div' : 'span'"
    class="math-node"
    :class="[isBlock ? 'math-node-block' : 'math-node-inline', { selected, 'is-empty': !latex.trim(), 'is-error': !!error }]"
    :data-latex="latex"
    :title="error ? `公式语法错误：${error}` : '双击编辑公式'"
    @dblclick.stop.prevent="edit"
  >
    <span v-if="isBlock" class="math-tools" contenteditable="false">
      <span class="math-kind">公式</span>
      <button class="math-edit" title="编辑 LaTeX 源码" @mousedown.stop.prevent @click.stop.prevent="edit">
        <Pencil :size="13" />
      </button>
    </span>
    <span v-if="error" class="math-error">{{ latex || '空公式' }}</span>
    <span v-else-if="!latex.trim()" class="math-empty">双击编辑公式</span>
    <span v-else class="math-render" v-html="rendered" />
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NodeViewWrapper, nodeViewProps } from '@tiptap/vue-3'
import { ElMessageBox } from 'element-plus'
import { Pencil } from 'lucide-vue-next'
import katex from 'katex'

const props = defineProps(nodeViewProps)

const isBlock = computed(() => props.node.type.name === 'mathBlock')
const latex = computed(() => String(props.node.attrs.latex ?? ''))

const result = computed<{ html: string; error: string }>(() => {
  const src = latex.value
  if (!src.trim()) return { html: '', error: '' }
  try {
    return {
      html: katex.renderToString(src, {
        displayMode: isBlock.value,
        throwOnError: true,
        output: 'htmlAndMathml',
      }),
      error: '',
    }
  } catch (err: any) {
    return { html: '', error: String(err?.message || '语法错误') }
  }
})

const rendered = computed(() => result.value.html)
const error = computed(() => result.value.error)

async function edit() {
  try {
    const res = await ElMessageBox.prompt(
      isBlock.value ? '编辑块级公式（LaTeX，无需 $$ 定界符）' : '编辑行内公式（LaTeX，无需 $ 定界符）',
      '公式',
      {
        inputValue: latex.value,
        inputType: 'textarea',
        inputPlaceholder: '例如：E = mc^2',
        confirmButtonText: '确定',
        cancelButtonText: '取消',
      },
    )
    props.updateAttributes?.({ latex: String(res.value ?? '') })
  } catch {
    /* 取消编辑 */
  }
}
</script>

<style scoped>
.math-node {
  cursor: pointer;
  border-radius: 6px;
  transition: background 0.15s, box-shadow 0.15s;
}

.math-node-inline {
  display: inline;
  padding: 1px 3px;
}

.math-node-inline:hover,
.math-node-inline.selected {
  background: #eef4ff;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.25);
}

.math-node-block {
  display: block;
  margin: 14px 0;
  padding: 10px 14px;
  background: #fafbfc;
  border: 1px solid #eef1f5;
  text-align: center;
  overflow-x: auto;
}

.math-node-block:hover,
.math-node-block.selected {
  border-color: #bfdbfe;
  background: #f8fbff;
}

.math-tools {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
  user-select: none;
}

.math-kind {
  font-size: 11px;
  color: #94a3b8;
  letter-spacing: 0.04em;
}

.math-edit {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fff;
  color: #64748b;
  cursor: pointer;
}

.math-edit:hover {
  color: #2563eb;
  border-color: #bfdbfe;
}

.math-render {
  cursor: pointer;
}

.math-empty {
  color: #a8b0bd;
  font-size: 14px;
  font-style: italic;
}

.math-error {
  color: #dc2626;
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 0.92em;
  word-break: break-all;
}

.math-node-block.is-error {
  border-color: #fecaca;
  background: #fff7f7;
}
</style>
