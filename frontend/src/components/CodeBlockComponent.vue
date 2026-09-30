<template>
  <node-view-wrapper class="code-block" :class="[`language-${node.attrs.language || 'plaintext'}`, { 'is-collapsed': collapsed }]">
    <div class="code-head" contenteditable="false">
      <select class="lang-select" @change="handleLanguageChange" :value="node.attrs.language || 'plaintext'">
        <option value="plaintext">Plain Text</option>
        <option value="javascript">JavaScript</option>
        <option value="typescript">TypeScript</option>
        <option value="python">Python</option>
        <option value="java">Java</option>
        <option value="go">Go</option>
        <option value="rust">Rust</option>
        <option value="c">C</option>
        <option value="cpp">C++</option>
        <option value="sql">SQL</option>
        <option value="bash">Bash</option>
        <option value="json">JSON</option>
        <option value="yaml">YAML</option>
        <option value="xml">XML</option>
        <option value="html">HTML</option>
        <option value="css">CSS</option>
        <option value="markdown">Markdown</option>
        <option value="mermaid">Mermaid</option>
      </select>
      <button class="copy-btn" type="button" @click="copyCode" :title="copied ? '已复制' : '复制代码'">
        {{ copied ? '已复制' : '复制' }}
      </button>
    </div>
    <pre spellcheck="false" :class="{ 'is-collapsed-pre': collapsed }"><code spellcheck="false"><node-view-content as="code" /></code></pre>
    <div v-if="longCode" class="code-fold" contenteditable="false">
      <button class="fold-btn" type="button" @click="expanded = !expanded">
        {{ expanded ? '收起' : `展开全部（共 ${lineCount} 行）` }}
      </button>
    </div>
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { NodeViewWrapper, NodeViewContent, nodeViewProps } from '@tiptap/vue-3'

const props = defineProps(nodeViewProps)
const copied = ref(false)

/** 超长代码块默认折叠, 点击展开(仅展示层, 不影响内容与复制)。 */
const COLLAPSE_LINE_LIMIT = 200
const expanded = ref(false)
const lineCount = computed(() => (props.node.textContent || '').split('\n').length)
const longCode = computed(() => lineCount.value > COLLAPSE_LINE_LIMIT)
const collapsed = computed(() => longCode.value && !expanded.value)

const handleLanguageChange = (event: Event) => {
  const language = (event.target as HTMLSelectElement).value
  props.updateAttributes?.({ language })
}

async function copyCode() {
  const text = (props.node.textContent || '')
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
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1500)
}
</script>

<style scoped>
.code-block {
  position: relative;
  background: #282c34;
  border-radius: 10px;
  padding: 0;
  margin: 16px 0;
  font-family: 'Fira Code', 'Consolas', monospace;
  overflow: hidden;
}

.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: #21252b;
  border-bottom: 1px solid #1b1f24;
}

.lang-select {
  background: #3e4451;
  border: none;
  color: #abb2bf;
  padding: 3px 8px;
  border-radius: 5px;
  font-size: 12px;
  cursor: pointer;
  outline: none;
}

.copy-btn {
  background: #3e4451;
  border: none;
  color: #abb2bf;
  padding: 3px 10px;
  border-radius: 5px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.copy-btn:hover {
  background: #4b5263;
}

.code-block pre {
  margin: 0;
  padding: 16px 20px;
  background: transparent;
  color: #abb2bf;
  font-size: 14px;
  line-height: 1.6;
  overflow-x: auto;
}

.code-block code {
  background: transparent;
  padding: 0;
}

/* 超长代码块折叠(>200 行): 默认截断 + 「展开全部」 */
.code-block.is-collapsed .is-collapsed-pre {
  max-height: 320px;
  overflow: hidden;
}

.code-fold {
  border-top: 1px solid #1b1f24;
  background: #21252b;
  text-align: center;
  padding: 4px 0;
}

.fold-btn {
  background: transparent;
  border: none;
  color: #89b4fa;
  font-size: 12px;
  cursor: pointer;
  padding: 2px 10px;
}

.fold-btn:hover {
  color: #a6c8ff;
  text-decoration: underline;
}
</style>
