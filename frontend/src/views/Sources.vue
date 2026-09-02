<template>
  <div class="sources-page">
    <header class="sources-header">
      <div class="header-left">
        <h2 class="page-title">数据源</h2>
        <el-tag v-if="!connectionList.length" type="info" size="small">尚未配置连接</el-tag>
      </div>
      <div class="header-actions">
        <el-button @click="load">刷新</el-button>
      </div>
    </header>

    <!-- Connector 卡片（基于 connector_setup 渲染，无连接也显示） -->
    <div class="connections-section">
      <h3 class="section-title">连接</h3>
      <div v-loading="loading" class="connections-list">
        <div v-for="key in connectorKeys" :key="key" class="connection-card">
          <div class="conn-header">
            <div class="conn-title">
              <el-tag size="small" :type="connectorStatusTag(key)">{{ connectorStatusLabel(key) }}</el-tag>
              <span class="conn-name">{{ connectorName(key) }}</span>
            </div>
            <div class="conn-actions">
              <el-button v-if="!primaryConn(key)" size="small" type="primary" @click="openCreateDialog(key)">配置{{ connectorName(key) }}</el-button>
              <template v-else>
                <el-button size="small" @click="openConfigDialog(primaryConn(key)!)">配置</el-button>
                <el-button size="small" @click="testConnection(primaryConn(key)!)">测试连接</el-button>
                <el-button v-if="primaryConn(key)!.enabled" size="small" @click="viewItems(primaryConn(key)!)">查看内容</el-button>
                <el-button v-if="primaryConn(key)!.enabled" size="small" type="primary" @click="openSyncDialog(primaryConn(key)!)">立即同步</el-button>
              </template>
            </div>
          </div>

          <div class="conn-meta">
            <span>凭证：{{ setupOf(key)?.configured ? '已配置' : '未配置' }}</span>
            <span>Connector：{{ setupOf(key)?.enabled ? '已启用' : '未启用' }}</span>
            <!-- J-1 最终返工：钉钉文件归属由路径映射决定，不显示目标知识库/权限未配置 -->
            <template v-if="key === 'dingtalk'">
              <span>文件归属：由路径映射决定</span>
              <span>已配置映射：{{ dingtalkMappingCount }} 条</span>
            </template>
            <template v-else>
              <span>目标知识库：{{ targetNotebookLabel(key) }}</span>
              <span :class="{ 'warn-text': targetGroupLabel(key) !== '已配置' }">权限范围：{{ targetGroupLabel(key) }}</span>
            </template>
          </div>

          <!-- setup blockers -->
          <div v-if="blockersFor(key).length" class="blockers">
            <el-alert
              v-for="b in blockersFor(key)"
              :key="b.code"
              :title="b.message"
              :type="blockerType(b.code)"
              :closable="false"
              show-icon
            />
          </div>

          <!-- 有连接时展示最近任务 -->
          <div v-if="primaryConn(key) && latestRun(primaryConn(key)!.id)" class="latest-run">
            <template v-if="latestRun(primaryConn(key)!.id)">
              <el-tag :type="runStatusTag(latestRun(primaryConn(key)!.id)!.status)" size="small">{{ runStatusLabel(latestRun(primaryConn(key)!.id)!.status) }}</el-tag>
              <span class="run-stage">{{ stageText(latestRun(primaryConn(key)!.id)!.stage) }}</span>
              <span class="run-counters">
                新增 {{ latestRun(primaryConn(key)!.id)!.created_count }} / 更新 {{ latestRun(primaryConn(key)!.id)!.updated_count }}
                / 删除 {{ latestRun(primaryConn(key)!.id)!.deleted_count }} / 失败 {{ latestRun(primaryConn(key)!.id)!.failed_count }}
              </span>
              <el-tag v-if="latestRun(primaryConn(key)!.id)!.llm_degraded" size="small" type="warning">GLM 降级</el-tag>
            </template>
          </div>
        </div>
        <div v-if="!connectorKeys.length && !loading" class="empty-tip">暂无可配置的数据源</div>
      </div>
    </div>

    <!-- P38：统一数据源路径权限映射 -->
    <div class="mappings-section">
      <div class="section-header">
        <h3 class="section-title">数据源路径权限映射</h3>
        <div class="mapping-toolbar">
          <el-select
            v-model="mappingConnectionId"
            placeholder="选择数据源连接"
            clearable
            size="small"
            style="width: 220px"
            @change="loadPathMappings"
          >
            <el-option
              v-for="c in connectionList"
              :key="c.id"
              :label="`${connectorName(c.connector_key)} - ${c.name}`"
              :value="c.id"
            />
          </el-select>
          <el-button size="small" type="primary" @click="openMappingDialog()">新增映射</el-button>
        </div>
      </div>
      <div v-loading="mappingsLoading" class="mappings-table">
        <el-table :data="pathMappings" size="small" border stripe>
          <el-table-column label="数据源连接" min-width="160">
            <template #default="{ row }">
              {{ connectionName(row.connection_id) }}
            </template>
          </el-table-column>
          <el-table-column label="路径/目录" min-width="220">
            <template #default="{ row }">
              <span class="path-namespace" v-if="row.path_namespace">{{ row.path_namespace }} / </span>{{ row.folder_path || '（根）' }}
            </template>
          </el-table-column>
          <el-table-column label="目标 Notebook" min-width="160">
            <template #default="{ row }">{{ row.notebook_name || row.notebook_id }}</template>
          </el-table-column>
          <el-table-column label="Notebook 可见范围" min-width="180">
            <template #default="{ row }">
              <el-tag v-if="scopeLabelFor(row.notebook_id)" size="small" :type="scopeTagFor(row.notebook_id)">{{ scopeLabelFor(row.notebook_id) }}</el-tag>
              <span v-else class="field-hint">未配置</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button size="small" @click="openMappingDialog(row)">编辑</el-button>
              <el-button size="small" type="danger" @click="removeMapping(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!pathMappings.length && !mappingsLoading" class="empty-tip">
          未配置映射。未映射的路径不会导入 Page/Chunk/Evidence。
        </div>
      </div>
    </div>

    <!-- J-1 最终遗留：独立的 Notebook 权限编辑区（company / admin / 一个或多个业务组） -->
    <div ref="notebookSectionEl" class="mappings-section">
      <div class="section-header">
        <h3 class="section-title">Notebook 权限</h3>
        <span class="field-hint">每个知识库的可见范围：全公司 / 仅管理员 / 指定业务组（可多选）。不依附数据源连接创建流程。</span>
      </div>
      <div class="mappings-table">
        <el-table :data="targetNotebooks" size="small" border stripe :row-class-name="notebookRowClassName">
          <el-table-column prop="name" label="Notebook" min-width="180" />
          <el-table-column label="可见范围" min-width="220">
            <template #default="{ row }">
              <el-tag v-if="row.scope_label" size="small" :type="scopeTagFor(row.id)">{{ row.scope_label }}</el-tag>
              <span v-else class="field-hint">未配置</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button size="small" @click="openNotebookScopeDialog(row)">编辑</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!targetNotebooks.length" class="empty-tip">暂无知识库</div>
      </div>
    </div>

    <!-- 运行记录 -->
    <div class="runs-section">
      <h3 class="section-title">同步记录</h3>
      <div v-loading="runsLoading" class="runs-table">
        <el-table :data="runs" size="small" border stripe>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="runStatusTag(row.status)" size="small">{{ runStatusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="mode" label="模式" width="110" />
          <el-table-column label="阶段" min-width="140">
            <template #default="{ row }">
              <span>{{ stageText(row.stage) }}</span>
              <el-tag v-if="row.llm_degraded" size="small" type="warning" style="margin-left: 6px">降级</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="进度" min-width="180">
            <template #default="{ row }">
              新 {{ row.created_count }} / 更 {{ row.updated_count }} / 未变 {{ row.unchanged_count }} / 删 {{ row.deleted_count }} / 失败 {{ row.failed_count }}
            </template>
          </el-table-column>
          <el-table-column label="开始时间" width="160">
            <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="220">
            <template #default="{ row }">
              <el-button v-if="row.status === 'running' || row.status === 'queued'" size="small" @click="cancelRun(row)">取消</el-button>
              <el-button v-if="row.failed_count > 0" size="small" type="warning" @click="viewErrors(row)">查看错误</el-button>
              <el-button v-if="canRetry(row)" size="small" type="primary" @click="retryRun(row)">失败重试</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!runs.length && !runsLoading" class="empty-tip">暂无同步记录</div>
      </div>
    </div>

    <!-- 创建连接抽屉（无连接时的「配置钉钉」入口） -->
    <el-drawer v-model="createDialog.visible" :title="'配置 ' + connectorName(createDialog.connectorKey)" size="480px">
      <el-form label-width="100px">
        <el-form-item label="连接名称">
          <el-input v-model="createDialog.name" placeholder="默认：钉钉知识库" />
        </el-form-item>
        <!-- J-1 最终遗留：钉钉创建连接不再配置目标知识库/权限范围；
             文件归属完全由「文件夹权限映射」决定，Notebook 权限在下方独立编辑区配置。 -->
        <template v-if="createDialog.connectorKey === 'dingtalk'">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="钉钉文件归属由「文件夹权限映射」决定，无需在此选择目标知识库。请在下方「Notebook 权限」区为目标知识库配置可见范围。"
          />
        </template>
        <template v-else>
          <el-form-item label="目标知识库">
            <el-select v-model="createDialog.target_notebook_id" style="width: 100%" placeholder="请选择目标知识库">
              <el-option
                v-for="notebook in targetNotebooks"
                :key="notebook.id"
                :label="`${notebook.name}${notebook.group_id ? '' : '（未绑定权限组）'}`"
                :value="notebook.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="权限范围">
            <el-tag v-if="!createDialog.target_notebook_id" type="info" size="small">未选择知识库</el-tag>
            <el-tag v-else-if="scopeLabelFor(createDialog.target_notebook_id)" :type="scopeTagFor(createDialog.target_notebook_id)" size="small">{{ scopeLabelFor(createDialog.target_notebook_id) }}</el-tag>
            <el-tag v-else type="danger" size="small">尚未设置</el-tag>
          </el-form-item>
        </template>
        <el-form-item label="凭证">
          <el-tag v-if="setupOf(createDialog.connectorKey)?.configured" type="success" size="small">已配置</el-tag>
          <el-tag v-else type="warning" size="small">未配置</el-tag>
          <div class="field-hint">凭证只来自环境变量，不回显密钥</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="createDialog.enabled" />
        </el-form-item>
        <el-collapse>
          <el-collapse-item title="高级设置（JSON）">
            <el-form-item label="配置 JSON">
              <el-input v-model="createDialog.config_text" type="textarea" :rows="4" placeholder="非敏感配置，如 {}" />
            </el-form-item>
          </el-collapse-item>
        </el-collapse>
      </el-form>
      <template #footer>
        <el-button @click="createDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="createDialog.saving" @click="submitCreate">创建连接</el-button>
      </template>
    </el-drawer>

    <!-- 编辑连接抽屉 -->
    <el-drawer v-model="configDialog.visible" :title="'配置 ' + connectorName(configDialog.connectorKey)" size="480px">
      <el-form label-width="100px">
        <!-- J-1 最终遗留：钉钉编辑连接隐藏目标知识库（归属由文件夹映射决定）。 -->
        <template v-if="configDialog.connectorKey === 'dingtalk'">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="钉钉文件归属由「文件夹权限映射」决定；Notebook 权限请在下方「Notebook 权限」区配置。"
          />
        </template>
        <template v-else>
          <el-form-item label="目标知识库">
            <el-select v-model="configDialog.target_notebook_id" style="width: 100%" placeholder="请选择已绑定权限组的知识库">
              <el-option
                v-for="notebook in targetNotebooks"
                :key="notebook.id"
                :label="`${notebook.name}${notebook.group_id ? '' : '（未绑定权限组）'}`"
                :value="notebook.id"
                :disabled="!notebook.group_id"
              />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item label="启用">
          <el-switch v-model="configDialog.enabled" />
        </el-form-item>
        <el-collapse>
          <el-collapse-item title="高级设置（JSON）">
            <el-form-item label="配置 JSON">
              <el-input v-model="configDialog.config_text" type="textarea" :rows="4" placeholder="非敏感配置，如 {}" />
            </el-form-item>
          </el-collapse-item>
        </el-collapse>
        <el-form-item label="Secret">
          <el-tag type="warning" size="small">密钥不回显，通过环境变量注入</el-tag>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="configDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="configDialog.saving" @click="saveConfig">保存</el-button>
      </template>
    </el-drawer>

    <!-- 同步设置抽屉 -->
    <el-drawer v-model="syncDialog.visible" :title="'同步 ' + connectorName(syncDialog.connectorKey)" size="480px">
      <el-form label-width="100px">
        <el-form-item label="同步模式">
          <el-radio-group v-model="syncDialog.mode" style="display: flex; flex-direction: column; gap: 10px">
            <el-radio value="incremental">增量同步 — 只处理变更，推荐日常使用</el-radio>
            <el-radio value="backfill">历史回填 — 补齐历史遗漏，可能耗时较长</el-radio>
            <el-radio value="full_reconcile">全量核对 — 识别远端删除与移动，耗时最长</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-alert
          v-if="syncDialog.mode === 'full_reconcile'"
          type="warning"
          :closable="false"
          show-icon
          title="全量核对会检测远端删除，相关 Page 会被归档、Chunk 停用、Evidence 标记失效，请确认删除策略后再执行"
        />
        <el-form-item label="试点数量">
          <el-input-number v-model="syncDialog.pilot_limit" :min="0" :max="10000" />
          <div class="field-hint">0 表示不限制（正式全量）；设为 1 或 10 仅作试点</div>
        </el-form-item>
        <el-form-item label="空间范围">
          <el-select v-model="syncDialog.selected_space_ids" multiple clearable filterable placeholder="留空使用配置范围" style="width: 100%">
            <el-option v-for="s in scopes" :key="s.scope_id" :label="s.name" :value="s.scope_id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="syncDialog.visible = false">取消</el-button>
        <el-button type="primary" @click="confirmSync">开始同步</el-button>
      </template>
    </el-drawer>

    <!-- P38：统一路径映射编辑抽屉 -->
    <el-drawer v-model="mappingDialog.visible" :title="mappingDialog.id ? '编辑路径映射' : '新增路径映射'" size="520px">
      <el-form label-width="120px">
        <el-form-item label="数据源连接">
          <el-select
            v-model="mappingDialog.connection_id"
            style="width: 100%"
            placeholder="选择数据源连接"
            :disabled="!!mappingDialog.id"
            @change="onMappingConnectionChange"
          >
            <el-option
              v-for="c in connectionList"
              :key="c.id"
              :label="`${connectorName(c.connector_key)} - ${c.name}`"
              :value="c.id"
            />
          </el-select>
          <div class="field-hint">{{ mappingCapabilityHint }}</div>
        </el-form-item>
        <el-form-item v-if="mappingHasPathCapability" label="路径命名域">
          <el-input v-model="mappingDialog.path_namespace" placeholder="留空匹配该连接内任意命名域" />
          <div class="field-hint">钉钉为 space_id；GitLab 为项目/仓库标识。留空则该连接内通配。</div>
        </el-form-item>
        <el-form-item label="路径/目录">
          <el-select
            v-model="mappingDialog.folder_path"
            filterable
            allow-create
            default-first-option
            style="width: 100%"
            placeholder="选择已发现目录，或手动输入完整路径"
            no-data-text="无已发现目录，请手动输入"
            @change="onSelectDiscoveredPath"
          >
            <el-option
              v-for="p in discoveredPaths"
              :key="`${p.path_namespace}/${p.folder_path}`"
              :label="`${p.path_namespace ? p.path_namespace + ' / ' : ''}${p.folder_path}`"
              :value="`${p.path_namespace}|||${p.folder_path}`"
            />
          </el-select>
          <div class="field-hint">
            优先从已发现目录选择（避免同名目录歧义）；未发现时手动输入完整路径作为后备。
            子目录继承此映射；更具体路径优先。
          </div>
        </el-form-item>
        <el-form-item label="目标 Notebook">
          <el-select v-model="mappingDialog.notebook_id" style="width: 100%" placeholder="选择权限域有效的知识库">
            <el-option
              v-for="notebook in eligibleTargetNotebooks"
              :key="notebook.id"
              :label="`${notebook.name}（${notebook.scope_label || '未配置'}）`"
              :value="notebook.id"
            />
          </el-select>
          <div v-if="targetNotebooks.some(isNotebookScopeUnknown)" class="field-hint warn-text">
            存在权限域未知（unknown）的知识库，已被禁用；请先在「Notebook 权限」区为其配置可见范围。
          </div>
        </el-form-item>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="一个路径只能映射到一个 Notebook；多个路径可以映射到同一 Notebook。"
        />
      </el-form>
      <template #footer>
        <el-button @click="mappingDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="mappingDialog.saving" @click="saveMapping">保存</el-button>
      </template>
    </el-drawer>

    <!-- J-1 最终遗留：Notebook 权限编辑抽屉（company / admin / 多组） -->
    <el-drawer v-model="scopeDialog.visible" :title="`编辑权限：${scopeDialog.notebookName || ''}`" size="520px">
      <el-form label-width="120px">
        <el-form-item label="可见范围类型">
          <el-radio-group v-model="scopeDialog.scopeType">
            <el-radio value="company">全公司</el-radio>
            <el-radio value="admin">仅管理员</el-radio>
            <el-radio value="groups">指定业务组</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="scopeDialog.scopeType === 'groups'" label="业务组">
          <el-select v-model="scopeDialog.groupNames" multiple filterable style="width: 100%" placeholder="可多选：允许这些业务组访问">
            <el-option v-for="s in businessScopeOptions" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
          <div class="field-hint">支持同时授权多个业务组；同一文件不复制，多组共享。仅显示真正业务组。</div>
          <div v-if="!businessScopeOptions.length" class="field-hint">
            尚未同步到业务组。管理员请使用「仅管理员」选项。
          </div>
        </el-form-item>
        <el-alert
          v-if="scopeDialog.scopeType === 'admin'"
          type="warning"
          :closable="false"
          show-icon
          title="该范围仅允许管理员访问，正式上线前应替换为公司 LDAP 业务组。"
        />
      </el-form>
      <template #footer>
        <el-button @click="scopeDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="scopeDialog.saving" @click="saveNotebookScope">保存</el-button>
      </template>
    </el-drawer>

    <!-- SourceItem 追溯抽屉 -->
    <el-drawer v-model="itemsDialog.visible" title="同步内容（SourceItem）" size="640px">
      <div v-loading="itemsLoading" class="items-list">
        <div v-for="item in items" :key="item.id" class="item-row">
          <el-tag :type="itemStateTag(item.state)" size="small">{{ item.state }}</el-tag>
          <span class="item-external" :title="item.external_id">{{ item.external_id }}</span>
          <span v-if="item.page_id" class="item-page">Page: {{ item.page_id.slice(0, 8) }}</span>
          <span v-if="item.last_error" class="error-text">错误: {{ item.last_error.slice(0, 40) }}</span>
        </div>
        <div v-if="!items.length && !itemsLoading" class="empty-tip">暂无条目</div>
      </div>
    </el-drawer>

    <!-- 错误详情抽屉 -->
    <el-drawer v-model="errorsDialog.visible" title="同步错误" size="560px">
      <div v-loading="errorsLoading" class="errors-list">
        <div v-for="(e, i) in errors" :key="i" class="error-row">
          <div class="error-stage">[{{ errorCategoryLabel(e.error_code) }}] {{ e.external_id || '' }}</div>
          <div class="error-code">{{ e.error_code || 'ERROR' }}</div>
          <div class="error-msg">{{ e.error_message }}</div>
          <el-tag v-if="e.retryable" size="small" type="warning">可重试</el-tag>
        </div>
        <div v-if="!errors.length && !errorsLoading" class="empty-tip">无错误</div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  sourceApi,
  type AccessScope,
  type ConnectorSetupStatus,
  type PathMapping,
  type SourceConnection,
  type SourceItem,
  type SourceSyncError,
  type SourceSyncRun,
  type SourceTargetNotebook,
} from '../api/sources'

const loading = ref(false)
const connections = ref<Record<string, SourceConnection>>({})
const connectors = ref<string[]>([])
const connectorSetup = ref<Record<string, ConnectorSetupStatus>>({})
const targetNotebooks = ref<SourceTargetNotebook[]>([])
const runs = ref<SourceSyncRun[]>([])
const runsLoading = ref(false)
const scopes = ref<{ scope_id: string; name: string }[]>([])

const route = useRoute()
const notebookSectionEl = ref<HTMLElement | null>(null)
const highlightNotebookId = ref('')

const connectionList = computed(() => Object.values(connections.value))
let pollTimer: number | null = null

const connectorKeys = computed<string[]>(() => {
  const keys = Object.keys(connectorSetup.value)
  if (keys.length) return keys
  return connectors.value
})

const createDialog = reactive({
  visible: false,
  connectorKey: 'dingtalk',
  name: '钉钉知识库',
  target_notebook_id: '',
  enabled: true,
  config_text: '{}',
  saving: false,
})

const accessScopes = ref<AccessScope[]>([])

const configDialog = reactive({
  visible: false,
  connectionId: '',
  connectorKey: '',
  target_notebook_id: '',
  enabled: true,
  config_text: '{}',
  saving: false,
})

const syncDialog = reactive({
  visible: false,
  connectionId: '',
  connectorKey: '',
  mode: 'incremental',
  pilot_limit: 0,
  selected_space_ids: [] as string[],
})

const itemsDialog = reactive({ visible: false, connectionId: '' })
const items = ref<SourceItem[]>([])
const itemsLoading = ref(false)

const errorsDialog = reactive({ visible: false, runId: '' })
const errors = ref<SourceSyncError[]>([])
const errorsLoading = ref(false)

// P38：统一数据源路径 → Notebook 权限映射
const pathMappings = ref<PathMapping[]>([])
const mappingsLoading = ref(false)
const mappingConnectionId = ref('')
// 已发现的路径（映射选择优先来源）
const discoveredPaths = ref<{ path_namespace: string; folder_path: string }[]>([])
const mappingCapability = ref<'path' | 'connection_level' | 'none'>('none')
const mappingDialog = reactive({
  visible: false,
  id: '',
  connection_id: '',
  path_namespace: '',
  folder_path: '',
  notebook_id: '',
  saving: false,
})

// J-1 最终遗留：独立 Notebook 权限编辑
const scopeDialog = reactive({
  visible: false,
  notebookId: '',
  notebookName: '',
  scopeType: 'company' as 'company' | 'admin' | 'groups',
  groupNames: [] as string[],
  saving: false,
})

const CONNECTOR_NAMES: Record<string, string> = {
  dingtalk: '钉钉知识库',
  gitlab: 'GitLab',
}

function connectorName(key: string): string {
  return CONNECTOR_NAMES[key] || key
}

function setupOf(key: string): ConnectorSetupStatus | undefined {
  return connectorSetup.value[key]
}

function connectionsOf(key: string): SourceConnection[] {
  return Object.values(connections.value).filter((c) => c.connector_key === key)
}

function primaryConn(key: string): SourceConnection | undefined {
  return connectionsOf(key)[0]
}

function connectorStatusLabel(key: string): string {
  const conn = primaryConn(key)
  if (!conn) return '待配置'
  return conn.enabled ? '已启用' : '已停用'
}

function connectorStatusTag(key: string): 'info' | 'success' | 'warning' {
  const conn = primaryConn(key)
  if (!conn) return 'info'
  return conn.enabled ? 'success' : 'warning'
}

function notebookById(id: string | null | undefined): SourceTargetNotebook | undefined {
  if (!id) return undefined
  return targetNotebooks.value.find((n) => n.id === id)
}

function targetNotebookLabel(key: string): string {
  const id = primaryConn(key)?.target_notebook_id || setupOf(key)?.target_notebook_id
  if (!id) return '未配置'
  const nb = notebookById(id)
  return nb ? nb.name : '未配置'
}

function targetGroupLabel(key: string): string {
  const id = primaryConn(key)?.target_notebook_id || setupOf(key)?.target_notebook_id
  if (!id) return '未配置'
  return notebookById(id)?.group_id ? '已配置' : '未配置'
}

function blockersFor(key: string) {
  return setupOf(key)?.setup_blockers || []
}

function blockerType(code: string): 'error' | 'warning' | 'info' {
  if (code === 'credentials_missing' || code === 'target_notebook_no_group') return 'error'
  if (code === 'connector_disabled') return 'warning'
  return 'info'
}

// J-1：Notebook 可见范围只读标签（company / 多组 / admin）。
function scopeLabelFor(notebookId: string | null | undefined): string {
  if (!notebookId) return ''
  return notebookById(notebookId)?.scope_label || ''
}

function scopeTagFor(notebookId: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' {
  if (!notebookId) return 'info'
  const label = notebookById(notebookId)?.scope_label || ''
  if (label.startsWith('全公司')) return 'success'
  if (label.startsWith('组：')) return 'warning'
  if (label.startsWith('仅管理员')) return 'danger'
  return 'info'
}

// J-1 最终返工：「指定业务组」下拉只显示真正业务组（type=ldap），
// 排除 __local_admin__ 等 admin/wiki_editor 角色组；admin 走独立「仅管理员」选项。
const businessScopeOptions = computed(() =>
  accessScopes.value.filter((s) => s.type === 'ldap')
)

// J-1 最终返工：文件夹映射只能选择权限域有效的 Notebook；unknown 禁用。
function isNotebookScopeUnknown(notebook: SourceTargetNotebook): boolean {
  return (notebook.scope_label || '').startsWith('未确定')
}
const eligibleTargetNotebooks = computed(() =>
  targetNotebooks.value.filter((n) => !isNotebookScopeUnknown(n))
)

const RUN_STATUS: Record<string, { label: string; tag: 'info' | 'warning' | 'success' | 'danger' }> = {
  queued: { label: '排队中', tag: 'info' },
  running: { label: '运行中', tag: 'warning' },
  succeeded: { label: '成功', tag: 'success' },
  failed: { label: '失败', tag: 'danger' },
  cancelled: { label: '已取消', tag: 'info' },
}

function runStatusLabel(s: string) { return RUN_STATUS[s]?.label || s }
function runStatusTag(s: string) { return RUN_STATUS[s]?.tag || 'info' }

const STAGE_TEXT: Record<string, string> = {
  remote: '刷新远程数据',
  'remote:scan': '扫描文档清单',
  'remote:download': '下载原文件',
  'remote:convert': '转换 Markdown',
  discover: '读取变更',
  index: '索引与证据',
  compile: '编译卡片',
  done: '完成',
}

function stageText(stage: string | null): string {
  if (!stage) return '等待 Worker'
  return STAGE_TEXT[stage] || stage
}

function itemStateTag(s: string): 'success' | 'info' | 'danger' | 'warning' {
  if (s === 'active') return 'success'
  if (s === 'deleted') return 'danger'
  if (s === 'error') return 'danger'
  if (s === 'skipped') return 'warning'
  return 'info'
}

function latestRun(connId: string): SourceSyncRun | undefined {
  return runs.value.find((r) => r.connection_id === connId)
}

function canRetry(run: SourceSyncRun): boolean {
  return run.status === 'failed' && run.failed_count > 0
}

function errorCategoryLabel(code: string | null): string {
  const map: Record<string, string> = {
    CREDENTIALS_MISSING: '凭证缺失',
    AUTH_FAILED: '凭证失效',
    PERMISSION_DENIED: '权限不足',
    ACL_FAILED: '权限不足',
    NETWORK_UNREACHABLE: '网络错误',
    DOC_DOWNLOAD_FAILED: '下载失败',
    CONVERSION_FAILED: '转换失败',
    LLM_DEGRADED: 'GLM 降级',
  }
  return map[code || ''] || code || '错误'
}

function formatTime(s: string | null): string {
  if (!s) return '-'
  try { return new Date(s).toLocaleString() } catch { return s }
}

async function load() {
  loading.value = true
  try {
    const data = await sourceApi.list()
    connectors.value = data.connectors
    connectorSetup.value = data.connector_setup || {}
    targetNotebooks.value = data.target_notebooks || []
    connections.value = Object.fromEntries(data.connections.map((c) => [c.id, c]))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载数据源失败')
  } finally {
    loading.value = false
  }
  await loadRuns()
}

async function loadRuns() {
  runsLoading.value = true
  try {
    runs.value = await sourceApi.runs()
  } catch {
    runs.value = []
  } finally {
    runsLoading.value = false
  }
}

async function loadPathMappings() {
  mappingsLoading.value = true
  try {
    pathMappings.value = await sourceApi.listPathMappings(mappingConnectionId.value || undefined)
  } catch (e: any) {
    pathMappings.value = []
    ElMessage.error(e?.response?.data?.detail || '加载路径映射失败')
  } finally {
    mappingsLoading.value = false
  }
}

function connectionName(id: string): string {
  const c = connections.value[id]
  if (!c) return id
  return `${connectorName(c.connector_key)} - ${c.name}`
}

// 钉钉连接的映射计数（连接卡片 meta 展示）。
const dingtalkMappingCount = computed(() => {
  const dtConn = Object.values(connections.value).find((c) => c.connector_key === 'dingtalk')
  if (!dtConn) return 0
  return pathMappings.value.filter((m) => m.connection_id === dtConn.id).length
})

// 连接路径能力提示。
const mappingHasPathCapability = computed(() => mappingCapability.value === 'path')
const mappingCapabilityHint = computed(() => {
  if (mappingCapability.value === 'path') return '该连接支持路径映射'
  return '该数据源使用连接级目标 Notebook'
})

async function onMappingConnectionChange(connectionId: string) {
  if (!connectionId) return
  try {
    const cap = await sourceApi.listDiscoveredPaths(connectionId)
    mappingCapability.value = cap.capability
    discoveredPaths.value = cap.paths || []
  } catch {
    mappingCapability.value = 'none'
    discoveredPaths.value = []
  }
}

function openMappingDialog(row?: PathMapping) {
  mappingDialog.id = row?.id || ''
  mappingDialog.connection_id = row?.connection_id || mappingConnectionId.value || ''
  mappingDialog.path_namespace = row?.path_namespace || ''
  mappingDialog.folder_path = row?.folder_path || ''
  mappingDialog.notebook_id = row?.notebook_id || ''
  mappingDialog.visible = true
  mappingCapability.value = 'none'
  discoveredPaths.value = []
  if (mappingDialog.connection_id) {
    onMappingConnectionChange(mappingDialog.connection_id)
  }
}

// 从已发现路径选择时，同时填入 path_namespace 与 folder_path（避免同名目录歧义）。
function onSelectDiscoveredPath(val: string | number | undefined) {
  const v = String(val ?? '')
  const sep = '|||'
  if (v.includes(sep)) {
    const [ns, path] = v.split(sep)
    mappingDialog.path_namespace = ns || ''
    mappingDialog.folder_path = path || ''
  }
  // 手动输入（allow-create）：v-model 已更新 folder_path，path_namespace 由用户填写，保持不变。
}

async function saveMapping() {
  if (!mappingDialog.connection_id) {
    ElMessage.warning('请选择数据源连接')
    return
  }
  if (!mappingDialog.folder_path.trim()) {
    ElMessage.warning('请填写路径/目录')
    return
  }
  if (!mappingDialog.notebook_id) {
    ElMessage.warning('请选择目标 Notebook')
    return
  }
  mappingDialog.saving = true
  try {
    const payload = {
      connection_id: mappingDialog.connection_id,
      path_namespace: mappingDialog.path_namespace.trim() || undefined,
      folder_path: mappingDialog.folder_path.trim(),
      notebook_id: mappingDialog.notebook_id,
    }
    if (mappingDialog.id) {
      await sourceApi.updatePathMapping(mappingDialog.id, payload)
    } else {
      await sourceApi.createPathMapping(payload)
    }
    ElMessage.success('映射已保存')
    mappingDialog.visible = false
    await loadPathMappings()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存映射失败')
  } finally {
    mappingDialog.saving = false
  }
}

async function removeMapping(row: PathMapping) {
  try {
    await sourceApi.deletePathMapping(row.id)
    ElMessage.success('映射已删除')
    await loadPathMappings()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除映射失败')
  }
}

// ---- J-1 最终遗留：独立 Notebook 权限编辑 ----

function notebookRowClassName({ row }: { row: SourceTargetNotebook }) {
  return row.id === highlightNotebookId.value ? 'highlight-row' : ''
}

function openNotebookScopeDialog(notebook: SourceTargetNotebook) {
  scopeDialog.notebookId = notebook.id
  scopeDialog.notebookName = notebook.name
  const label = notebook.scope_label || ''
  const groups = notebook.groups || []
  if (label.startsWith('仅管理员')) {
    scopeDialog.scopeType = 'admin'
    scopeDialog.groupNames = []
  } else if (groups.length) {
    scopeDialog.scopeType = 'groups'
    scopeDialog.groupNames = [...groups]
  } else {
    scopeDialog.scopeType = 'company'
    scopeDialog.groupNames = []
  }
  scopeDialog.visible = true
}

async function saveNotebookScope() {
  if (!scopeDialog.notebookId) {
    ElMessage.warning('未选择知识库')
    return
  }
  if (scopeDialog.scopeType === 'groups' && !scopeDialog.groupNames.length) {
    ElMessage.warning('请至少选择一个业务组')
    return
  }
  scopeDialog.saving = true
  try {
    // J-1 最终返工：只调用原子权限 API（单事务，不再串行 updateAccessScope+groups）。
    await sourceApi.updateNotebookPermissions(scopeDialog.notebookId, {
      scope_type: scopeDialog.scopeType,
      group_names: scopeDialog.scopeType === 'groups' ? scopeDialog.groupNames : [],
    })
    ElMessage.success('权限已保存')
    scopeDialog.visible = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存权限失败')
  } finally {
    scopeDialog.saving = false
  }
}

async function openCreateDialog(key: string) {
  createDialog.connectorKey = key
  createDialog.name = CONNECTOR_NAMES[key] || key
  createDialog.target_notebook_id = ''
  createDialog.enabled = true
  createDialog.config_text = '{}'
  createDialog.visible = true
  try {
    accessScopes.value = await sourceApi.accessScopes()
  } catch (e: any) {
    accessScopes.value = []
    ElMessage.error(
      e?.response?.data?.detail ||
      '权限范围加载失败，请确认后端服务已更新'
    )
  }
}

async function submitCreate() {
  if (!createDialog.name.trim()) {
    ElMessage.warning('请填写连接名称')
    return
  }
  // J-1 最终遗留：钉钉连接不配置目标知识库（归属由文件夹映射决定，权限在独立区编辑）；
  // 非钉钉 Connector 仍必须绑定目标知识库。
  if (createDialog.connectorKey !== 'dingtalk' && !createDialog.target_notebook_id) {
    ElMessage.warning('请选择目标知识库')
    return
  }
  let configJson = {}
  try { configJson = JSON.parse(createDialog.config_text || '{}') } catch { ElMessage.warning('配置 JSON 格式错误'); return }

  createDialog.saving = true
  try {
    await sourceApi.create({
      connector_key: createDialog.connectorKey,
      name: createDialog.name.trim(),
      target_notebook_id:
        createDialog.connectorKey === 'dingtalk' ? undefined : (createDialog.target_notebook_id || undefined),
      config_json: configJson,
      default_acl_json: {},
    })
    ElMessage.success('连接已创建')
    createDialog.visible = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    createDialog.saving = false
  }
}

function openConfigDialog(conn: SourceConnection) {
  configDialog.connectionId = conn.id
  configDialog.connectorKey = conn.connector_key
  configDialog.target_notebook_id = conn.target_notebook_id || ''
  configDialog.enabled = conn.enabled
  let parsed = {}
  try { parsed = JSON.parse(conn.config_json || '{}') } catch { parsed = {} }
  configDialog.config_text = JSON.stringify(parsed, null, 2)
  configDialog.visible = true
}

async function saveConfig() {
  // J-1 最终返工：钉钉编辑连接不要求 target_notebook_id（归属由文件夹映射决定）。
  if (configDialog.connectorKey !== 'dingtalk' && !configDialog.target_notebook_id) {
    ElMessage.warning('请选择已绑定权限组的目标知识库')
    return
  }
  configDialog.saving = true
  try {
    let configJson = {}
    try { configJson = JSON.parse(configDialog.config_text || '{}') } catch { ElMessage.warning('配置 JSON 格式错误'); return }
    await sourceApi.update(configDialog.connectionId, {
      config_json: configJson,
      // J-1 最终遗留：钉钉编辑连接不修改 target_notebook_id（归属由文件夹映射决定）。
      target_notebook_id:
        configDialog.connectorKey === 'dingtalk' ? undefined : (configDialog.target_notebook_id || null),
      enabled: configDialog.enabled,
    })
    ElMessage.success('已保存')
    configDialog.visible = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    configDialog.saving = false
  }
}

async function testConnection(conn: SourceConnection) {
  try {
    const res = await sourceApi.test(conn.id)
    if (res.ok) ElMessage.success(`连接测试通过：${res.message}`)
    else ElMessage.error(`连接测试失败（${errorCategoryLabel(res.error_code)}）：${res.message}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '测试失败')
  }
}

async function openSyncDialog(conn: SourceConnection) {
  syncDialog.connectionId = conn.id
  syncDialog.connectorKey = conn.connector_key
  syncDialog.mode = 'incremental'
  syncDialog.pilot_limit = 0
  syncDialog.selected_space_ids = []
  syncDialog.visible = true
}

async function confirmSync() {
  try {
    const res = await sourceApi.sync(syncDialog.connectionId, {
      mode: syncDialog.mode,
      pilot_limit: syncDialog.pilot_limit > 0 ? syncDialog.pilot_limit : null,
      selected_space_ids: syncDialog.selected_space_ids,
    })
    ElMessage.success(`同步已排队（${res.mode}，run ${res.run_id.slice(0, 8)}）`)
    syncDialog.visible = false
    await loadRuns()
    startPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '触发同步失败')
  }
}

async function cancelRun(run: SourceSyncRun) {
  try {
    await sourceApi.cancelRun(run.id)
    ElMessage.success('已请求取消')
    await loadRuns()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '取消失败')
  }
}

async function retryRun(run: SourceSyncRun) {
  try {
    const res = await sourceApi.retryRun(run.id)
    ElMessage.success(`已重试 ${res.retry_count} 个失败项`)
    await loadRuns()
    startPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重试失败')
  }
}

async function viewErrors(run: SourceSyncRun) {
  errorsDialog.runId = run.id
  errorsDialog.visible = true
  errorsLoading.value = true
  try {
    errors.value = await sourceApi.runErrors(run.id)
  } catch {
    errors.value = []
  } finally {
    errorsLoading.value = false
  }
}

async function viewItems(conn: SourceConnection) {
  itemsDialog.connectionId = conn.id
  itemsDialog.visible = true
  itemsLoading.value = true
  try {
    items.value = await sourceApi.items(conn.id)
  } catch {
    items.value = []
  } finally {
    itemsLoading.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    const active = runs.value.some((r) => r.status === 'running' || r.status === 'queued')
    if (active) await loadRuns()
    else stopPolling()
  }, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function handleNotebookQuery() {
  const nbId = route.query.notebook
  if (typeof nbId !== 'string' || !nbId) return
  const nb = notebookById(nbId)
  if (!nb) return // 无效/已删除 ID 安全退回列表，不报错
  highlightNotebookId.value = nb.id
  nextTick(() => {
    notebookSectionEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
  openNotebookScopeDialog(nb)
}

onMounted(async () => {
  await load()
  await loadPathMappings()
  // 若存在运行中的任务，自动轮询
  if (runs.value.some((r) => r.status === 'running' || r.status === 'queued')) startPolling()
  handleNotebookQuery()
})

onBeforeUnmount(stopPolling)
</script>

<style scoped>
.sources-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 24px;
  overflow-y: auto;
}
.sources-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-title {
  font-size: 18px;
  font-weight: 700;
  color: #111827;
  margin: 0;
}
.header-actions {
  display: flex;
  gap: 8px;
}
.section-title {
  font-size: 14px;
  font-weight: 600;
  color: #374151;
  margin: 0 0 8px;
}
.connections-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.connection-card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
}
.conn-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.conn-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.conn-name {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}
.conn-actions {
  display: flex;
  gap: 6px;
}
.conn-meta {
  margin-top: 8px;
  font-size: 12px;
  color: #6b7280;
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.blockers {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.latest-run {
  margin-top: 10px;
  font-size: 12px;
  color: #4b5563;
  display: flex;
  align-items: center;
  gap: 10px;
}
.run-stage {
  color: #6b7280;
}
.run-counters {
  color: #6b7280;
}
.warn-text { color: #f59e0b; }
.mappings-table :deep(.highlight-row) { background-color: #fdf6ec; }
.error-text { color: #ef4444; }
.field-hint {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 4px;
}
.empty-tip {
  font-size: 13px;
  color: #9ca3af;
  padding: 20px;
  text-align: center;
}
.runs-table { min-height: 100px; }
.mappings-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.mappings-table { min-height: 80px; }
.items-list, .errors-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid #f3f4f6;
  border-radius: 6px;
  font-size: 13px;
}
.item-external {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: monospace;
}
.item-page {
  font-size: 12px;
  color: #6b7280;
}
.error-row {
  padding: 10px;
  border: 1px solid #fee2e2;
  border-radius: 6px;
  background: #fef2f2;
}
.error-stage {
  font-size: 12px;
  color: #6b7280;
}
.error-code {
  font-size: 13px;
  font-weight: 600;
  color: #dc2626;
  margin: 4px 0;
}
.error-msg {
  font-size: 13px;
  color: #4b5563;
}
</style>
