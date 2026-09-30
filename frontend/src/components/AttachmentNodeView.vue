<template>
  <node-view-wrapper as="div" class="attach-node" :class="{ selected, uploading: node.attrs.uploading }">
    <div
      class="attach-card"
      :title="node.attrs.uploading ? '附件上传中…' : node.attrs.name"
      @click="onCardClick"
    >
      <span class="attach-icon">📎</span>
      <span class="attach-meta">
        <span class="attach-name">{{ node.attrs.name || '未命名附件' }}</span>
        <span class="attach-sub">
          <template v-if="node.attrs.uploading">上传中…</template>
          <template v-else>{{ sizeText || '未知大小' }}<template v-if="typeText"> · {{ typeText }}</template></template>
        </span>
      </span>
      <span v-if="node.attrs.uploading" class="attach-spinner" />
      <span v-else class="attach-action">{{ canPreview ? '打开' : '下载' }}</span>
    </div>
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NodeViewWrapper, nodeViewProps } from '@tiptap/vue-3'
import { ElMessage } from 'element-plus'
import { canPreviewAttachment, downloadAttachment, formatBytes, openAttachment } from '../utils/attachment'

const props = defineProps(nodeViewProps)

const canPreview = computed(() => canPreviewAttachment(props.node.attrs.mime, props.node.attrs.name))
const sizeText = computed(() => formatBytes(Number(props.node.attrs.size) || 0))
const typeText = computed(() => {
  const mime = String(props.node.attrs.mime || '')
  const sub = mime.split('/').pop() || ''
  if (sub && sub !== 'octet-stream') return sub.toUpperCase()
  const ext = String(props.node.attrs.name || '').split('.').pop() || ''
  return ext && ext !== props.node.attrs.name ? ext.toUpperCase() : ''
})

async function onCardClick() {
  if (props.node.attrs.uploading || !props.node.attrs.url) return
  try {
    if (canPreview.value) await openAttachment(props.node.attrs.url)
    else await downloadAttachment(props.node.attrs.url, props.node.attrs.name)
  } catch {
    ElMessage.error('附件打开失败，请稍后重试')
  }
}
</script>

<style scoped>
.attach-node {
  margin: 6px 0;
  max-width: 100%;
}
.attach-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fafbfc;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  user-select: none;
}
.attach-card:hover {
  border-color: #bfdbfe;
  background: #f4f8ff;
  box-shadow: 0 2px 10px rgba(59, 130, 246, 0.1);
}
.attach-node.selected .attach-card {
  outline: 2px solid #3b82f6;
  outline-offset: 1px;
}
.attach-node.uploading .attach-card {
  cursor: default;
  border-style: dashed;
  opacity: 0.85;
}
.attach-icon {
  font-size: 20px;
  line-height: 1;
  flex-shrink: 0;
}
.attach-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.attach-name {
  font-size: 13px;
  color: #1f2937;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attach-sub {
  font-size: 12px;
  color: #94a3b8;
}
.attach-action {
  flex-shrink: 0;
  padding: 4px 12px;
  border: 1px solid #dbeafe;
  border-radius: 7px;
  background: #fff;
  color: #2563eb;
  font-size: 12px;
}
.attach-card:hover .attach-action {
  background: #eff6ff;
}
.attach-spinner {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  border: 2px solid #dbeafe;
  border-top-color: #3b82f6;
  border-radius: 50%;
  animation: attach-spin 0.8s linear infinite;
}
@keyframes attach-spin {
  to { transform: rotate(360deg); }
}
</style>
