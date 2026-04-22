<template>
  <div class="graph-page">
    <header class="graph-header">
      <div class="graph-stats">
        <span>{{ stats.total_nodes }} 个节点</span>
        <span>{{ stats.total_edges }} 条关系</span>
        <span>{{ stats.avg_connections }} 平均连接</span>
        <span>{{ stats.clusters }} 个聚类</span>
      </div>
      <div class="graph-controls">
        <el-input v-model="filterText" placeholder="过滤节点..." clearable style="width: 200px" />
        <el-button @click="zoomIn">放大</el-button>
        <el-button @click="zoomOut">缩小</el-button>
        <el-button @click="zoomFit">适应</el-button>
        <el-button type="primary" @click="rebuildGraph" :loading="rebuilding">重建图谱</el-button>
      </div>
    </header>
    <div class="graph-container" ref="containerRef">
      <svg ref="svgRef" width="100%" height="100%"></svg>
    </div>
    <div v-if="hoveredNode" class="node-tooltip" :style="{ left: tooltipPos.x + 'px', top: tooltipPos.y + 'px' }">
      <strong>{{ hoveredNode.title }}</strong>
      <div>{{ hoveredNode.link_count }} 个连接</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import http from '../api/http'
import * as d3 from 'd3'

interface GraphNode {
  id: string
  title: string
  notebook_id: string | null
  link_count: number
}

interface GraphEdge {
  id: string
  source_id: string
  target_id: string
  weight: number
  edge_type: string
}

const router = useRouter()
const containerRef = ref<HTMLDivElement>()
const svgRef = ref<SVGSVGElement>()
const filterText = ref('')
const rebuilding = ref(false)
const hoveredNode = ref<GraphNode | null>(null)
const tooltipPos = ref({ x: 0, y: 0 })
const stats = ref({ total_nodes: 0, total_edges: 0, avg_connections: 0, clusters: 0 })

const notebookColors = [
  '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
  '#ec4899', '#06b6d4', '#84cc16', '#f97316', '#6366f1',
  '#14b8a6', '#e11d48', '#7c3aed', '#0ea5e9', '#d946ef',
]

let simulation: d3.Simulation<any, undefined> | null = null
let zoomBehavior: d3.ZoomBehavior<SVGSVGElement, unknown> | null = null
let nodes: GraphNode[] = []
let edges: GraphEdge[] = []
let notebookMap: Record<string, string> = {}
let svgSelection: d3.Selection<SVGSVGElement, unknown, null, undefined> | null = null

const loadStats = async () => {
  try {
    const res = await http.get('/api/graph/stats')
    stats.value = res.data
  } catch {}
}

const loadData = async () => {
  try {
    const res = await http.get('/api/graph/data')
    nodes = res.data.nodes || []
    edges = res.data.edges || []
    await loadStats()
    renderGraph()
  } catch {
    ElMessage.error('加载图谱数据失败')
  }
}

const getColor = (node: GraphNode) => {
  if (!node.notebook_id) return '#9ca3af'
  if (!(node.notebook_id in notebookMap)) {
    const idx = Object.keys(notebookMap).length % notebookColors.length
    notebookMap[node.notebook_id] = notebookColors[idx]
  }
  return notebookMap[node.notebook_id]
}

const renderGraph = () => {
  if (!svgRef.value || !containerRef.value) return

  d3.select(svgRef.value).selectAll('*').remove()

  const width = containerRef.value.clientWidth
  const height = containerRef.value.clientHeight

  svgSelection = d3.select(svgRef.value)
  svgSelection.attr('viewBox', `0 0 ${width} ${height}`)

  const g = svgSelection.append('g')

  zoomBehavior = d3.zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.1, 4])
    .on('zoom', (event) => {
      g.attr('transform', event.transform)
    })
  svgSelection.call(zoomBehavior)

  const filteredNodes = filterText.value
    ? nodes.filter(n => n.title.toLowerCase().includes(filterText.value.toLowerCase()))
    : nodes
  const filteredNodeIds = new Set(filteredNodes.map(n => n.id))
  const filteredEdges = edges.filter(e => filteredNodeIds.has(e.source_id) && filteredNodeIds.has(e.target_id))

  const simNodes: (GraphNode & d3.SimulationNodeDatum)[] = filteredNodes.map(n => ({ ...n }))
  const simLinks: d3.SimulationLinkDatum<typeof simNodes[0]>[] = filteredEdges.map(e => ({
    source: e.source_id,
    target: e.target_id,
  }))

  const link = g.append('g')
    .selectAll('line')
    .data(simLinks)
    .join('line')
    .attr('stroke', '#e5e7eb')
    .attr('stroke-width', 1)
    .attr('stroke-opacity', 0.6)

  const node = g.append('g')
    .selectAll<SVGCircleElement, typeof simNodes[0]>('circle')
    .data(simNodes)
    .join('circle')
    .attr('r', (d) => Math.max(6, Math.sqrt(d.link_count + 1) * 5))
    .attr('fill', (d) => getColor(d))
    .attr('stroke', '#fff')
    .attr('stroke-width', 2)
    .style('cursor', 'pointer')
    .on('mouseover', (event, d) => {
      hoveredNode.value = d
      tooltipPos.value = { x: event.pageX + 10, y: event.pageY - 10 }
      node.attr('opacity', n => {
        const connected = simLinks.some(
          l =>
            (l.source as any).id === d.id && (l.target as any).id === n.id ||
            (l.target as any).id === d.id && (l.source as any).id === n.id ||
            n.id === d.id
        )
        return connected ? 1 : 0.15
      })
      link.attr('stroke', l => {
        const s = (l.source as any).id
        const t = (l.target as any).id
        return s === d.id || t === d.id ? '#3b82f6' : '#e5e7eb'
      }).attr('stroke-width', l => {
        const s = (l.source as any).id
        const t = (l.target as any).id
        return s === d.id || t === d.id ? 2.5 : 1
      })
    })
    .on('mouseout', () => {
      hoveredNode.value = null
      node.attr('opacity', 1)
      link.attr('stroke', '#e5e7eb').attr('stroke-width', 1)
    })
    .on('click', (_event, d) => {
      router.push({ path: '/', query: { pageId: d.id } })
    })
    .call(d3.drag<SVGCircleElement, typeof simNodes[0]>()
      .on('start', (event, d) => {
        if (!event.active) simulation?.alphaTarget(0.3).restart()
        d.fx = d.x
        d.fy = d.y
      })
      .on('drag', (event, d) => {
        d.fx = event.x
        d.fy = event.y
      })
      .on('end', (event, d) => {
        if (!event.active) simulation?.alphaTarget(0)
        d.fx = null
        d.fy = null
      })
    )

  const label = g.append('g')
    .selectAll('text')
    .data(simNodes)
    .join('text')
    .text(d => d.title.length > 8 ? d.title.slice(0, 8) + '...' : d.title)
    .attr('font-size', 11)
    .attr('fill', '#374151')
    .attr('text-anchor', 'middle')
    .attr('dy', (d) => -(Math.max(6, Math.sqrt(d.link_count + 1) * 5) + 6))

  simulation = d3.forceSimulation(simNodes)
    .force('link', d3.forceLink(simLinks).id((d: any) => d.id).distance(100))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius((d: any) => Math.max(6, Math.sqrt(d.link_count + 1) * 5) + 10))
    .on('tick', () => {
      link
        .attr('x1', (d: any) => d.source.x)
        .attr('y1', (d: any) => d.source.y)
        .attr('x2', (d: any) => d.target.x)
        .attr('y2', (d: any) => d.target.y)
      node
        .attr('cx', (d: any) => d.x)
        .attr('cy', (d: any) => d.y)
      label
        .attr('x', (d: any) => d.x)
        .attr('y', (d: any) => d.y)
    })
}

const zoomIn = () => {
  if (svgSelection && zoomBehavior) {
    svgSelection.transition().duration(300).call(zoomBehavior.scaleBy, 1.3)
  }
}
const zoomOut = () => {
  if (svgSelection && zoomBehavior) {
    svgSelection.transition().duration(300).call(zoomBehavior.scaleBy, 0.7)
  }
}
const zoomFit = () => {
  if (svgSelection && zoomBehavior && containerRef.value) {
    svgSelection.transition().duration(500).call(
      zoomBehavior.transform,
      d3.zoomIdentity.translate(0, 0).scale(1)
    )
  }
}

const rebuildGraph = async () => {
  rebuilding.value = true
  try {
    const res = await http.post('/api/graph/rebuild')
    ElMessage.success(res.data.message)
    await loadData()
  } catch {
    ElMessage.error('重建图谱失败')
  } finally {
    rebuilding.value = false
  }
}

watch(filterText, () => {
  renderGraph()
})

let resizeObserver: ResizeObserver | null = null

onMounted(() => {
  loadData()
  if (containerRef.value) {
    resizeObserver = new ResizeObserver(() => renderGraph())
    resizeObserver.observe(containerRef.value)
  }
})

onBeforeUnmount(() => {
  simulation?.stop()
  resizeObserver?.disconnect()
})
</script>

<style scoped>
.graph-page {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}
.graph-header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 12px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.graph-stats {
  display: flex;
  gap: 20px;
  font-size: 14px;
  color: #606266;
}
.graph-controls {
  display: flex;
  gap: 8px;
  align-items: center;
}
.graph-container {
  flex: 1;
  position: relative;
  overflow: hidden;
}
.node-tooltip {
  position: fixed;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 8px 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.1);
  font-size: 13px;
  z-index: 100;
  pointer-events: none;
}
</style>
