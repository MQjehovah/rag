<template>
  <node-view-wrapper class="code-block">
    <select contenteditable="false" @change="handleLanguageChange">
      <option disabled :selected="!node.attrs.language">选择语言</option>
      <option value="javascript" :selected="node.attrs.language === 'javascript'">JavaScript</option>
      <option value="typescript" :selected="node.attrs.language === 'typescript'">TypeScript</option>
      <option value="c" :selected="node.attrs.language === 'c'">C</option>
      <option value="python" :selected="node.attrs.language === 'python'">Python</option>
      <option value="java" :selected="node.attrs.language === 'java'">Java</option>
      <option value="go" :selected="node.attrs.language === 'go'">Go</option>
      <option value="rust" :selected="node.attrs.language === 'rust'">Rust</option>
      <option value="sql" :selected="node.attrs.language === 'sql'">SQL</option>
      <option value="bash" :selected="node.attrs.language === 'bash'">Bash</option>
      <option value="json" :selected="node.attrs.language === 'json'">JSON</option>
      <option value="yaml" :selected="node.attrs.language === 'yaml'">YAML</option>
      <option value="html" :selected="node.attrs.language === 'html'">HTML</option>
      <option value="css" :selected="node.attrs.language === 'css'">CSS</option>
      <option value="plaintext" :selected="node.attrs.language === 'plaintext'">Plain Text</option>
    </select>
    <pre><code><node-view-content as="code" /></code></pre>
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { NodeViewWrapper, NodeViewContent, nodeViewProps } from '@tiptap/vue-3'

const props = defineProps(nodeViewProps)

const handleLanguageChange = (event: Event) => {
  const language = (event.target as HTMLSelectElement).value
  props.updateAttributes?.({ language })
}
</script>

<style scoped>
.code-block {
  position: relative;
  background: #282c34;
  border-radius: 8px;
  padding: 16px 20px;
  margin: 16px 0;
  font-family: 'Fira Code', 'Consolas', monospace;
}

.code-block select {
  position: absolute;
  top: 8px;
  right: 8px;
  background: #3e4451;
  border: none;
  color: #abb2bf;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}

.code-block pre {
  margin: 0;
  padding: 0;
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
</style>