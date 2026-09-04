<template>
  <section class="api-endpoint-section">
    <!-- 可展开标题按钮：原生 button + click/Enter/Space 均可切换；aria-expanded 反映状态。
         折叠状态按 section id 记忆在当前会话（模块级 Map，不跨组件实例泄漏），
         默认展开；切换即写回，保证返回同一页面时状态不丢、也不与其他章节串用。 -->
    <button
      type="button"
      class="api-ep-toggle"
      :aria-expanded="expanded ? 'true' : 'false'"
      @click="toggle"
      @keydown.enter.prevent="toggle"
      @keydown.space.prevent="toggle"
    >
      <span class="api-method">{{ display.endpoint.method }}</span>
      <code class="api-path">{{ display.endpoint.path }}</code>
      <span class="api-ver">{{ versionLabel }}</span>
    </button>

    <div v-show="expanded" class="api-ep-body">
      <p v-if="display.endpoint.summary" class="api-summary">{{ display.endpoint.summary }}</p>
      <p class="api-desc">{{ display.endpoint.description || '未提供' }}</p>

      <!-- 参数表：位置 / 名称 / 类型 / 必填 / 说明（类型只读显式声明；缺省→未提供，不虚构） -->
      <div class="api-part">
        <div class="api-part-title">参数</div>
        <template v-if="display.parameters.length">
          <div class="api-table-scroll">
            <table class="api-table api-params-table">
              <thead>
                <tr>
                  <th scope="col">位置</th>
                  <th scope="col">名称</th>
                  <th scope="col">类型</th>
                  <th scope="col">必填</th>
                  <th scope="col">说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in display.parameters" :key="p.location + ':' + p.name">
                  <td>{{ p.location }}</td>
                  <td class="api-cell-name">{{ p.name }}</td>
                  <td class="api-cell-type">{{ p.type || '未提供' }}</td>
                  <td>{{ p.required ? '是' : '否' }}</td>
                  <td>{{ p.description || '未提供' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <p v-else class="api-empty-tip">该端点未提供参数表信息</p>
      </div>

      <!-- 请求体 -->
      <div class="api-part">
        <div class="api-part-title">请求体</div>
        <template v-if="display.request_body">
          <div class="api-reqbody">
            <div class="api-reqbody-line">必填：{{ display.request_body.required ? '是' : '否' }}</div>
            <div v-if="display.request_body.description" class="api-reqbody-desc">
              {{ display.request_body.description }}
            </div>
            <div
              v-for="mt in display.request_body.media_types"
              :key="mt.media_type"
              class="api-media"
            >
              {{ mt.media_type }} · {{ schemaLabel(mt) }}
            </div>
          </div>
        </template>
        <p v-else class="api-empty-tip">未提供请求体</p>
      </div>

      <!-- 响应分区：每个媒体类型保留为独立行；schema 只做保守判定 -->
      <div class="api-part">
        <div class="api-part-title">响应</div>
        <template v-if="responseRows.length">
          <div class="api-table-scroll">
            <table class="api-table api-responses-table">
              <thead>
                <tr>
                  <th scope="col">状态码</th>
                  <th scope="col">媒体类型</th>
                  <th scope="col">结构</th>
                  <th scope="col">说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in responseRows" :key="row.key">
                  <td class="api-cell-code">{{ row.status_code }}</td>
                  <td class="api-cell-type">{{ row.media_type }}</td>
                  <td>{{ row.schema_label }}</td>
                  <td>{{ row.description }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <p v-else class="api-empty-tip">未提供响应信息</p>
      </div>

      <!-- 业务错误码独立分区：不并入 HTTP 状态列 -->
      <div class="api-part">
        <div class="api-part-title">业务错误码</div>
        <p class="api-hint">业务错误码不等于 HTTP 状态</p>
        <template v-if="display.error_codes.length">
          <div class="api-table-scroll">
            <table class="api-table api-errors-table">
              <thead>
                <tr>
                  <th scope="col">业务码</th>
                  <th scope="col">说明</th>
                  <th scope="col">HTTP状态</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in display.error_codes" :key="e.code">
                  <td class="api-cell-code">{{ e.code }}</td>
                  <td>{{ e.description || '未提供' }}</td>
                  <td>{{ e.http_status || '未提供' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <p v-else class="api-empty-tip">该端点未提供业务错误码信息</p>
      </div>

      <!-- 请求/响应示例：安全 <pre> 文本块（JSON.stringify，绝不 v-html） -->
      <div class="api-part">
        <div class="api-part-title">示例</div>
        <template v-if="display.examples.length">
          <div v-for="(ex, i) in display.examples" :key="ex.title || ('example-' + i)" class="api-example">
            <div class="api-example-label">
              <span class="api-example-title">{{ ex.title || '示例' }}</span>
              <span v-if="ex.media_type" class="api-example-type">{{ ex.media_type }}</span>
            </div>
            <p v-if="ex.description" class="api-example-desc">{{ ex.description }}</p>
            <pre class="api-example-pre">{{ exampleText(ex.content) }}</pre>
          </div>
        </template>
        <p v-else class="api-empty-tip">该端点未提供示例</p>
      </div>

      <!-- 版本说明分区 -->
      <div class="api-part">
        <div class="api-part-title">版本说明</div>
        <template v-if="display.version_notes.length">
          <p v-for="(vn, i) in display.version_notes" :key="i" class="api-note-row">
            <span v-if="vn.version_scope" class="api-note-ver">{{ vn.version_scope }}</span>
            {{ vn.note || '未提供' }}
          </p>
        </template>
        <p v-else class="api-empty-tip">该端点暂无版本说明</p>
      </div>

      <!-- 知识缺口分区 -->
      <div class="api-part">
        <div class="api-part-title">知识缺口</div>
        <p class="api-hint">以下为当前端点未覆盖事实的缺口提示，仅供补充验证参考。</p>
        <template v-if="display.knowledge_gaps.length">
          <p v-for="(g, i) in display.knowledge_gaps" :key="i" class="api-gap-row">
            <span v-if="g.gap_type" class="api-gap-type">{{ g.gap_type }}</span>
            {{ g.description || '未提供' }}
          </p>
        </template>
        <p v-else class="api-empty-tip">暂无知识缺口</p>
      </div>

      <!-- 冲突事实：只提示存在冲突，不暴露证据 id/候选 -->
      <div class="api-part">
        <div class="api-part-title">冲突事实</div>
        <template v-if="display.conflicts.length">
          <p v-for="(c, i) in display.conflicts" :key="i" class="api-conflict-row">
            存在冲突事实，未判定对错
            <span v-if="c.field_path" class="api-conflict-path">{{ c.field_path }}</span>
          </p>
        </template>
        <p v-else class="api-empty-tip">未发现冲突事实</p>
      </div>
    </div>
  </section>
</template>

<script lang="ts">
// 折叠状态按 section id 存于模块级 Map：同一页面来回导航可恢复，不与其他 Section/
// Revision/Wiki/Workspace 串用（各 section id 独立）。不随组件卸载清空，避免返回页面时状态丢失。
// 注意：必须放在 <script>（模块作用域）而非 <script setup>（每个实例都会执行）。
const collapsedBySection = new Map<string, boolean>()
</script>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ApiMediaType, ApiSectionDisplay } from '../../api/wiki'

const props = defineProps<{
  sectionId: string
  display: ApiSectionDisplay
}>()

const expanded = ref(!collapsedBySection.get(props.sectionId))

function toggle() {
  expanded.value = !expanded.value
  collapsedBySection.set(props.sectionId, !expanded.value)
}

const versionLabel = computed(() => {
  const v = (props.display.version_scope || '').trim()
  return v || 'unversioned'
})

function schemaLabel(media: ApiMediaType): string {
  // present：媒体确有具体 schema；unspecified：无法区分缺失与显式空 → 保守文案。
  return media.schema_status === 'present' ? '含Schema' : '未提供具体结构'
}

interface ResponseRowView {
  key: string
  status_code: string
  media_type: string
  schema_label: string
  description: string
}

// 把每个响应按媒体类型平铺为行：无 content 的响应仍保留一行（媒体类型=未提供），
// 原正文展示过的媒体类型名称不被丢弃。
const responseRows = computed<ResponseRowView[]>(() => {
  const out: ResponseRowView[] = []
  const responses = props.display.responses || []
  for (const r of responses) {
    const media = Array.isArray(r.media_types) ? r.media_types : []
    const desc = r.description || '未提供'
    if (media.length === 0) {
      out.push({
        key: r.status_code + ':none',
        status_code: r.status_code,
        media_type: '未提供',
        schema_label: '未提供',
        description: desc,
      })
      continue
    }
    for (const m of media) {
      out.push({
        key: r.status_code + ':' + m.media_type,
        status_code: r.status_code,
        media_type: m.media_type,
        schema_label: schemaLabel(m),
        description: desc,
      })
    }
  }
  return out
})

function exampleText(content: unknown): string {
  if (content === undefined) return ''
  try {
    return JSON.stringify(content, null, 2)
  } catch {
    return String(content)
  }
}
</script>

<style scoped>
.api-endpoint-section {
  max-width: 100%;
}
.api-ep-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  max-width: 100%;
  font: inherit;
  text-align: left;
  cursor: pointer;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 8px 10px;
  color: #1f2937;
  box-sizing: border-box;
}
.api-ep-toggle:hover {
  background: #f1f5f9;
  border-color: #cbd5e1;
}
.api-ep-toggle:focus-visible {
  outline: 2px solid #2563eb;
  outline-offset: 2px;
}
.api-method {
  font-weight: 700;
  font-size: 12px;
  color: #fff;
  background: #2563eb;
  border-radius: 4px;
  padding: 1px 6px;
  flex: none;
}
.api-path {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  font-weight: 600;
  color: #111827;
  word-break: break-all;
}
.api-ver {
  flex: none;
  font-size: 11px;
  color: #6b7280;
  background: #eef2ff;
  border: 1px solid #c7d2fe;
  border-radius: 999px;
  padding: 1px 8px;
}
.api-ep-body {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 100%;
}
.api-summary {
  margin: 0;
  font-weight: 600;
  color: #1f2937;
}
.api-desc {
  margin: 0;
  color: #4b5563;
  overflow-wrap: anywhere;
}
.api-part {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
  max-width: 100%;
  min-width: 0;
}
.api-part-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin-bottom: 6px;
}
.api-hint {
  font-size: 12px;
  color: #b45309;
  background: #fffbeb;
  border-radius: 6px;
  padding: 4px 8px;
  margin: 0 0 6px;
}
.api-table-scroll {
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid #eef2f7;
  border-radius: 6px;
}
/* 窄屏可读性：表格给足内容宽度，由外层 .api-table-scroll 横向滚动；
   短字段（状态码/位置/类型/必填等）不逐字母断行，说明列自然换行。
   不用 overflow:hidden 裁列，不隐藏任何事实；桌面宽度充足时自动填满不额外滚动。 */
table.api-table {
  width: 100%;
  table-layout: auto;
  border-collapse: collapse;
  font-size: 12px;
  color: #4b5563;
}
table.api-params-table {
  min-width: 560px;
}
table.api-responses-table {
  min-width: 620px;
}
table.api-errors-table {
  min-width: 460px;
}
.api-table th,
.api-table td {
  padding: 6px 8px;
  border: 1px solid #e5e7eb;
  text-align: left;
  vertical-align: top;
}
.api-table th {
  background: #f1f5f9;
  color: #374151;
  font-weight: 600;
  white-space: nowrap;
}
/* 说明/长文本列：允许自然换行 */
.api-table td {
  overflow-wrap: anywhere;
  white-space: normal;
}
/* 参数表：位置/名称/类型/必填为短字段（不逐字母断行）；说明自然换行 */
table.api-params-table td:nth-child(-n+4) {
  white-space: nowrap;
  overflow-wrap: normal;
}
/* 响应表：状态码/媒体类型/结构为短字段；说明自然换行 */
table.api-responses-table td:nth-child(-n+3) {
  white-space: nowrap;
  overflow-wrap: normal;
}
/* 错误码表：业务码/HTTP 状态为短字段；说明自然换行 */
table.api-errors-table td:nth-child(1),
table.api-errors-table td:nth-child(3) {
  white-space: nowrap;
  overflow-wrap: normal;
}
.api-cell-name,
.api-cell-code,
.api-cell-type {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.api-cell-type {
  white-space: nowrap;
  overflow-wrap: normal;
}
.api-reqbody {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}
.api-reqbody-line {
  font-weight: 600;
  color: #374151;
}
.api-reqbody-desc {
  color: #4b5563;
}
.api-media {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.api-empty-tip {
  margin: 0;
  font-size: 12px;
  color: #9ca3af;
}
.api-example {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 100%;
  min-width: 0;
}
.api-example + .api-example {
  margin-top: 8px;
}
.api-example-label {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  flex-wrap: wrap;
}
.api-example-title {
  font-weight: 600;
  color: #374151;
}
.api-example-type {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: #6b7280;
}
.api-example-desc {
  margin: 2px 0 0;
  font-size: 12px;
  color: #6b7280;
  overflow-wrap: anywhere;
}
pre.api-example-pre {
  margin: 0;
  max-width: 100%;
  max-height: 280px;
  padding: 8px 10px;
  overflow-x: auto;
  overflow-y: auto;
  border-radius: 6px;
  background: #1f2937;
  color: #e5e7eb;
  font-size: 12px;
  line-height: 1.5;
}
.api-note-row,
.api-gap-row,
.api-conflict-row {
  margin: 0 0 4px;
  font-size: 12px;
  color: #4b5563;
  overflow-wrap: anywhere;
}
.api-note-ver,
.api-gap-type,
.api-conflict-path {
  display: inline-block;
  margin-right: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: #374151;
  background: #f3f4f6;
  border-radius: 4px;
  padding: 0 5px;
}
.api-conflict-row {
  color: #b45309;
}
.api-conflict-path {
  color: #b45309;
  background: #fffbeb;
}
</style>
