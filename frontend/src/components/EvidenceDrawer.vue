<template>
  <el-drawer
    v-model="visible"
    :title="title"
    size="520px"
    direction="rtl"
    @closed="onClosed"
  >
    <div v-loading="loading" class="evidence-drawer-body">
      <div v-if="!loading && evidences.length === 0" class="empty-state">
        该来源暂无 Evidence 记录
      </div>

      <div v-for="ev in evidences" :key="ev.id" class="evidence-item">
        <!-- 定位信息 -->
        <div class="evidence-locator">
          <el-tag size="small" :type="typeTag(ev.evidence_type)">{{ ev.evidence_type }}</el-tag>
          <span v-if="ev.locator.page_number" class="locator-chip">第 {{ ev.locator.page_number }} 页</span>
          <span v-if="ev.locator.heading" class="locator-chip locator-heading">{{ ev.locator.heading }}</span>
          <el-tag v-if="ev.status !== 'active'" size="small" :type="ev.status === 'stale' ? 'warning' : 'danger'">
            {{ ev.status === 'stale' ? '已过期' : '已否决' }}
          </el-tag>
          <el-tag v-if="ev.needs_review" size="small" type="warning" effect="dark">待复核</el-tag>
        </div>

        <!-- 原文 -->
        <div class="evidence-content">
          <MarkdownPreview :content="ev.content" />
        </div>

        <!-- 图片：从 locator 或关联 Observation 提取 image_url -->
        <div v-if="imageUrls(ev).length" class="evidence-images">
          <el-image
            v-for="(url, i) in imageUrls(ev)"
            :key="i"
            :src="url"
            :preview-src-list="imageUrls(ev)"
            fit="contain"
            class="evidence-image"
          />
        </div>

        <!-- 关联 Observation（OCR / manual） -->
        <div v-if="observationsOf(ev).length" class="evidence-observations">
          <div
            v-for="obs in observationsOf(ev)"
            :key="obs.id"
            class="observation-row"
          >
            <div class="observation-head">
              <el-tag size="small" type="info">{{ obs.observation_type }}</el-tag>
              <el-tag v-if="obs.inferred" size="small" type="warning">推断 inferred=true</el-tag>
              <el-tag v-if="obs.needs_review" size="small" type="warning">待人工描述</el-tag>
            </div>
            <div class="observation-content">{{ obs.content }}</div>
            <el-button
              v-if="isAdmin && obs.needs_review"
              size="small"
              type="primary"
              text
              @click="openManualForm(obs.asset_id, obs.observation_type)"
            >补充功能说明</el-button>
          </div>
        </div>

        <!-- 来源 -->
        <div class="evidence-source">
          来源：{{ ev.source.title || '（无标题）' }}
          <span v-if="ev.source.page_id" class="source-id">page_id={{ ev.source.page_id }}</span>
        </div>
      </div>
    </div>

    <!-- 管理员补充 manual 说明对话框（P1-FE-04） -->
    <el-dialog
      v-model="manualForm.visible"
      title="补充图片功能说明"
      width="480px"
      append-to-body
    >
      <el-input
        v-model="manualForm.content"
        type="textarea"
        :rows="4"
        placeholder="描述该图片的业务功能，例如：展示水箱的安装位置与固定方式"
      />
      <template #footer>
        <el-button @click="manualForm.visible = false">取消</el-button>
        <el-button type="primary" :loading="manualForm.saving" @click="submitManual">保存</el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { evidenceApi, type EvidenceItem, type Observation } from '../api/evidence'
import { useAuthStore } from '../stores/auth'
import MarkdownPreview from './MarkdownPreview.vue'

const props = defineProps<{
  modelValue: boolean
  pageId: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.user?.groups?.includes('__local_admin__') ?? false)

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const title = computed(() => `证据溯源${props.pageId ? '' : ''}`)
const loading = ref(false)
const evidences = ref<EvidenceItem[]>([])
const detailCache = ref<Record<string, Observation[]>>({})

const manualForm = reactive({
  visible: false,
  assetId: '',
  content: '',
  saving: false,
})

function typeTag(type: string): 'primary' | 'success' | 'warning' | 'info' {
  if (type === 'table') return 'success'
  if (type === 'image_observation') return 'warning'
  if (type === 'text') return 'primary'
  return 'info'
}

// 图片 URL：优先 locator.image_url，其次关联 Observation 的 image_url
function imageUrls(ev: EvidenceItem): string[] {
  const urls: string[] = []
  if (ev.locator.image_url) urls.push(ev.locator.image_url)
  for (const obs of observationsOf(ev)) {
    if (obs.locator.image_url && !urls.includes(obs.locator.image_url)) {
      urls.push(obs.locator.image_url)
    }
  }
  return urls
}

function observationsOf(ev: EvidenceItem): Observation[] {
  // 优先详情缓存；否则从 evidence 自带的 observations（detail 场景）
  return detailCache.value[ev.id] || (ev as any).observations || []
}

async function load() {
  if (!props.pageId) return
  loading.value = true
  try {
    evidences.value = await evidenceApi.byPage(props.pageId)
    // 逐条拉详情以获取关联 Observation（图片 OCR/manual）
    detailCache.value = {}
    for (const ev of evidences.value) {
      try {
        const detail = await evidenceApi.detail(ev.id)
        detailCache.value[ev.id] = detail.observations || []
      } catch {
        detailCache.value[ev.id] = []
      }
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载 Evidence 失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.pageId],
  ([open, _pid]) => {
    if (open) load()
  },
  { immediate: true },
)

function onClosed() {
  evidences.value = []
  detailCache.value = {}
}

function openManualForm(assetId: string, _obsType: string) {
  manualForm.assetId = assetId
  manualForm.content = ''
  manualForm.visible = true
}

async function submitManual() {
  if (!manualForm.content.trim()) {
    ElMessage.warning('请输入功能说明')
    return
  }
  manualForm.saving = true
  try {
    await evidenceApi.upsertManualObservation(manualForm.assetId, manualForm.content.trim())
    ElMessage.success('功能说明已保存')
    manualForm.visible = false
    await load() // 重新加载，manual Observation 生效
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    manualForm.saving = false
  }
}
</script>

<style scoped>
.evidence-drawer-body {
  min-height: 200px;
}
.empty-state {
  text-align: center;
  color: #9ca3af;
  padding: 40px 0;
}
.evidence-item {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px;
  margin-bottom: 14px;
  background: #fff;
}
.evidence-locator {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.locator-chip {
  font-size: 12px;
  color: #6b7280;
  background: #f3f4f6;
  padding: 2px 8px;
  border-radius: 6px;
}
.locator-heading {
  color: #374151;
  font-weight: 500;
}
.evidence-content {
  font-size: 13px;
  color: #374151;
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid #f3f4f6;
  border-radius: 8px;
  padding: 10px;
  background: #fafafa;
}
.evidence-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.evidence-image {
  width: 120px;
  height: 90px;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
}
.evidence-observations {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.observation-row {
  border-left: 3px solid #3b82f6;
  background: #f8fafc;
  padding: 8px 10px;
  border-radius: 6px;
}
.observation-head {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}
.observation-content {
  font-size: 12px;
  color: #4b5563;
  white-space: pre-wrap;
}
.evidence-source {
  margin-top: 10px;
  font-size: 12px;
  color: #9ca3af;
}
.source-id {
  margin-left: 6px;
  font-family: monospace;
  font-size: 11px;
}
</style>
