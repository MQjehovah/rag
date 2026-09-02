<template>
  <div class="graph-hub">
    <header class="graph-toolbar">
      <el-input
        v-model="searchText"
        placeholder="搜索实体..."
        clearable
        style="width: 200px"
        @keyup.enter="load"
        @clear="load"
      />
      <el-select v-model="entityType" placeholder="实体类型" clearable style="width: 140px" @change="load">
        <el-option v-for="t in facets.entity_types" :key="t" :label="t" :value="t" />
      </el-select>
      <el-select v-model="versionFilter" placeholder="版本" clearable style="width: 120px" @change="load">
        <el-option v-for="v in facets.versions" :key="v" :label="v" :value="v" />
      </el-select>
      <el-select v-model="communityFilter" placeholder="分组" clearable style="width: 150px" @change="load">
        <el-option v-for="c in facets.communities" :key="c.key" :label="c.display_name" :value="c.key" />
      </el-select>
      <el-button type="primary" @click="load" :loading="loading">查询</el-button>
      <span v-if="truncated" class="truncated-hint">结果已截断（后端上限）</span>
    </header>

    <div class="graph-body">
      <div class="graph-canvas-wrap">
        <V4GraphCanvas :nodes="nodes" :edges="edges" @select="onSelectNode" />
        <div v-if="error" class="error-tip">{{ error }}</div>
        <div v-else-if="!loading && !nodes.length" class="empty-tip">暂无实体关系</div>
      </div>

      <aside v-if="selectedNode" class="node-detail">
        <div class="detail-head">
          <h3 class="detail-title">{{ selectedNode.display_name }}</h3>
          <el-tag size="small">{{ selectedNode.entity_type }}</el-tag>
          <el-tag v-if="selectedNode.version_status" size="small" type="info">{{ selectedNode.version_status }}</el-tag>
        </div>
        <div v-if="selectedNode.community" class="detail-community">
          所属分组：{{ selectedNode.community.display_name }}
        </div>
        <div v-if="selectedNode.related_wiki?.length" class="detail-wikis">
          <h4 class="section-title">相关 Wiki</h4>
          <div
            v-for="w in selectedNode.related_wiki"
            :key="w.id"
            class="wiki-item"
            @click="goWiki(w.id)"
          >
            {{ w.title }}
          </div>
        </div>
        <div v-else class="detail-empty">无关联 Wiki</div>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { graphApi, type V4GraphNode, type V4GraphEdge, type V4Facets } from '../api/graph'
import V4GraphCanvas from '../components/graph/V4GraphCanvas.vue'

const router = useRouter()
const loading = ref(false)
const error = ref('')
const nodes = ref<V4GraphNode[]>([])
const edges = ref<V4GraphEdge[]>([])
const selectedNode = ref<V4GraphNode | null>(null)
const truncated = ref(false)
const facets = ref<V4Facets>({ versions: [], entity_types: [], communities: [] })

const searchText = ref('')
const entityType = ref('')
const versionFilter = ref('')
const communityFilter = ref('')

async function loadFacets() {
  try {
    facets.value = await graphApi.facets()
  } catch {
    facets.value = { versions: [], entity_types: [], communities: [] }
  }
}

async function load() {
  loading.value = true
  error.value = ''
  selectedNode.value = null
  try {
    const res = await graphApi.subgraph({
      q: searchText.value || undefined,
      entity_type: entityType.value || undefined,
      version: versionFilter.value || undefined,
      community: communityFilter.value || undefined,
      depth: 2,
      limit: 100,
    })
    nodes.value = res.nodes
    edges.value = res.edges
    truncated.value = res.truncated
  } catch {
    error.value = '图谱加载失败'
    nodes.value = []
    edges.value = []
  } finally {
    loading.value = false
  }
}

function onSelectNode(node: V4GraphNode) {
  selectedNode.value = node
  focusNode(node.id)
}

async function focusNode(nodeId: string) {
  loading.value = true
  error.value = ''
  try {
    // 点击节点聚焦：保留当前 version/entity_type/community 筛选。
    const res = await graphApi.subgraph({
      focus: nodeId,
      depth: 1,
      limit: 60,
      entity_type: entityType.value || undefined,
      version: versionFilter.value || undefined,
      community: communityFilter.value || undefined,
    })
    nodes.value = res.nodes
    edges.value = res.edges
    truncated.value = res.truncated
  } catch {
    error.value = '聚焦加载失败'
  } finally {
    loading.value = false
  }
}

function goWiki(id: string) {
  router.push(`/knowledge/wiki/${id}`)
}

onMounted(async () => {
  await loadFacets()
  await load()
})
</script>

<style scoped>
.graph-hub {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow: hidden;
  padding: 16px 24px;
}
.graph-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.truncated-hint {
  font-size: 12px;
  color: #b45309;
}
.graph-body {
  flex: 1;
  display: flex;
  gap: 12px;
  overflow: hidden;
  min-height: 0;
}
.graph-canvas-wrap {
  flex: 1;
  position: relative;
  min-width: 0;
}
.error-tip {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  background: #fef2f2;
  border: 1px solid #fca5a5;
  color: #b91c1c;
  font-size: 12px;
  padding: 6px 14px;
  border-radius: 8px;
}
.empty-tip {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  color: #9ca3af;
  font-size: 13px;
}
.node-detail {
  width: 260px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.detail-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.detail-title {
  font-size: 15px;
  font-weight: 700;
  color: #111827;
  margin: 0;
}
.detail-community {
  font-size: 12px;
  color: #6b7280;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin: 0 0 6px;
}
.detail-wikis {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wiki-item {
  font-size: 13px;
  color: #2563eb;
  cursor: pointer;
  padding: 6px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
}
.wiki-item:hover {
  background: #eff6ff;
}
.detail-empty {
  font-size: 12px;
  color: #9ca3af;
}
</style>
