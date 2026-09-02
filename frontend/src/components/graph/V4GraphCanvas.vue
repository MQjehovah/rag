<template>
  <div class="v4-graph-canvas" ref="containerRef">
    <svg ref="svgRef"></svg>
    <div v-if="!nodes.length" class="graph-empty">
      <el-empty description="暂无实体关系" :image-size="60" />
    </div>
    <div class="graph-tooltip" ref="tooltipRef" v-show="tooltip.visible">
      <div class="tooltip-title">{{ tooltip.title }}</div>
      <div class="tooltip-sub">{{ tooltip.subtitle }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as d3 from 'd3'
import type { V4GraphNode, V4GraphEdge } from '../../api/graph'

const props = defineProps<{
  nodes: V4GraphNode[]
  edges: V4GraphEdge[]
}>()

const emit = defineEmits<{
  (e: 'select', node: V4GraphNode): void
}>()

const containerRef = ref<HTMLElement>()
const svgRef = ref<SVGSVGElement>()
const tooltipRef = ref<HTMLElement>()

let simulation: d3.Simulation<GraphNode, undefined> | null = null
let svg: d3.Selection<SVGSVGElement, unknown, null, undefined> | null = null
let nodeGroupSel: d3.Selection<SVGGElement, GraphNode, SVGGElement, unknown> | null = null
let linkSel: d3.Selection<SVGLineElement, GraphLink, SVGGElement, unknown> | null = null
let linkLabelSel: d3.Selection<SVGTextElement, GraphLink, SVGGElement, unknown> | null = null
let labelSel: d3.Selection<SVGTextElement, GraphNode, SVGGElement, unknown> | null = null
let currentTransform: d3.ZoomTransform = d3.zoomIdentity

interface GraphNode extends d3.SimulationNodeDatum {
  id: string
  node: V4GraphNode
}

interface GraphLink extends d3.SimulationLinkDatum<GraphNode> {
  rel: V4GraphEdge
}

const tooltip = ref({ visible: false, title: '', subtitle: '' })

// Community 颜色：确定性调色板（按 community 显示名稳定映射）。
const COMMUNITY_PALETTE = [
  '#6366f1', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444',
  '#8b5cf6', '#14b8a6', '#ec4899', '#f43f5e', '#3b82f6',
]

const ENTITY_COLORS: Record<string, string> = {
  product: '#f43f5e',
  version: '#3b82f6',
  component: '#8b5cf6',
  parameter: '#10b981',
  operation: '#ec4899',
  fault: '#ef4444',
  tool: '#14b8a6',
  system: '#6366f1',
  error_code: '#f59e0b',
  software: '#0ea5e9',
  artifact: '#a855f7',
}

const RELATION_LABELS: Record<string, string> = {
  belongs_to: '属于', contains: '包含', installed_on: '安装在',
  connects_to: '连接到', requires: '依赖', applies_to: '适用于',
  supports: '支持', leads_to: '导致', solves: '解决',
  supersedes: '替代', uploaded_to: '上传到', runs_on: '运行于',
  deployed_to: '部署到', invokes: '调用', uses: '使用',
  stored_in: '保存到', flashed_to: '烧录到',
  capacity: '容量', voltage: '电压', current: '电流',
  power: '功率', torque: '扭矩', speed: '转速',
}

function colorFor(node: V4GraphNode): string {
  if (node.community?.display_name) {
    const idx = hashIndex(node.community.display_name, COMMUNITY_PALETTE.length)
    return COMMUNITY_PALETTE[idx]
  }
  return ENTITY_COLORS[node.entity_type] || '#64748b'
}

function hashIndex(s: string, mod: number): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return h % mod
}

function relationLabel(type: string): string {
  return RELATION_LABELS[type] || type
}

function stopSimulation() {
  if (simulation) {
    simulation.stop()
    simulation = null
  }
}

function adjacentIds(links: GraphLink[], id: string): Set<string> {
  const s = new Set<string>([id])
  links.forEach((l) => {
    const src = String(typeof l.source === 'object' ? l.source.id : l.source)
    const tgt = String(typeof l.target === 'object' ? l.target.id : l.target)
    if (src === id) s.add(tgt)
    if (tgt === id) s.add(src)
  })
  return s
}

function draw() {
  if (!containerRef.value || !svgRef.value) return
  const container = containerRef.value
  const width = container.clientWidth || 800
  const height = container.clientHeight || 600

  stopSimulation()
  svg = d3.select(svgRef.value)
  svg.selectAll('*').remove()
  svg.attr('viewBox', `0 0 ${width} ${height}`)

  const g = svg.append('g')

  const nodes: GraphNode[] = props.nodes.map((n) => ({ id: n.id, node: n }))
  const nodeIdSet = new Set(nodes.map((n) => n.id))

  const links: GraphLink[] = props.edges
    .filter((e) => nodeIdSet.has(e.source) && nodeIdSet.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, rel: e }))

  const zoom = d3.zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.2, 4])
    .on('zoom', (event) => {
      currentTransform = event.transform
      g.attr('transform', event.transform)
      applySemanticZoom(event.transform.k)
    })
  svg.call(zoom)

  // 连线（带关系名）
  linkSel = g.append('g')
    .attr('stroke', '#cbd5e1')
    .attr('stroke-opacity', 0.6)
    .selectAll<SVGLineElement, GraphLink>('line')
    .data(links)
    .join('line')
    .attr('stroke-width', 1.4)

  linkLabelSel = g.append('g')
    .selectAll<SVGTextElement, GraphLink>('text')
    .data(links)
    .join('text')
    .attr('text-anchor', 'middle')
    .attr('font-size', 10)
    .attr('fill', '#94a3b8')
    .attr('paint-order', 'stroke')
    .attr('stroke', '#f8fafc')
    .attr('stroke-width', 3)
    .text((d) => relationLabel(d.rel.relation_label))

  nodeGroupSel = g.append('g').selectAll<SVGGElement, GraphNode>('g').data(nodes).join('g')

  nodeGroupSel
    .append('circle')
    .attr('r', 16)
    .attr('fill', (d) => colorFor(d.node))
    .attr('fill-opacity', 0.88)
    .attr('stroke', '#fff')
    .attr('stroke-width', 1.5)
    .style('cursor', 'pointer')

  labelSel = nodeGroupSel
    .append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', 30)
    .attr('font-size', 12)
    .attr('fill', '#1f2937')
    .attr('font-weight', 600)
    .text((d) => d.node.display_name)

  nodeGroupSel
    .attr('role', 'button')
    .attr('tabindex', 0)
    .attr('aria-label', (d) => d.node.display_name)

  nodeGroupSel
    .on('mouseenter', (event, d) => {
      showTooltip(event, d)
      const adjacent = adjacentIds(links, d.id)
      nodeGroupSel!.style('opacity', (n) => (adjacent.has(n.id) ? 1 : 0.2))
      linkSel!.style('opacity', (l) => {
        const s = String(typeof l.source === 'object' ? l.source.id : l.source)
        const t = String(typeof l.target === 'object' ? l.target.id : l.target)
        return s === d.id || t === d.id ? 1 : 0.15
      })
    })
    .on('mousemove', (event) => positionTooltip(event))
    .on('mouseleave', () => {
      tooltip.value.visible = false
      nodeGroupSel!.style('opacity', 1)
      linkSel!.style('opacity', 1)
    })
    .on('click', (event, d) => {
      event.stopPropagation()
      emit('select', d.node)
    })
    .on('keydown', (event, d) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        emit('select', d.node)
      }
    })

  // 拖拽
  const dragBehavior = d3.drag<SVGGElement, GraphNode>()
    .clickDistance(5)
    .on('start', (event, d) => {
      if (!simulation) return
      if (!event.active) simulation.alphaTarget(0.3).restart()
      d.fx = d.x ?? 0
      d.fy = d.y ?? 0
    })
    .on('drag', (event, d) => {
      d.fx = event.x
      d.fy = event.y
    })
    .on('end', (event, d) => {
      if (!simulation) return
      if (!event.active) simulation.alphaTarget(0)
      d.fx = null
      d.fy = null
    })
  nodeGroupSel.call(dragBehavior as any)

  simulation = d3.forceSimulation<GraphNode>(nodes)
    .force('link', d3.forceLink<GraphNode, GraphLink>(links).id((d) => d.id).distance(130))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide<GraphNode>().radius((d) => 22 + Math.min(d.node.display_name.length * 3, 60)))
    .on('tick', () => {
      linkSel!
        .attr('x1', (d) => (typeof d.source === 'object' ? d.source.x ?? 0 : 0))
        .attr('y1', (d) => (typeof d.source === 'object' ? d.source.y ?? 0 : 0))
        .attr('x2', (d) => (typeof d.target === 'object' ? d.target.x ?? 0 : 0))
        .attr('y2', (d) => (typeof d.target === 'object' ? d.target.y ?? 0 : 0))
      linkLabelSel!
        .attr('x', (d) => {
          const sx = typeof d.source === 'object' ? d.source.x ?? 0 : 0
          const tx = typeof d.target === 'object' ? d.target.x ?? 0 : 0
          return (sx + tx) / 2
        })
        .attr('y', (d) => {
          const sy = typeof d.source === 'object' ? d.source.y ?? 0 : 0
          const ty = typeof d.target === 'object' ? d.target.y ?? 0 : 0
          return (sy + ty) / 2
        })
      nodeGroupSel!.attr('transform', (d) => `translate(${d.x ?? 0},${d.y ?? 0})`)
    })

  applySemanticZoom(currentTransform.k)
}

function showTooltip(event: MouseEvent, d: GraphNode) {
  tooltip.value = {
    visible: true,
    title: d.node.display_name,
    subtitle: d.node.entity_type,
  }
  positionTooltip(event)
}

function positionTooltip(event: MouseEvent) {
  if (!containerRef.value || !tooltipRef.value) return
  const rect = containerRef.value.getBoundingClientRect()
  tooltipRef.value.style.left = `${event.clientX - rect.left + 14}px`
  tooltipRef.value.style.top = `${event.clientY - rect.top + 14}px`
}

function applySemanticZoom(k: number) {
  labelSel?.attr('display', k < 0.4 ? 'none' : null)
  linkLabelSel?.attr('display', k < 1.05 ? 'none' : null)
}

onMounted(() => {
  draw()
  window.addEventListener('resize', draw)
})

onBeforeUnmount(() => {
  stopSimulation()
  window.removeEventListener('resize', draw)
})

watch(
  () => [props.nodes, props.edges],
  () => { draw() },
  { deep: true },
)
</script>

<style scoped>
.v4-graph-canvas {
  position: relative;
  width: 100%;
  height: 100%;
  background: #f8fafc;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  overflow: hidden;
}
.v4-graph-canvas svg {
  width: 100%;
  height: 100%;
  display: block;
}
.graph-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.graph-tooltip {
  position: absolute;
  pointer-events: none;
  background: rgba(17, 24, 39, 0.92);
  color: #fff;
  font-size: 12px;
  border-radius: 8px;
  padding: 8px 12px;
  max-width: 280px;
  z-index: 10;
}
.tooltip-title {
  font-weight: 600;
  margin-bottom: 2px;
}
.tooltip-sub {
  color: #d1d5db;
}
</style>
