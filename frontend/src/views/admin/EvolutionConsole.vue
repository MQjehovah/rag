<template>
  <div class="evolution-console">
    <h2>WikiSkill 演化管理控制台</h2>
    <el-alert
      v-if="status && status.console === 'disabled'"
      type="warning"
      :closable="false"
      title="功能未启用"
      description="后端未启用演化实验控制台（wikiskill_console_enabled=false）。"
    />
    <el-alert
      v-else-if="status && status.console === 'unavailable'"
      type="info"
      :closable="false"
      title="控制台不可用"
      :description="status.reason || '未配置服务端实验根映射'"
    />
    <el-alert
      v-else-if="fetchError"
      type="error"
      :closable="false"
      title="查询失败"
      :description="fetchError"
    />
    <el-alert
      v-else-if="loaded && experiments.length === 0"
      type="info"
      :closable="false"
      title="无数据"
      description="当前实验根下没有任何演化实验记录。"
    />

    <template v-if="status && status.console === 'ok'">
      <div class="meta-banner">
        <el-tag type="info" size="small">
          真实链路：{{ status.real_link_verified ? '已验证' : '未验证' }}
        </el-tag>
        <el-tag type="info" size="small">
          效果：{{ status.effect_verified ? '已验证' : '未验证' }}
        </el-tag>
        <span class="muted">{{ status.reason || '受控演化管理：创建/运行控制/晋升与回退' }}</span>
      </div>

      <el-form inline>
        <el-form-item label="实验根">
          <el-select v-model="rootId" @change="reloadExperiments">
            <el-option
              v-for="r in status.roots"
              :key="r.id"
              :label="r.label"
              :value="r.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" plain @click="openCreate">新建实验</el-button>
        </el-form-item>
        <el-form-item>
          <el-button @click="reloadExperiments">刷新</el-button>
        </el-form-item>
      </el-form>

      <el-table
        :data="experiments"
        border
        :row-class-name="rowClass"
        @row-click="openExperiment"
      >
        <el-table-column prop="experiment_id" label="实验 ID" width="240" />
        <el-table-column prop="dataset_version" label="数据集" width="150" />
        <el-table-column prop="grader_version" label="评分器" width="190" />
        <el-table-column prop="status" label="状态" width="100" />
        <el-table-column label="最优(通过/总数)" width="130">
          <template #default="{ row }">
            {{
              row.best_score_total == null
                ? '—'
                : row.best_score_passed + ' / ' + row.best_score_total
            }}
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="200" />
      </el-table>
      <el-pagination
        layout="prev, pager, next, total"
        :total="total"
        :page-size="pageSize"
        :current-page="page"
        @current-change="onPage"
      />

      <el-drawer
        v-model="detailOpen"
        size="70%"
        :title="selected ? selected.experiment_id : '详情'"
      >
        <template v-if="detail">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="workspace_id">
              {{ detail.workspace_id }}
            </el-descriptions-item>
            <el-descriptions-item label="dataset">
              {{ detail.dataset_version }}
            </el-descriptions-item>
            <el-descriptions-item label="当前技能成员">
              {{ memberSummary }}
            </el-descriptions-item>
            <el-descriptions-item label="最优分数">
              {{
                detail.best_score
                  ? detail.best_score.passed + ' / ' + detail.best_score.total
                  : '—'
              }}
            </el-descriptions-item>
          </el-descriptions>

          <h3>运行（{{ detail.runs_summary?.length || 0 }}）</h3>
          <el-table :data="detail.runs_summary || []" border size="small">
            <el-table-column prop="run_id" label="run_id" width="240" />
            <el-table-column label="模式" width="90">
              <template #default="{ row }">
                <el-tag
                  :type="row.model_mode === 'real' ? 'warning' : 'info'"
                  size="small"
                >
                  {{ row.model_mode }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="120" />
            <el-table-column prop="stop_reason" label="停止原因" />
            <el-table-column label="模型调用 已用/上限" width="150">
              <template #default="{ row }">
                {{ row.used_model_calls == null ? '—' : row.used_model_calls }} /
                {{ row.model_cap == null ? '不限' : row.model_cap }}
              </template>
            </el-table-column>
            <el-table-column label="token/费用" width="130">
              <template #default="{ row }">
                <el-tag v-if="row.usage_unknown" type="info" size="small">
                  未知（不显示为 0）
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="190">
              <template #default="{ row }">
                <template v-if="row.status === 'queued'">
                  <el-button size="small" type="success" link
                             @click.stop="runAction(row, 'start')">
                    启动
                  </el-button>
                  <el-button size="small" type="danger" link
                             @click.stop="runAction(row, 'cancel')">
                    取消
                  </el-button>
                </template>
                <template v-else-if="row.status === 'running'">
                  <el-button size="small" type="warning" link
                             @click.stop="runAction(row, 'pause')">
                    暂停
                  </el-button>
                  <el-button size="small" type="danger" link
                             @click.stop="runAction(row, 'cancel')">
                    取消
                  </el-button>
                </template>
                <template v-else-if="row.status === 'paused'">
                  <el-button size="small" type="success" link
                             @click.stop="runAction(row, 'resume')">
                    恢复
                  </el-button>
                  <el-button size="small" type="danger" link
                             @click.stop="runAction(row, 'cancel')">
                    取消
                  </el-button>
                </template>
                <span v-else class="muted">终态</span>
              </template>
            </el-table-column>
            <el-table-column label="轨迹" width="90">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="primary"
                  link
                  @click.stop="loadTrajectories(row.run_id)"
                >
                  查看
                </el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="promo-box">
            <h3>业务晋升 / 回退（目标作用域 = 业务库 workspace_id）</h3>
            <el-form inline size="small">
              <el-form-item label="workspace_id">
                <el-input v-model="promoWs" style="width: 300px"
                          placeholder="业务 WikiWorkspace id（须 active）" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" size="small" :loading="promoBusy"
                           :disabled="!promoWs" @click="loadPromoPreview">
                  晋升预览
                </el-button>
                <el-button type="warning" size="small" :loading="promoBusy"
                           :disabled="!promoWs" @click="doRollback">
                  回退到历史版本
                </el-button>
              </el-form-item>
            </el-form>
            <el-alert v-if="promoBindingError" type="warning" :closable="false"
                      :description="promoBindingError" />
            <el-descriptions v-if="promoBinding" :column="2" border size="small">
              <el-descriptions-item label="业务库当前状态">
                {{
                  promoBinding.no_binding
                    ? '无绑定（首次绑定令牌 rev=0）'
                    : String(promoBinding.current?.kind || '')
                }}
              </el-descriptions-item>
              <el-descriptions-item label="当前 rev / 集合哈希">
                {{ promoBinding.expected_rev ?? '—' }} /
                {{ shortHash(promoBinding.expected_set_hash) }}
              </el-descriptions-item>
            </el-descriptions>
            <el-alert
              v-if="rollbackUnknown"
              type="warning"
              :closable="false"
              title="上次回退结果未知，可重试原请求"
              :description="
                '回退请求未收到明确结果（网络断开/超时/5xx）。点“重试上次回退（同键同请求）”' +
                '将原样重发同一请求：复用同一 workspace_id / expected_rev / expected_set_hash / ' +
                'idempotency_key，不刷新绑定、不生成新键。'
              "
            />
            <el-button
              v-if="rollbackPending && rollbackUnknown"
              type="warning"
              size="small"
              :loading="promoBusy"
              @click="retryRollback"
            >
              重试上次回退（同键同请求）
            </el-button>
            <el-alert
              v-if="rollbackError && !rollbackUnknown"
              type="error"
              :closable="false"
              title="回退操作失败"
              :description="rollbackError"
            />
            <p class="muted">
              提交将原样携带本次预览与当前绑定令牌；成功后同请求（网络超时）重试
              复用同一幂等键。冲突（409）不会自动取新令牌覆盖——请重新预览。
            </p>
            <el-alert v-if="promoError" type="error" :closable="false"
                      title="晋升操作失败" :description="promoError" />
            <template v-if="promoPreview">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="来源实验">
                  {{ promoPreview.experiment_id }}
                </el-descriptions-item>
                <el-descriptions-item label="目标集合（完整成员）">
                  {{ memberList(promoPreview.members) || '—' }}
                </el-descriptions-item>
                <el-descriptions-item label="目标集合哈希">
                  {{ shortHash(promoPreview.exp_set_hash) || '—' }}
                </el-descriptions-item>
                <el-descriptions-item label="证据模式">
                  {{ promoPreview.model_mode_evidence }}
                  （真实链路：{{ promoPreview.real_link_verified ? '已验证' : '未验证' }}）
                </el-descriptions-item>
                <el-descriptions-item label="最优评估">
                  {{
                    promoPreview.evidence &&
                    promoPreview.evidence.best_score
                      ? promoPreview.evidence.best_score.passed + ' / ' +
                        promoPreview.evidence.best_score.total
                      : '—'
                  }}
                </el-descriptions-item>
              </el-descriptions>
              <el-alert v-if="promoPreview.blocked && promoPreview.blocked.length"
                        type="error" :closable="false" title="阻止晋升（无可绕过选项）"
                        :description="promoPreview.blocked.join('；')" />
              <p class="muted">模拟证据不构成真实效果证据；效果仍未验证。</p>
              <el-button type="primary" size="small" :loading="promoBusy"
                         :disabled="!promoPreview.promotable || !promoBinding"
                         @click="doPromote">
                执行晋升（携带预览目标 + 当前令牌）
              </el-button>
              <p class="muted">
                技能回退只影响后续编译请求，不恢复已经发布的 Wiki 内容。
              </p>
            </template>
          </div>

          <h3>技能集合与候选（版本不可变；正文与差异按需查看）</h3>
          <el-table v-if="skills && skills.versions" :data="skills.versions" border size="small">
            <el-table-column prop="version_id" label="version_id" width="230" />
            <el-table-column prop="seq" label="seq" width="56" />
            <el-table-column prop="content_hash" label="content_hash" />
            <el-table-column label="正文/目的字符数" width="120">
              <template #default="{ row }">
                {{ row.skill_md_chars }} / {{ row.purpose_md_chars }}
              </template>
            </el-table-column>
            <el-table-column label="查看" width="170">
              <template #default="{ row }">
                <el-button size="small" type="primary" link
                           @click.stop="viewContent(row)">
                  查看正文
                </el-button>
                <el-button size="small" type="primary" link
                           @click.stop="prepareDiff(row)">
                  对比差异
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="无技能版本" />
          <p class="muted">
            正文与差异视为不可信文本：仅原样展示，不解析、不执行 HTML 或指令。
          </p>
          <p v-if="skills && skills.candidates && skills.candidates.length" class="muted">
            候选 {{ skills.candidates.length }} 条：此处仅摘要（diff 元数据）；精确正文
            差异经“对比差异”在任意两个版本间查看。
          </p>

          <h3>门控历史 / 评估 / 经验模式（仅摘要）</h3>
          <el-tabs>
            <el-tab-pane label="门控历史">
              <el-table :data="gateItems" border size="small">
                <el-table-column prop="decision" label="决策" width="110" />
                <el-table-column prop="reason" label="理由" />
              </el-table>
            </el-tab-pane>
            <el-tab-pane label="评估">
              <el-table :data="evalItems" border size="small">
                <el-table-column prop="kind" label="kind" width="110" />
                <el-table-column prop="valid" label="valid" width="70" />
                <el-table-column label="分数" width="90">
                  <template #default="{ row }">
                    {{
                      row.valid
                        ? row.main_passed + ' / ' + row.main_total
                        : '无效(诊断)'
                    }}
                  </template>
                </el-table-column>
                <el-table-column label="v2 评审" width="170">
                  <template #default="{ row }">
                    <el-tag
                      v-if="row.pending_count > 0"
                      type="warning"
                      size="small"
                    >
                      待审 {{ row.pending_count }} 任务
                    </el-tag>
                    <el-tag v-else type="info" size="small">
                      评审请求 {{ row.review_requests || 0 }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="invalid_reason" label="invalid_reason" />
              </el-table>
            </el-tab-pane>
            <el-tab-pane label="经验模式">
              <el-table :data="patternItems" border size="small">
                <el-table-column prop="pattern_id" label="pattern_id" />
                <el-table-column prop="status" label="status" width="100" />
                <el-table-column prop="title" label="title" />
              </el-table>
            </el-tab-pane>
          </el-tabs>
          <el-empty v-if="detailEmpty" description="该实验无运行/评估/门控记录" />
        </template>
      </el-drawer>

      <el-dialog v-model="trajOpen" title="轨迹摘要（按需加载）" width="70%">
        <p class="muted">仅摘要：不包含正文、参考答案或来源内容。</p>
        <el-table :data="trajItems" border size="small">
          <el-table-column prop="execution_id" label="execution_id" width="220" />
          <el-table-column prop="split" label="split" width="80" />
          <el-table-column prop="run_status" label="状态" width="120" />
          <el-table-column prop="failure_kind" label="失败类" />
          <el-table-column label="已发布" width="90">
            <template #default="{ row }">{{ row.published ? '是' : '否' }}</template>
          </el-table-column>
        </el-table>
      </el-dialog>

      <el-dialog
        v-model="contentOpen"
        :title="contentData ? '技能正文（不可变版本）' : '技能正文'"
        width="78%"
        top="4vh"
      >
        <el-alert
          v-if="contentError"
          type="error"
          :closable="false"
          title="正文读取失败"
          :description="contentError"
        />
        <template v-if="contentData">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="version_id">
              {{ contentData.version_id }}
            </el-descriptions-item>
            <el-descriptions-item label="父版本">
              {{ contentData.parent_version_id || '（无）' }}
            </el-descriptions-item>
            <el-descriptions-item label="content_hash">
              {{ contentData.content_hash }}
            </el-descriptions-item>
            <el-descriptions-item label="来源">
              {{ contentData.source_type }}
            </el-descriptions-item>
          </el-descriptions>
          <p class="muted">
            完整性校验 {{ contentData.integrity }}；正文视为不可信文本原样展示
            （不解析/不执行 HTML 或指令）。截断部分不在此展示。
          </p>
          <el-tabs>
            <el-tab-pane :label="mdLabel(contentData.skill_md)">
              <pre class="raw-text">{{ contentData.skill_md.body }}</pre>
            </el-tab-pane>
            <el-tab-pane :label="mdLabel(contentData.purpose_md)">
              <pre class="raw-text">{{ contentData.purpose_md.body }}</pre>
            </el-tab-pane>
          </el-tabs>
        </template>
      </el-dialog>

      <el-dialog v-model="diffOpen" title="精确版本正文差异" width="78%" top="4vh">
        <el-form inline>
          <el-form-item label="基准版本（旧）">
            <el-select
              v-model="diffBase"
              filterable
              placeholder="选择基准版本"
              style="width: 300px"
            >
              <el-option
                v-for="v in versionOptions"
                :key="v.version_id"
                :label="v.version_id + ' · ' + v.skill_id + ' #' + v.seq"
                :value="v.version_id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="对比版本（新）">
            <el-select
              v-model="diffHead"
              filterable
              placeholder="选择对比版本"
              style="width: 300px"
            >
              <el-option
                v-for="v in versionOptions"
                :key="v.version_id"
                :label="v.version_id + ' · ' + v.skill_id + ' #' + v.seq"
                :value="v.version_id"
              />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :disabled="!diffBase || !diffHead"
                       :loading="diffLoading" @click="compareDiff">
              查看差异
            </el-button>
          </el-form-item>
        </el-form>
        <el-alert
          v-if="diffError"
          type="error"
          :closable="false"
          title="差异读取失败"
          :description="diffError"
        />
        <template v-if="diffData">
          <p class="muted">
            差异为文本（unified diff），视为不可信文本原样展示；超长已截断。
          </p>
          <el-tabs v-if="diffData.files">
            <el-tab-pane v-for="f in diffData.files" :key="f.label" :label="f.label">
              <el-alert
                v-if="f.truncated"
                type="info"
                :closable="false"
                title="差异过长，仅展示前部"
              />
              <pre class="raw-text">{{ f.body || '（无差异）' }}</pre>
            </el-tab-pane>
          </el-tabs>
        </template>
      </el-dialog>

      <el-dialog v-model="createOpen" title="新建演化实验（模拟/真实，管理员）"
                 width="64%">
        <el-form label-width="150px">
          <el-form-item label="数据集">
            <el-select
              v-model="createForm.dataset_version"
              style="width: 360px"
              @change="onDatasetChange"
            >
              <el-option
                v-for="d in metaDatasets"
                :key="d.dataset_version"
                :value="d.dataset_version"
                :disabled="!d.grader_ready"
                :label="d.dataset_version + '（' + d.grader_purpose + '）'"
              />
            </el-select>
            <p v-if="selDsMeta" class="muted">
              评分器 {{ selDsMeta.grader_version }}（{{ selDsMeta.grader_role }}）
              — {{ dsMetaLines.join('；') }}
            </p>
            <p v-else class="muted">
              选择数据集后显示服务端返回的评分器/校准/评审契约元数据。
            </p>
          </el-form-item>
          <el-form-item label="模式">
            <el-radio-group v-model="createForm.model_mode">
              <el-radio value="simulated">模拟（可执行）</el-radio>
              <el-radio value="real">真实（仅创建排队记录）</el-radio>
            </el-radio-group>
            <p v-if="createForm.model_mode === 'real'" class="muted">
              real 仅创建 queued 记录：创建阶段不调用任何模型；启动仍需服务端
              授权开关（当前{{ realStartEnabled ? '已开启' : '未开启' }}）与逐次完整确认
              （数据集/迭代/预算/配置指纹/评审指纹），确认后由独立 worker 执行。
            </p>
            <p v-else class="muted">
              模拟模式仅作隔离工程验证，不代表真实链路或效果验证；不会被升级为 real。
            </p>
          </el-form-item>
          <el-form-item label="经验模式">
            <el-radio-group v-model="createForm.experience">
              <el-radio value="full">full（完整经验闭环）</el-radio>
              <el-radio value="none">none（无经验对照）</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="迭代轮数">
            <el-input-number v-model="createForm.max_iterations" :min="1"
                             :max="20" />
          </el-form-item>
          <el-form-item label="请求预算（模型调用）">
            <el-input-number v-model="createForm.max_model_calls" :min="2"
                             :max="5000" />
          </el-form-item>
          <el-form-item label="工具调用预算">
            <el-input-number v-model="createForm.max_tool_calls" :min="1"
                             :max="5000" />
          </el-form-item>
          <el-form-item label="时间预算（秒）">
            <el-input-number v-model="createForm.max_seconds" :min="1"
                             :max="86400" />
          </el-form-item>
        </el-form>
        <el-alert v-if="createError" type="error" :closable="false"
                  title="创建失败" :description="createError" />
        <template #footer>
          <el-button @click="createOpen = false">取消</el-button>
          <el-button type="primary" :loading="createBusy" @click="submitCreate">
            创建（queued）
          </el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<script lang="ts">
import { defineComponent } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '../../api/evolutionConsole'
import * as adminApi from '../../api/evolutionAdmin'
import {
  buildRealStartConfirm,
  createPendingRollback,
  classifyRollbackError,
  type StartPreview,
  type PendingRollback,
} from '../../utils/evolutionConfirm'

interface DetailRow {
  experiment_id?: string
  workspace_id?: string
  dataset_version?: string
  best_score?: { passed: number; total: number } | null
  runs_summary?: Record<string, unknown>[]
  [k: string]: unknown
}

export default defineComponent({
  name: 'EvolutionConsole',
  data() {
    return {
      status: null as api.ConsoleStatus | null,
      rootId: '',
      experiments: [] as Record<string, any>[],
      total: 0,
      page: 1,
      pageSize: 20,
      loaded: false,
      fetchError: '',
      detailOpen: false,
      detail: null as DetailRow | null,
      selected: null as Record<string, any> | null,
      skills: null as Record<string, any> | null,
      gateItems: [] as Record<string, any>[],
      evalItems: [] as Record<string, any>[],
      patternItems: [] as Record<string, any>[],
      detailEmpty: false,
      trajOpen: false,
      trajItems: [] as Record<string, any>[],
      versionsCatalog: [] as Record<string, any>[],
      createOpen: false,
      createBusy: false,
      createError: '',
      metaDatasets: [] as Record<string, any>[],
      createForm: {
        dataset_version: '',
        model_mode: 'simulated',
        experience: 'full',
        max_iterations: 3,
        max_model_calls: 90,
        max_tool_calls: 40,
        max_seconds: 3600,
        review: null as string | null,
      },
      realStartEnabled: false,
      promoWs: '',
      promoBusy: false,
      promoError: '',
      promoPreview: null as Record<string, any> | null,
      promoBinding: null as Record<string, any> | null,
      promoBindingError: '',
      promoKey: '',
      rollbackPending: null as PendingRollback | null,
      rollbackUnknown: false,
      rollbackError: '',
      contentOpen: false,
      contentData: null as Record<string, any> | null,
      contentError: '',
      diffOpen: false,
      diffBase: '',
      diffHead: '',
      diffData: null as Record<string, any> | null,
      diffError: '',
      diffLoading: false,
    }
  },
  computed: {
    memberSummary(): string {
      const d = this.detail as Record<string, any> | null
      const cur = d ? (d.current_skill_set as any) : null
      const members = cur && cur.members ? (cur.members as any[]) : []
      return members.length
        ? members.map((m) => String(m.version_id)).join(', ')
        : '空'
    },
    selDsMeta(): Record<string, any> | null {
      if (!this.createForm.dataset_version) return null
      const found = (this.metaDatasets || []).find(
        (d) => d.dataset_version === this.createForm.dataset_version
      )
      return found || null
    },
    dsMetaLines(): string[] {
      const m = this.selDsMeta
      if (!m) return []
      const lines: string[] = []
      if (m.grader_ready === false) lines.push('grader 未就绪，禁止创建')
      if (m.required_review) {
        lines.push(
          '要求 review=' + String(m.required_review) +
          '（提交自动携带；缺省服务端 422）'
        )
      }
      if (m.grader_calibration === 'engineering_only') {
        lines.push('校准=engineering_only（人工标签/真实校准未完成）')
      } else if (m.grader_calibration) {
        lines.push('校准=' + String(m.grader_calibration))
      }
      lines.push(
        m.allow_business_promotion === false
          ? '不允许业务晋升'
          : '允许业务晋升'
      )
      if (m.grader_purpose) lines.push(String(m.grader_purpose))
      return lines
    },
    versionOptions(): Record<string, any>[] {
      return (this.versionsCatalog || []).slice().sort(
        (a, b) =>
          String(a.skill_id).localeCompare(String(b.skill_id)) ||
          Number(a.seq) - Number(b.seq)
      )
    },
  },
  async mounted() {
    try {
      this.status = await api.consoleStatus()
      if (this.status.console === 'ok' && this.status.roots.length > 0) {
        this.rootId = this.status.roots[0].id
        await this.reloadExperiments()
      }
      this.loaded = true
    } catch (e: any) {
      this.fetchError = String(e?.response?.data?.detail ?? e?.message ?? e)
      this.loaded = true
    }
  },
  methods: {
    rowClass(): string {
      return 'clickable-row'
    },
    async reloadExperiments() {
      this.page = 1
      await this.fetchExperiments()
    },
    async fetchExperiments() {
      if (!this.rootId) return
      try {
        const res = await api.listExperiments(this.rootId, this.page, this.pageSize)
        this.experiments = res.items as Record<string, any>[]
        this.total = res.total
        this.fetchError = ''
      } catch (e: any) {
        this.fetchError = String(e?.response?.data?.detail ?? e?.message ?? e)
      }
    },
    async onPage(p: number) {
      this.page = p
      await this.fetchExperiments()
    },
    async openExperiment(row: Record<string, any>) {
      this.selected = row
      this.detailOpen = true
      this.detail = null
      this.skills = null
      this.gateItems = []
      this.evalItems = []
      this.patternItems = []
      this.versionsCatalog = []
      const expId = String(row.experiment_id)
      const root = this.rootId
      try {
        this.detail = (await api.experimentDetail(expId, root)) as DetailRow
        this.skills = await api.experimentSkills(expId, root)
        const catalog = await api.experimentVersions(expId, root)
        this.versionsCatalog = (catalog.items || []) as Record<string, any>[]
        this.gateItems = (await api.gateHistory(expId, root)).items
        this.evalItems = (await api.evaluations(expId, root)).items
        this.patternItems = (await api.experimentPatterns(expId, root)).items
        const runs = ((this.detail.runs_summary || []) as Record<string, any>[])
        this.detailEmpty = runs.length === 0
        this.fetchError = ''
      } catch (e: any) {
        this.fetchError = String(e?.response?.data?.detail ?? e?.message ?? e)
      }
    },
    async loadTrajectories(runId: string) {
      this.trajOpen = true
      this.trajItems = []
      try {
        this.trajItems = (await api.trajectories(runId, this.rootId)).items
      } catch (e: any) {
        this.fetchError = String(e?.response?.data?.detail ?? e?.message ?? e)
      }
    },
    mdLabel(f: Record<string, any> | undefined): string {
      if (!f) return ''
      const label = String(f.label || '')
      return f.truncated ? label + '（已截断，仅前部）' : label
    },
    async openCreate() {
      this.createOpen = true
      this.createError = ''
      if (this.metaDatasets.length === 0) {
        try {
          const meta = await adminApi.adminMeta()
          this.metaDatasets = (meta.datasets || []) as Record<string, any>[]
          this.realStartEnabled = !!meta.real_start_enabled
        } catch (e: any) {
          this.createError = String(
            e?.response?.data?.detail ?? e?.message ?? e
          )
        }
      }
    },
    genKey(): string {
      try {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
          return crypto.randomUUID()
        }
      } catch {
        /* ignore */
      }
      return 'k-' + Date.now() + '-' + Math.random().toString(36).slice(2)
    },
    shortHash(h?: unknown): string {
      if (!h) return ''
      const s = String(h)
      return s.length > 16 ? s.slice(0, 8) + '…' + s.slice(-8) : s
    },
    memberList(members?: unknown): string {
      if (!Array.isArray(members)) return ''
      return members
        .map((m) => String((m as any).version_id || ''))
        .join(', ')
    },
    async refreshPromoBinding() {
      this.promoBinding = null
      this.promoBindingError = ''
      if (!this.promoWs) return
      try {
        const st = await adminApi.businessState(this.promoWs)
        this.promoBinding = {
          no_binding: !!st.no_binding,
          expected_rev: st.expected_rev ?? null,
          expected_set_hash: st.expected_set_hash ?? null,
          current: st.current || null,
        }
      } catch (e: any) {
        this.promoBindingError = String(
          e?.response?.data?.detail ?? e?.message ?? e
        )
      }
    },
    async runAction(row: Record<string, any>, action: string) {
      this.fetchError = ''
      const runId = String(row.run_id)
      try {
        if (action === 'start' || action === 'resume') {
          if (row.model_mode === 'real') {
            let preview: Record<string, any>
            try {
              preview = await adminApi.startPreview(runId, this.rootId)
            } catch (e: any) {
              this.fetchError = '配置预览失败（未触网络）：' + String(
                e?.response?.data?.detail ?? e?.message ?? e
              )
              return
            }
            const roles = (preview.roles || [] as any[]).map((x: any) =>
              `${x.role}=${x.model} @${x.endpoint_host}（provider=${x.provider_id ?? '—'}，max_tokens=${x.max_output_tokens ?? '—'}，retries=${x.retries}）`
            ).join('\n')
            const msg =
              `确认${action === 'start' ? '启动' : '恢复'}该真实模式运行？\n\n` +
              `数据范围：${preview.dataset_version}（迭代 ${preview.max_iterations}；预算 ${preview.budget?.max_model_calls ?? '—'} 模型调用 / ${preview.budget?.max_tool_calls ?? '—'} 工具 / ${preview.budget?.max_seconds ?? '—'}s）\n` +
              `角色配置：\n${roles || '（无）'}\n` +
              `评审：${preview.reviewer ? preview.reviewer.model_id + ' @' + preview.reviewer.endpoint_host + '（provider=' + (preview.reviewer.provider_id ?? '—') + '，credential_bound=' + (preview.reviewer.credential_env_bound === false ? '否' : '是') + '，max_tokens=' + preview.reviewer.max_output_tokens + '）' : '（无）'}\n` +
              `配置指纹：${preview.config_fingerprint || '（未就绪）'}\n` +
              `评审指纹：${preview.reviewer_fingerprint || '（无）'}\n\n` +
              '密钥不展示（仅显示受控 provider 引用与可用性布尔）、不发探测请求；' +
              '确认后配置漂移将被拒绝（不静默冻结新配置）。'
            try {
              await ElMessageBox.confirm(msg, '真实模式 ' + action + ' 确认', {
                type: 'warning',
                confirmButtonText: '确认并' + action,
                cancelButtonText: '取消',
              })
            } catch {
              return
            }
            if (!preview.config_fingerprint) {
              this.fetchError = '配置指纹未就绪（real 配置不可用）——不启动'
              return
            }
            let confirmBody: Record<string, unknown>
            try {
              confirmBody = { ...buildRealStartConfirm(preview as StartPreview) }
            } catch (e: any) {
              this.fetchError =
                'real 启动确认构造失败（拒绝提交，不补齐默认值）：' +
                String(e?.message ?? e)
              return
            }
            await adminApi.controlRun(runId, action as any, this.rootId,
                                      confirmBody)
            ElMessage.success('已确认并提交 ' + action)
          } else {
            await adminApi.controlRun(runId, action as any, this.rootId)
          }
        } else {
          await adminApi.controlRun(runId, action as any, this.rootId)
        }
        if (this.selected) {
          await this.openExperiment(this.selected)
        }
      } catch (e: any) {
        this.fetchError = String(
          e?.response?.data?.detail ?? e?.message ?? e
        )
      }
    },
    onDatasetChange() {
      const ds = (this.metaDatasets || []).find(
        (d) => d.dataset_version === this.createForm.dataset_version
      )
      this.createForm.review =
        ds && ds.required_review ? ds.required_review : null
    },
    async submitCreate() {
      this.createBusy = true
      this.createError = ''
      try {
        await adminApi.createExperiment({
          root: this.rootId,
          dataset_version: this.createForm.dataset_version,
          model_mode: this.createForm.model_mode,
          experience: this.createForm.experience,
          max_iterations: this.createForm.max_iterations,
          max_model_calls: this.createForm.max_model_calls,
          max_tool_calls: this.createForm.max_tool_calls,
          max_seconds: this.createForm.max_seconds,
          review: this.createForm.review,
        })
        this.createOpen = false
        this.fetchError = ''
        await this.reloadExperiments()
      } catch (e: any) {
        this.createError = String(
          e?.response?.data?.detail ?? e?.message ?? e
        )
      } finally {
        this.createBusy = false
      }
    },
    async loadPromoPreview() {
      if (!this.promoWs || !this.selected) return
      this.promoBusy = true
      this.promoError = ''
      this.promoPreview = null
      try {
        await this.refreshPromoBinding()
        this.promoPreview = await adminApi.promotionPreview(
          String(this.selected.experiment_id),
          this.rootId,
          this.promoWs
        )
        this.promoKey = this.genKey() // 新预览 → 新操作键
        if (!this.promoBinding) await this.refreshPromoBinding()
      } catch (e: any) {
        this.promoError = String(
          e?.response?.data?.detail ?? e?.message ?? e
        )
      } finally {
        this.promoBusy = false
      }
    },
    async doPromote() {
      if (!this.promoWs || !this.selected || !this.promoPreview ||
          !this.promoBinding) {
        return
      }
      const preview = this.promoPreview
      if (!this.promoKey) this.promoKey = this.genKey()
      const body: Record<string, unknown> = {
        root: this.rootId,
        experiment_id: String(this.selected.experiment_id),
        workspace_id: this.promoWs,
        expected_rev: this.promoBinding.expected_rev ?? null,
        expected_set_hash: this.promoBinding.expected_set_hash ?? null,
        target_members: Array.isArray(preview.members)
          ? (preview.members as unknown[])
          : [],
        idempotency_key: this.promoKey,
      }
      this.promoBusy = true
      this.promoError = ''
      try {
        const res = await adminApi.businessPromote(body)
        ElMessage.success(res && res.changed ? '晋升成功（集合已绑定）' : '已是最新集合')
        this.promoPreview = null
        this.promoKey = ''
        await this.refreshPromoBinding()
      } catch (e: any) {
        const status = e?.response?.status
        const detail = String(e?.response?.data?.detail ?? e?.message ?? e)
        if (status === 409 && detail.includes('重新预览')) {
          // 冲突：不自动取新令牌覆盖；强制重新预览（新键）
          this.promoPreview = null
          this.promoKey = ''
          this.promoError = '状态已变化：' + detail + '（请重新预览后再提交）'
        } else if (status === 409 || status === 422) {
          this.promoError =
            detail +
            '（被拒绝；如需变更请重新预览生成新键，不覆盖原请求）'
        } else {
          // 网络/超时类失败：保留预览与幂等键 → 原请求重试复用同键同内容
          this.promoError =
            detail + '（可重试：将复用同一幂等键与同一请求内容）'
        }
      } finally {
        this.promoBusy = false
      }
    },
    async doRollback() {
      if (!this.promoWs) return
      this.rollbackError = ''
      // 同键重试模式：pending 已存在且上次结果未知 → 逐字复用原请求，不刷新绑定。
      if (this.rollbackPending && this.rollbackUnknown) {
        await this.retryRollback()
        return
      }
      await this.refreshPromoBinding()
      if (!this.promoBinding) {
        this.rollbackError = '无法读取业务库当前绑定状态'
        return
      }
      try {
        await ElMessageBox.confirm(
          '回退目标 = 审计历史中上一明确生效集合（服务端按审计链固定，不在页面重选）。\n' +
            '注意：技能回退只影响后续编译请求，不恢复已经发布的 Wiki 内容。',
          '确认回退业务技能绑定',
          { type: 'warning', confirmButtonText: '确认回退',
            cancelButtonText: '取消' }
        )
      } catch {
        return
      }
      const pending = createPendingRollback(
        this.promoWs,
        this.promoBinding.expected_rev ?? null,
        this.promoBinding.expected_set_hash ?? null
      )
      this.rollbackPending = pending
      await this._sendRollback(pending)
    },
    async retryRollback() {
      if (!this.rollbackPending) return
      await this._sendRollback(this.rollbackPending, true)
    },
    async _sendRollback(pending: PendingRollback, retrying = false) {
      this.promoBusy = true
      this.rollbackError = ''
      try {
        const res = await adminApi.businessRollback(pending.workspace_id, {
          expected_rev: pending.expected_rev ?? null,
          expected_set_hash: pending.expected_set_hash ?? null,
          idempotency_key: pending.idempotency_key,
        })
        // 明确 2xx：清空 pending 后再刷新绑定
        this.rollbackPending = null
        this.rollbackUnknown = false
        this.rollbackError = ''
        const target =
          Array.isArray(res.members) && res.members.length
            ? res.members
                .map((m: any) => String(m.version_id))
                .join(', ')
            : '空集合（恢复初始无绑定）'
        ElMessage.success(
          `${retrying ? '重试成功（返回首次结果）' : '回退成功'} → ${target}（rev=${res.rev}）`
        )
        this.promoPreview = null
        this.promoKey = ''
        await this.refreshPromoBinding()
      } catch (e: any) {
        const kind = classifyRollbackError(e)
        const detail = String(
          e?.response?.data?.detail ?? e?.message ?? e
        )
        if (kind === 'indeterminate') {
          // 网络断开/timeout/5xx：保留 pending，可同键重试原请求
          this.rollbackUnknown = true
          this.rollbackError =
            '上次回退结果未知，可重试原请求（将复用同一预期修订与同一幂等键）。原因：' +
            detail
        } else {
          // 409/422/其它确定性错误：作废 pending，必须重新预览/重新发起
          this.rollbackPending = null
          this.rollbackUnknown = false
          this.rollbackError =
            (kind === 'conflict'
              ? '状态已变化（请重新预览后再发起回退）：'
              : kind === 'idem_conflict'
                ? '请求内容冲突（原请求作废，请重新发起）：'
                : '回退失败：') + detail
        }
      } finally {
        this.promoBusy = false
      }
    },
    async viewContent(row: Record<string, any>) {
      this.contentOpen = true
      this.contentData = null
      this.contentError = ''
      try {
        this.contentData = await api.skillContent(
          String(row.version_id),
          this.rootId
        )
      } catch (e: any) {
        this.contentError = String(
          e?.response?.data?.detail ?? e?.message ?? e
        )
      }
    },
    prepareDiff(row: Record<string, any>) {
      this.diffOpen = true
      this.diffError = ''
      this.diffData = null
      this.diffHead = String(row.version_id)
      // 同 skill 前一 seq 作为默认基准（存在时）。
      const same = (this.versionsCatalog || []).filter(
        (v) => String(v.skill_id) === String(row.skill_id)
      )
      same.sort((a, b) => Number(a.seq) - Number(b.seq))
      const idx = same.findIndex((v) => v.version_id === row.version_id)
      this.diffBase = idx > 0 ? String(same[idx - 1].version_id) : ''
    },
    async compareDiff() {
      if (!this.diffBase || !this.diffHead) return
      this.diffLoading = true
      this.diffError = ''
      this.diffData = null
      try {
        this.diffData = await api.skillDiff(
          this.diffHead,
          this.diffBase,
          this.rootId
        )
      } catch (e: any) {
        this.diffError = String(e?.response?.data?.detail ?? e?.message ?? e)
      } finally {
        this.diffLoading = false
      }
    },
  },
})
</script>

<style scoped>
.meta-banner {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}
.muted {
  color: #909399;
  font-size: 12px;
}
.clickable-row {
  cursor: pointer;
}
.promo-box {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 8px 12px 12px;
  margin: 10px 0;
}
h2 {
  margin-bottom: 12px;
}
h3 {
  margin-top: 18px;
}
.raw-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: Consolas, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  background: #f7f8fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  padding: 10px;
  max-height: 60vh;
  overflow: auto;
}
</style>
