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
      <span v-else class="attach-action">{{ previewKind === 'none' ? '下载' : '预览' }}</span>
    </div>

    <el-dialog
      v-model="previewOpen"
      :title="node.attrs.name || '附件预览'"
      width="min(920px, 94vw)"
      append-to-body
      destroy-on-close
      class="attach-preview-dialog"
      @closed="onPreviewClosed"
    >
      <div v-if="previewKind === 'image'" class="attach-preview-stage" @click.self="previewOpen = false">
        <img
          v-if="previewUrl"
          :src="previewUrl"
          :alt="node.attrs.name"
          class="attach-preview-image"
          @error="onPreviewError"
        />
        <span v-else class="attach-preview-loading">加载中…</span>
      </div>
      <div v-else class="attach-preview-stage">
        <iframe
          v-if="previewUrl"
          :src="previewUrl"
          class="attach-preview-frame"
          title="PDF 预览"
          @error="onPreviewError"
        />
        <span v-else class="attach-preview-loading">加载中…</span>
      </div>
      <template #footer>
        <el-button @click="previewOpen = false">关闭</el-button>
        <el-button type="primary" @click="onDownload">下载</el-button>
      </template>
    </el-dialog>
  </node-view-wrapper>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { NodeViewWrapper, nodeViewProps } from '@tiptap/vue-3'
import { ElMessage } from 'element-plus'
import {
  attachmentPreviewKind,
  downloadAttachment,
  formatBytes,
  resolveAttachmentUrlForPreview,
} from '../utils/attachment'

const props = defineProps(nodeViewProps)

const previewKind = computed(() =>
  attachmentPreviewKind(String(props.node.attrs.mime || ''), String(props.node.attrs.name || '')),
)
const sizeText = computed(() => formatBytes(Number(props.node.attrs.size) || 0))
const typeText = computed(() => {
  const mime = String(props.node.attrs.mime || '')
  const sub = mime.split('/').pop() || ''
  if (sub && sub !== 'octet-stream') return sub.toUpperCase()
  const ext = String(props.node.attrs.name || '').split('.').pop() || ''
  return ext && ext !== props.node.attrs.name ? ext.toUpperCase() : ''
})

const previewOpen = ref(false)
const previewUrl = ref('')
// 签名过期/加载失败时只允许重新签名重试一次,避免 403 死循环
let previewRetried = false

async function onCardClick() {
  if (props.node.attrs.uploading || !props.node.attrs.url) return
  if (previewKind.value === 'none') {
    try {
      await downloadAttachment(props.node.attrs.url, props.node.attrs.name)
    } catch {
      ElMessage.error('附件下载失败，请稍后重试')
    }
    return
  }
  previewOpen.value = true
  previewRetried = false
  previewUrl.value = ''
  try {
    previewUrl.value = await resolveAttachmentUrlForPreview(props.node.attrs.url)
  } catch {
    previewOpen.value = false
    ElMessage.error('附件预览失败，请稍后重试')
  }
}

async function onPreviewError() {
  if (previewRetried || !props.node.attrs.url) return
  previewRetried = true
  try {
    previewUrl.value = await resolveAttachmentUrlForPreview(props.node.attrs.url)
  } catch {
    ElMessage.error('附件预览失败，请稍后重试')
  }
}

async function onDownload() {
  try {
    await downloadAttachment(props.node.attrs.url, props.node.attrs.name)
  } catch {
    ElMessage.error('附件下载失败，请稍后重试')
  }
}

function onPreviewClosed() {
  previewUrl.value = ''
  previewRetried = false
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
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  user-select: none;
}
.attach-card:hover {
  border-color: var(--primary);
  background: var(--surface-hover);
  box-shadow: var(--shadow-sm);
}
.attach-node.selected .attach-card {
  outline: 2px solid var(--primary);
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
  color: var(--text);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attach-sub {
  font-size: 12px;
  color: var(--text-3);
}
.attach-action {
  flex-shrink: 0;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: var(--primary);
  font-size: 12px;
}
.attach-card:hover .attach-action {
  background: var(--surface-hover);
  border-color: var(--primary);
}
.attach-spinner {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: attach-spin 0.8s linear infinite;
}
@keyframes attach-spin {
  to { transform: rotate(360deg); }
}

/* 内联预览器:图片缩放适配 / PDF iframe 直连,配色走主题变量 */
.attach-preview-stage {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
  max-height: 70vh;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
}
.attach-preview-image {
  display: block;
  max-width: 100%;
  max-height: 70vh;
  object-fit: contain;
}
.attach-preview-frame {
  width: 100%;
  height: 70vh;
  border: 0;
  border-radius: 8px;
  background: var(--surface);
}
.attach-preview-loading {
  padding: 48px 0;
  color: var(--text-3);
  font-size: 13px;
}
</style>
