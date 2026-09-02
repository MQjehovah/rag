<template>
  <div class="debt-v4-container">
    <el-tabs v-model="activeTab">
      <el-tab-pane label="知识缺口" name="debts">
        <div v-if="loading" v-loading="true" style="min-height: 200px"></div>
        <div v-else-if="!displayDebts.length" class="placeholder">当前范围无知识缺口记录</div>
        <div v-else class="debt-v4-list">
          <div v-for="(row, i) in displayDebts" :key="i" class="debt-v4-item">
            <div class="debt-v4-title">{{ row.title }}</div>
            <el-tag :type="row.status === 'open' ? 'danger' : 'success'" size="small">
              {{ row.status === 'open' ? '待处理' : '已解决' }}
            </el-tag>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane v-if="isAdmin" label="访问申请" name="access">
        <div v-if="accessLoading" v-loading="true" style="min-height: 200px"></div>
        <div v-else-if="!accessRequests.length" class="placeholder">暂无访问申请</div>
        <div v-else class="debt-v4-list">
          <div v-for="req in accessRequests" :key="req.id" class="debt-v4-item">
            <div class="debt-v4-title">{{ requestTitle(req) }}</div>
            <el-tag :type="req.status === 'open' ? 'danger' : 'success'" size="small">
              {{ req.status === 'open' ? '待处理' : '已解决' }}
            </el-tag>
            <el-button v-if="req.status === 'open'" size="small" text type="primary" @click="goSetupPermission(req)">
              去设置权限
            </el-button>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { debtsV4Api, type AccessRequestV4, type DebtV4 } from '../api/debtsV4'

const router = useRouter()
const auth = useAuthStore()

const activeTab = ref('debts')
const debts = ref<DebtV4[]>([])
const accessRequests = ref<AccessRequestV4[]>([])
const loading = ref(false)
const accessLoading = ref(false)

const isAdmin = computed(() => auth.user?.is_admin === true)

// J-4：展示层折叠完全相同标题、隐藏空问题，不修改真实数据。
const displayDebts = computed(() => {
  const seen = new Set<string>()
  const out: DebtV4[] = []
  for (const d of debts.value) {
    const title = (d.title || '').trim()
    if (!title) continue
    if (seen.has(title)) continue
    seen.add(title)
    out.push({ ...d, title })
  }
  return out
})

const load = async () => {
  loading.value = true
  try {
    debts.value = await debtsV4Api.list({ status: 'open' })
  } finally {
    loading.value = false
  }
}

const loadAccess = async () => {
  if (!isAdmin.value) return
  accessLoading.value = true
  try {
    accessRequests.value = await debtsV4Api.listAccessRequests({ status: 'open' })
  } finally {
    accessLoading.value = false
  }
}

const goSetupPermission = (req: AccessRequestV4) => {
  // 跳转到对应 Notebook 权限设置位置（数据源页的 Notebook 权限区），
  // 而非永远只打开数据源首页。
  router.push({
    path: '/sources',
    query: req.target_notebook_id ? { notebook: req.target_notebook_id } : {},
  })
}

// J-4：company / 单组 / 多组访问申请文案。
function requestTitle(req: AccessRequestV4): string {
  const groups = req.groups || []
  const label = groups.length ? groups.map((g) => `${g}组`).join('、') : '全公司'
  return `${label}需要 ${req.original_query} 相关资料`
}

onMounted(() => {
  load()
  loadAccess()
})
</script>

<style scoped>
.debt-v4-container {
  padding: 16px;
  max-width: 900px;
  margin: 0 auto;
}
.debt-v4-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.debt-v4-item {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.debt-v4-title {
  font-weight: 600;
  flex: 1;
}
.placeholder {
  color: #909399;
  text-align: center;
  padding: 40px 0;
}
</style>
