<template>
  <div class="entity-graph-canvas" ref="containerRef">
    <svg ref="svgRef"></svg>
    <div v-if="!entities.length && !cards?.length" class="graph-empty">
      <el-empty description="暂无实体" :image-size="60" />
    </div>
    <div
      v-if="entities.length && !relations.length && !cards?.length"
      class="graph-warning"
    >
      当前只有孤立实体，尚未从已发布知识中提取出明确关系。
    </div>
    <div class="graph-tooltip" ref="tooltipRef" v-show="tooltip.visible">
      <div class="tooltip-title">{{ tooltip.title }}</div>
      <div class="tooltip-sub">{{ tooltip.subtitle }}</div>
      <div v-if="tooltip.cards.length" class="tooltip-cards">
        <div v-for="c in tooltip.cards" :key="c.id" class="tooltip-card">
          {{ c.canonical_title }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as d3 from 'd3'
import type { P5CardNode, P5CardEntityLink, P5Entity, P5Relation } from '../../api/p5graph'

const props = defineProps<{
  entities: P5Entity[]
  relations: P5Relation[]
  cards?: P5CardNode[]
  cardEntityLinks?: P5CardEntityLink[]
  filterText?: string
}>()

const emit = defineEmits<{
  (e: 'select', entity: P5Entity): void
  (e: 'select-card', card: P5CardNode): void
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
  kind: 'entity' | 'card'
  entity?: P5Entity
  card?: P5CardNode
}

interface GraphLink extends d3.SimulationLinkDatum<GraphNode> {
  kind: 'relation' | 'card_entity'
  type: string
  rel?: P5Relation
}

const tooltip = ref({
  visible: false,
  title: '',
  subtitle: '',
  cards: [] as P5CardNode[],
})

const ENTITY_COLORS: Record<string, string> = {
  product: '#f43f5e',
  version: '#3b82f6',
  error_code: '#f59e0b',
  parameter: '#10b981',
  component: '#8b5cf6',
  software: '#0ea5e9',
  tool: '#14b8a6',
  system: '#6366f1',
  artifact: '#a855f7',
  operation: '#ec4899',
  solution: '#06b6d4',
}

const CARD_COLOR = '#64748b'

const RELATION_LABELS: Record<string, string> = {
  belongs_to: '属于', contains: '包含', installed_on: '安装在',
  connects_to: '连接到', requires: '依赖', applies_to: '适用于',
  supports: '支持', leads_to: '导致', solves: '解决',
  supersedes: '替代', uploaded_to: '上传到', runs_on: '运行于',
  deployed_to: '部署到', invokes: '调用', uses: '使用',
  stored_in: '保存到', flashed_to: '烧录到',
}

function colorFor(entity: P5Entity): string {
  return ENTITY_COLORS[entity.entity_type] || '#64748b'
}

function displayName(d: GraphNode): string {
  return d.kind === 'card' ? (d.card?.canonical_title || '') : (d.entity?.name || '')
}

/** 按名称长度动态碰撞半径：长名称占位更大，降低标签重叠。 */
function collisionRadius(d: GraphNode): number {
  const base = d.kind === 'card' ? 28 : 20
  return base + Math.min(displayName(d).length * 3, 60)
}

function relationLabel(type: string): string {
  return RELATION_LABELS[type] || type
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

/** 停止并释放当前 simulation（避免重复创建未释放的 D3 simulation）。 */
function stopSimulation() {
  if (simulation) {
    simulation.stop()
    simulation = null
  }
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

  const entityNodes: GraphNode[] = props.entities.map((e) => ({
    id: e.id,
    kind: 'entity',
    entity: e,
  }))
  const cardNodes: GraphNode[] = (props.cards || []).map((c) => ({
    id: `card:${c.id}`,
    kind: 'card',
    card: c,
  }))
  const nodes: GraphNode[] = [...entityNodes, ...cardNodes]

  // 关系边（实体-实体） + Card 关联边（实体-Card）
  const relationLinks: GraphLink[] = props.relations
    .filter((r) => {
      const ids = new Set(props.entities.map((e) => e.id))
      return ids.has(r.source_entity_id) && ids.has(r.target_entity_id)
    })
    .map((r) => ({
      source: r.source_entity_id,
      target: r.target_entity_id,
      kind: 'relation' as const,
      type: r.relation_type,
      rel: r,
    }))

  const cardLinks: GraphLink[] = []
  const entityIdToCardTitles = new Map<string, P5CardNode[]>()
  const cardsById = new Map((props.cards || []).map((c) => [c.id, c]))
  ;(props.cardEntityLinks || []).forEach((link) => {
    const card = cardsById.get(link.card_id)
    if (!card) return
    link.entity_ids.forEach((entityId) => {
      cardLinks.push({
        source: `card:${link.card_id}`,
        target: entityId,
        kind: 'card_entity' as const,
        type: 'card_entity',
      })
      // 为 Tooltip 准备「实体 → 关联 Card 标题」
      const arr = entityIdToCardTitles.get(entityId) || []
      arr.push(card)
      entityIdToCardTitles.set(entityId, arr)
    })
  })

  const links: GraphLink[] = [...relationLinks, ...cardLinks]

  const zoom = d3.zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.2, 4])
    .on('zoom', (event) => {
      currentTransform = event.transform
      g.attr('transform', event.transform)
      applySemanticZoom(event.transform.k)
    })
  svg.call(zoom)

  // 连线（关系边实线，Card 关联边虚线）
  linkSel = g.append('g')
    .attr('stroke', '#cbd5e1')
    .attr('stroke-opacity', 0.6)
    .selectAll<SVGLineElement, GraphLink>('line')
    .data(links)
    .join('line')
    .attr('stroke-width', (d) => (d.kind === 'card_entity' ? 1 : 1.4))
    .attr('stroke-dasharray', (d) => (d.kind === 'card_entity' ? '4,3' : null))

  // 关系标签
  linkLabelSel = g.append('g')
    .selectAll<SVGTextElement, GraphLink>('text')
    .data(links.filter((l) => l.kind === 'relation'))
    .join('text')
    .attr('text-anchor', 'middle')
    .attr('font-size', 10)
    .attr('fill', '#94a3b8')
    .attr('paint-order', 'stroke')
    .attr('stroke', '#f8fafc')
    .attr('stroke-width', 3)
    .text((d) => relationLabel(d.type))

  // 节点组
  nodeGroupSel = g.append('g').selectAll<SVGGElement, GraphNode>('g').data(nodes).join('g')

  nodeGroupSel
    .filter((d) => d.kind === 'entity')
    .append('circle')
    .attr('r', 18)
    .attr('fill', (d) => colorFor(d.entity!))
    .attr('fill-opacity', 0.85)
    .attr('stroke', '#fff')
    .attr('stroke-width', 1.5)
    .style('cursor', 'pointer')

  nodeGroupSel
    .filter((d) => d.kind === 'card')
    .append('rect')
    .attr('x', -26)
    .attr('y', -18)
    .attr('width', 52)
    .attr('height', 36)
    .attr('rx', 6)
    .attr('fill', CARD_COLOR)
    .attr('fill-opacity', 0.9)
    .attr('stroke', '#fff')
    .attr('stroke-width', 1.5)
    .style('cursor', 'pointer')

  // 节点名称标签（圆点/矩形外部下方，完整显示）
  labelSel = nodeGroupSel
    .append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', (d) => (d.kind === 'card' ? 30 : 30))
    .attr('font-size', 12)
    .attr('fill', '#1f2937')
    .attr('font-weight', 600)
    .text((d) => displayName(d))

  // Card 节点可聚焦，支持键盘 Enter 打开（整个 <g> 上绑定，非仅矩形）。
  nodeGroupSel
    .filter((d) => d.kind === 'card')
    .attr('role', 'button')
    .attr('tabindex', 0)
    .attr('aria-label', (d) => displayName(d))

  // 悬停：Tooltip + 高亮相邻
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
    .on('mousemove', (event) => {
      positionTooltip(event)
    })
    .on('mouseleave', () => {
      tooltip.value.visible = false
      nodeGroupSel!.style('opacity', 1)
      linkSel!.style('opacity', 1)
    })
    .on('click', (event, d) => {
      event.stopPropagation()
      if (d.kind === 'card' && d.card) emit('select-card', d.card)
      else if (d.entity) emit('select', d.entity)
    })
    .on('keydown', (event, d) => {
      if ((event.key === 'Enter' || event.key === ' ') && d.kind === 'card' && d.card) {
        event.preventDefault()
        emit('select-card', d.card)
      }
    })

  // 拖拽：clickDistance 容差区分点击与拖拽——移动 < 5px 视为点击（click 正常触发），
  // 超过则进入拖拽并抑制 click，避免拖拽结束后误触发打开。
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

  // 力导向模拟
  simulation = d3.forceSimulation<GraphNode>(nodes)
    .force('link', d3.forceLink<GraphNode, GraphLink>(links).id((d) => d.id).distance((d) => (d.kind === 'card_entity' ? 90 : 120)))
    .force('charge', d3.forceManyBody().strength(-260))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide<GraphNode>().radius((d) => collisionRadius(d)))
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
  if (d.kind === 'card' && d.card) {
    tooltip.value = {
      visible: true,
      title: d.card.canonical_title,
      subtitle: `Card · ${d.card.card_type}`,
      cards: [],
    }
  } else if (d.entity) {
    tooltip.value = {
      visible: true,
      title: d.entity.name,
      subtitle: d.entity.entity_type,
      cards: d.entity.cards || [],
    }
  }
  positionTooltip(event)
}

function positionTooltip(event: MouseEvent) {
  if (!containerRef.value || !tooltipRef.value) return
  const rect = containerRef.value.getBoundingClientRect()
  const x = event.clientX - rect.left + 14
  const y = event.clientY - rect.top + 14
  tooltipRef.value.style.left = `${x}px`
  tooltipRef.value.style.top = `${y}px`
}

/** 语义缩放：缩小隐藏标签，放大显示关系标签。 */
function applySemanticZoom(k: number) {
  labelSel?.attr('display', k < 0.4 ? 'none' : null)
  linkLabelSel?.attr('display', k < 1.05 ? 'none' : null)
}

function applyFilter() {
  if (!nodeGroupSel) return
  if (!props.filterText) {
    nodeGroupSel.style('opacity', 1)
    return
  }
  const q = props.filterText.toLowerCase()
  nodeGroupSel.style('opacity', (d: any) => {
    const name = d.kind === 'card' ? (d.card?.canonical_title || '') : (d.entity?.name || '')
    return name.toLowerCase().includes(q) ? 1 : 0.25
  })
}

onMounted(() => {
  draw()
  window.addEventListener('resize', draw)
})

onBeforeUnmount(() => {
  stopSimulation()
  window.removeEventListener('resize', draw)
})

// 数据源合并为单个 watch：一次加载只触发一次 draw，避免重复重建 simulation。
watch(
  () => [props.entities, props.relations, props.cards, props.cardEntityLinks],
  () => { draw() },
  { deep: true },
)
watch(() => props.filterText, () => { applyFilter() })
</script>

<style scoped>
.entity-graph-canvas {
  position: relative;
  width: 100%;
  height: 100%;
  background: #f8fafc;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  overflow: hidden;
}
.entity-graph-canvas svg {
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
.graph-warning {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  background: #fffbeb;
  border: 1px solid #f59e0b;
  color: #92400e;
  font-size: 12px;
  padding: 6px 14px;
  border-radius: 8px;
  white-space: nowrap;
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
  margin-bottom: 4px;
}
.tooltip-cards {
  border-top: 1px solid rgba(255, 255, 255, 0.2);
  padding-top: 4px;
  margin-top: 2px;
}
.tooltip-card {
  color: #e5e7eb;
  word-break: break-all;
}
</style>
