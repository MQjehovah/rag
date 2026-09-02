<template>
  <div class="community-view">
    <header class="community-header">
      <span>{{ communities.length }} 个主题聚类</span>
      <el-button v-if="isAdmin" type="primary" size="small" :loading="rebuilding" @click="rebuild">重建聚类</el-button>
    </header>
    <div class="community-list">
      <div v-for="c in communities" :key="c.id" class="community-item">
        <div class="community-head">
          <span class="community-title">{{ c.title }}</span>
          <span class="community-meta">
            {{ c.entity_count }} 实体 · {{ c.card_count }} 卡片
            <el-tag v-if="c.dirty" size="small" type="warning">dirty</el-tag>
            <el-tag v-if="c.algorithm" size="small" type="info">{{ c.algorithm }}</el-tag>
          </span>
        </div>
        <div v-if="c.cited_cards.length" class="community-cards">
          引用卡片：
          <el-tag
            v-for="card in c.cited_cards"
            :key="card.id"
            size="small"
            type="success"
            class="cite-chip"
            @click="openCard(card)"
          >
            {{ card.canonical_title }}
          </el-tag>
        </div>
        <div v-if="c.member_entity_ids.length" class="community-members">
          {{ c.member_entity_ids.length }} 个成员实体
        </div>
      </div>
      <div v-if="!communities.length" class="empty-tip">暂无主题聚类（需先重建）</div>
    </div>

    <CardDetailDrawer
      v-model="cardDetailVisible"
      :card-id="cardDetailId"
      revision-mode="published"
      @changed="load"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { p5GraphApi, type P5CardNode, type P5Community } from '../api/p5graph'
import { useAuthStore } from '../stores/auth'
import CardDetailDrawer from '../components/CardDetailDrawer.vue'

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.user?.groups?.includes('__local_admin__') ?? false)

const communities = ref<P5Community[]>([])
const rebuilding = ref(false)

const cardDetailVisible = ref(false)
const cardDetailId = ref('')

function openCard(card: P5CardNode) {
  cardDetailId.value = card.id
  cardDetailVisible.value = true
}

async function load() {
  try {
    communities.value = await p5GraphApi.communities()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载主题聚类失败')
  }
}

async function rebuild() {
  rebuilding.value = true
  try {
    const res = await p5GraphApi.rebuild()
    ElMessage.success(res.message)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重建失败')
  } finally {
    rebuilding.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.community-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow: hidden;
}
.community-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: #6b7280;
}
.community-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.community-item {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
}
.community-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.community-title {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}
.community-meta {
  font-size: 12px;
  color: #6b7280;
  display: flex;
  gap: 6px;
  align-items: center;
}
.community-cards {
  margin-top: 8px;
  font-size: 12px;
  color: #6b7280;
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  align-items: center;
}
.community-members {
  margin-top: 6px;
  font-size: 12px;
  color: #9ca3af;
}
.cite-chip {
  cursor: pointer;
  transition: all 0.15s;
}
.cite-chip:hover {
  border-color: #16a34a;
  background: #dcfce7;
  text-decoration: underline;
}
.empty-tip {
  font-size: 13px;
  color: #9ca3af;
  padding: 24px;
  text-align: center;
}
</style>
