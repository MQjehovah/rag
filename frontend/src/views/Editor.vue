<template>
  <div class="app-container" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <div class="app-body">
      <!-- 侧边栏 -->
      <aside class="sidebar" :style="{ width: sidebarCollapsed ? '0px' : sidebarWidth + 'px' }">
        <div class="side-search">
          <el-input v-model="searchQuery" placeholder="搜索笔记..." clearable @keyup.enter="doSearch" />
        </div>

        <template v-if="favPages.length">
          <div class="side-section">
            <span class="side-section-label">收藏</span>
          </div>
          <div class="fav-list">
            <div v-for="f in favPages" :key="f.id" class="page-item fav" @click="openPageById(f.id)">
              <span class="fav-star">★</span>
              <span class="page-title">{{ f.title }}</span>
            </div>
          </div>
        </template>

        <template v-if="recentPages.length">
          <div class="side-section">
            <span class="side-section-label">最近访问</span>
          </div>
          <div class="fav-list">
            <div v-for="r in recentPages" :key="r.id" class="page-item" @click="openPageById(r.id)">
              <span class="page-ico">{{ r.icon || '📄' }}</span>
              <span class="page-title">{{ r.title }}</span>
            </div>
          </div>
        </template>

        <template v-if="myTemplates.length">
          <div class="side-section">
            <span class="side-section-label">我的模板</span>
          </div>
          <div class="tpl-list">
            <div v-for="t in myTemplates" :key="t.id" class="page-item tpl" @click="newFromTemplate(t)">
              <span class="tpl-icon">📄</span>
              <span class="page-title">{{ t.name }}</span>
              <button
                class="icon-btn"
                :title="t.visibility === 'public' ? '取消共享（他人不可见）' : '共享给所有用户（只读）'"
                @click.stop="toggleTemplateVisibility(t)"
              >{{ t.visibility === 'public' ? '🌐' : '🔒' }}</button>
              <button class="icon-btn" title="删除模板" @click.stop="deleteTemplate(t.id)">✕</button>
            </div>
          </div>
        </template>

        <template v-if="sharedTemplates.length">
          <div class="side-section">
            <span class="side-section-label">共享模板</span>
          </div>
          <div class="tpl-list">
            <div
              v-for="t in sharedTemplates"
              :key="t.id"
              class="page-item tpl"
              :title="`来自 ${t.owner || '他人'} 的共享模板（从模板新建）`"
              @click="newFromTemplate(t)"
            >
              <span class="tpl-icon">🌐</span>
              <span class="page-title">{{ t.name }}</span>
              <span class="tpl-owner">{{ t.owner || '共享' }}</span>
            </div>
          </div>
        </template>

        <div class="side-section">
          <span class="side-section-label">笔记本</span>
          <button class="icon-btn" title="新建笔记本" @click="showNewNotebook = true">＋</button>
        </div>

        <div class="tag-filter">
          <el-select
            v-model="activeTag"
            placeholder="按标签过滤..."
            clearable
            filterable
            size="small"
            style="width: 100%"
          >
            <el-option
              v-for="t in tagOptions"
              :key="t.tag"
              :label="`${t.tag} (${t.count})`"
              :value="t.tag"
            />
          </el-select>
        </div>

        <div class="notebook-list">
          <div v-if="activeTag" class="tag-pages">
            <div class="tag-pages-header">标签：{{ activeTag }}（{{ tagPages.length }}）</div>
            <div
              v-for="page in tagPages"
              :key="page.id"
              class="page-item"
              :class="{ active: currentPage?.id === page.id }"
              @click="selectPage(page)"
            >
              <div class="page-info">
                <span class="page-ico">{{ page.icon || '📄' }}</span>
                <span class="page-title">{{ page.title || '无标题' }}</span>
              </div>
            </div>
            <el-empty v-if="tagPages.length === 0" description="该标签下暂无笔记" :image-size="40" />
          </div>
          <template v-for="grp in notebookGroups" :key="grp.section">
            <div v-if="grp.section" class="side-section nb-group">{{ grp.section }}</div>
            <div
              v-for="nb in grp.items"
              :key="nb.id"
              class="notebook-item"
              :class="{
                active: currentNotebook?.id === nb.id,
                dragging: nbDragId === nb.id,
                'nb-drop-before': nbDrop?.id === nb.id && nbDrop.zone === 'before',
                'nb-drop-after': nbDrop?.id === nb.id && nbDrop.zone === 'after',
              }"
            >
            <div
              class="notebook-info"
              draggable="true"
              @click="selectNotebook(nb)"
              @dragstart="onNbDragStart(nb, $event)"
              @dragover="onNbDragOver(nb, $event)"
              @drop.stop="onNbDrop(nb, $event)"
              @dragend="onNbDragEnd"
            >
              <span class="nb-chevron" :class="{ open: currentNotebook?.id === nb.id }">›</span>
              <span class="notebook-icon">{{ nb.icon || '📁' }}</span>
              <span class="notebook-name">{{ nb.name }}</span>
              <el-dropdown trigger="click" @command="(cmd: string) => handleNotebookCmd(cmd, nb)">
                <el-button size="small" text>⋮</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="newPage">新建笔记</el-dropdown-item>
                    <el-dropdown-item command="exportZip">导出本笔记本 (zip)</el-dropdown-item>
                    <el-dropdown-item command="settings">设置</el-dropdown-item>
                    <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>

            <div v-if="currentNotebook?.id === nb.id" class="page-list">
              <div
                v-for="row in treeRowsVisible"
                :key="row.page.id"
                class="page-item"
                :class="{
                  active: currentPage?.id === row.page.id,
                  dragging: dragPageId === row.page.id,
                  'drop-before': dropTarget?.id === row.page.id && dropTarget.zone === 'before',
                  'drop-after': dropTarget?.id === row.page.id && dropTarget.zone === 'after',
                  'drop-inside': dropTarget?.id === row.page.id && dropTarget.zone === 'inside',
                }"
                :style="{ paddingLeft: (8 + row.depth * 16) + 'px' }"
                draggable="true"
                @dragstart="onPageDragStart(row.page, $event)"
                @dragover="onPageDragOver(row.page, $event)"
                @drop.stop="onPageDrop(row.page, $event)"
                @dragend="onPageDragEnd"
              >
                <div class="page-info" @click="selectPage(row.page)">
                  <span
                    v-if="row.hasChildren"
                    class="page-chevron"
                    :class="{ open: !row.collapsed }"
                    @click.stop="toggleCollapse(row.page.id)"
                  >›</span>
                  <span v-else class="page-chevron placeholder"></span>
                  <span class="page-ico">{{ row.page.icon || '📄' }}</span>
                  <span class="page-title">{{ row.page.title || '无标题' }}</span>
                </div>
                <el-dropdown trigger="click" @command="(cmd: string) => handlePageCmd(cmd, row.page)">
                  <el-button size="small" text class="page-menu-btn">⋮</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="child">新建子页面</el-dropdown-item>
                      <el-dropdown-item command="index">重新索引</el-dropdown-item>
                      <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
              <div v-if="treeRowsMore > 0" class="load-more" @click="renderLimit += TREE_RENDER_BATCH">
                加载更多（还有 {{ treeRowsMore }} 条）
              </div>
            </div>
            </div>
          </template>

          <div v-if="notebooks.length === 0" class="empty-tip">
            暂无笔记本
          </div>
        </div>
        <div class="side-foot">
          <button class="side-foot-btn" @click="toggleTrash">
            <span>🗑 回收站</span>
            <span v-if="trashPages.length" class="count">{{ trashPages.length }}</span>
          </button>
        </div>
        <div v-if="trashOpen" class="trash-list">
          <div v-if="!trashPages.length" class="muted-hint" style="padding: 6px 12px">回收站为空</div>
          <div v-for="t in trashPages" :key="t.id" class="trash-item">
            <span class="page-title">{{ t.title }}</span>
            <button class="icon-btn" title="恢复" @click="restoreTrash(t.id)">↩</button>
            <button class="icon-btn" title="彻底删除" @click="purgeTrash(t.id, t.title)">✕</button>
          </div>
        </div>
        <div v-if="!sidebarCollapsed" class="sidebar-resizer" @mousedown.prevent="startSidebarResize"></div>
      </aside>

      <!-- 主编辑区 -->
      <main class="main-content">
        <div class="doc-topbar">
          <button v-if="sidebarCollapsed" class="icon-btn expand" title="展开侧边栏" @click="sidebarCollapsed = false">»</button>
          <div v-if="currentPage" class="doc-tab" :title="currentPage.title">
            <span class="doc-tab-icon">{{ currentPage.icon || '📄' }}</span>
            <span class="doc-tab-title">{{ currentPage.title || '无标题' }}</span>
          </div>
          <div v-else class="doc-tab doc-tab-empty">{{ currentNotebook?.name || '未选择笔记本' }}</div>
          <div class="topbar-actions">
            <span
              v-if="collabActive"
              class="collab-peers"
              :class="{ 'is-offline': !collabConnected }"
              :title="collabTitle"
            >
              <span
                v-for="p in collabUsers"
                :key="p.name + p.color"
                class="collab-avatar"
                :style="{ background: p.color }"
              >{{ (p.name || '?').slice(0, 1) }}</span>
            </span>
            <span
              class="save-state"
              :class="[saveStatus, { clickable: saveStatus === 'error' }]"
              :title="saveStatus === 'error' ? '点击重试保存' : saveText"
              @click="saveStatus === 'error' && retrySave()"
            >
              <span class="save-dot" :class="saveStatus"></span>
              <span class="save-text">{{ saveText }}</span>
            </span>
            <button v-if="currentPage" class="topbar-text-btn" title="分享" @click="openShare">分享</button>
            <button v-if="currentPage" class="icon-btn" :title="isFav ? '取消收藏' : '收藏'" @click="toggleFav">{{ isFav ? '★' : '☆' }}</button>
            <button class="icon-btn" :class="{ 'is-on': commentsOpen }" title="评论" @click="toggleComments">💬<span v-if="comments.length" class="badge">{{ comments.length }}</span></button>
            <button class="icon-btn" :class="{ 'is-on': outlineOpen }" title="大纲" @click="outlineOpen = !outlineOpen; if (outlineOpen) commentsOpen = false">☰</button>
            <el-dropdown v-if="currentPage" trigger="click" @command="onPageMenu">
              <button class="icon-btn" title="页面设置">⋯</button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="import">导入知识</el-dropdown-item>
                  <el-dropdown-item command="organize">{{ organizing ? '整理中…' : '自动整理' }}</el-dropdown-item>
                  <el-dropdown-item command="fav" divided>{{ isFav ? '★ 取消收藏' : '☆ 收藏' }}</el-dropdown-item>
                  <el-dropdown-item command="link">复制链接</el-dropdown-item>
                  <el-dropdown-item command="dup">创建副本</el-dropdown-item>
                  <el-dropdown-item command="export">导出 Markdown</el-dropdown-item>
                  <el-dropdown-item command="export-html">导出 HTML</el-dropdown-item>
                  <el-dropdown-item command="export-docx">导出 Word (.docx)</el-dropdown-item>
                  <el-dropdown-item command="export-pdf">导出 PDF（含大纲）</el-dropdown-item>
                  <el-dropdown-item command="template">另存为模板</el-dropdown-item>
                  <el-dropdown-item command="view-doc" divided>视图：文档</el-dropdown-item>
                  <el-dropdown-item command="view-table">视图：表格</el-dropdown-item>
                  <el-dropdown-item command="view-board">视图：看板</el-dropdown-item>
                  <el-dropdown-item command="view-calendar">视图：日历</el-dropdown-item>
                  <el-dropdown-item command="history" divided>版本历史</el-dropdown-item>
                  <el-dropdown-item command="share">分享只读链接</el-dropdown-item>
                  <el-dropdown-item command="wide" divided>{{ pageWide ? '关闭全宽' : '全宽显示' }}</el-dropdown-item>
                  <el-dropdown-item command="small">{{ pageSmall ? '取消小字号' : '小字号' }}</el-dropdown-item>
                  <el-dropdown-item command="delete" divided>移到回收站</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button size="small" type="primary" @click="showNewNotebook = true">新建</el-button>
          </div>
        </div>
        <div class="doc-scroll">
        <div v-if="currentPage && isDatabase" class="db-view">
          <div class="db-hero">
            <span class="db-hero-icon">{{ currentPage.icon || '🗂' }}</span>
            <h1 class="db-hero-title">{{ currentPage.title || '无标题' }}</h1>
          </div>
          <div class="db-toolbar">
            <div class="db-tabs">
              <button class="db-tab" :class="{ active: currentPage.view_type === 'table' }" @click="setViewType('table')">▦ 表格</button>
              <button class="db-tab" :class="{ active: currentPage.view_type === 'board' }" @click="setViewType('board')">▤ 看板</button>
              <button class="db-tab" :class="{ active: currentPage.view_type === 'calendar' }" @click="setViewType('calendar')">🗓 日历</button>
              <button class="db-tab" :class="{ active: currentPage.view_type === 'doc' }" @click="setViewType('doc')">📄 文档</button>
            </div>
            <div class="db-toolbar-right">
              <el-select v-model="dbFilterStatus" size="small" clearable placeholder="全部状态" style="width: 118px">
                <el-option v-for="s in STATUS_OPTIONS.filter(x => x)" :key="s" :label="s" :value="s" />
              </el-select>
              <el-select v-model="dbSort" size="small" style="width: 118px">
                <el-option label="按更新时间" value="updated" />
                <el-option label="按标题" value="title" />
              </el-select>
              <el-button size="small" type="primary" @click="addRow">＋ 新建</el-button>
            </div>
          </div>

          <div v-if="currentPage.view_type === 'table'" class="db-table">
            <div class="db-row db-row-head">
              <span class="db-c-title">标题</span>
              <span class="db-c-status">状态</span>
              <span class="db-c-time">更新时间</span>
            </div>
            <div v-for="c in dbChildrenFiltered" :key="c.id" class="db-row">
              <span class="db-c-title"><a class="db-link" @click="openPageById(c.id)">{{ c.title || '无标题' }}</a></span>
              <span class="db-c-status">
                <el-select :model-value="c.status || ''" size="small" style="width: 112px" @change="(v: string) => setChildStatus(c.id, v)">
                  <el-option v-for="s in STATUS_OPTIONS" :key="s" :label="s || '未设置'" :value="s" />
                </el-select>
              </span>
              <span class="db-c-time">{{ formatTime(c.updated_at) }}</span>
            </div>
            <div v-if="!dbChildrenFiltered.length" class="muted-hint" style="padding: 16px">暂无数据，点击右上角「新建」添加</div>
          </div>

          <div v-else-if="currentPage.view_type === 'board'" class="db-board">
            <div
              v-for="col in BOARD_COLUMNS"
              :key="col"
              class="db-col"
              :style="{ background: STATUS_META[col].bg }"
              @dragover.prevent
              @drop="onBoardDrop(col, $event)"
            >
              <div class="db-col-head">
                <span class="db-col-dot" :style="{ background: STATUS_META[col].dot }"></span>
                <span class="db-col-name">{{ col || '未设置' }}</span>
                <span class="db-col-count">{{ dbChildrenFiltered.filter(c => (c.status || '') === col).length }}</span>
              </div>
              <div class="db-col-body">
                <div
                  v-for="c in dbChildrenFiltered.filter(x => (x.status || '') === col)"
                  :key="c.id"
                  class="db-card"
                  draggable="true"
                  @dragstart="boardDragId = c.id"
                  @dragend="boardDragId = null"
                  @click="openPageById(c.id)"
                >{{ c.title || '无标题' }}</div>
                <div class="db-col-add" @click="addRow">＋ 新页面</div>
              </div>
            </div>
          </div>

          <div v-else-if="currentPage.view_type === 'calendar'" class="db-cal">
            <div class="db-cal-head">{{ calendar.label }}</div>
            <div class="db-cal-grid">
              <div v-for="d in ['日', '一', '二', '三', '四', '五', '六']" :key="d" class="db-cal-dow">{{ d }}</div>
              <div v-for="i in calendar.pad" :key="'p' + i" class="db-cal-cell empty"></div>
              <div v-for="cell in calendar.cells" :key="cell.day" class="db-cal-cell">
                <div class="db-cal-day">{{ cell.day }}</div>
                <div v-for="c in cell.items" :key="c.id" class="db-cal-item" @click="openPageById(c.id)">{{ c.title }}</div>
              </div>
            </div>
          </div>
        </div>
        <div v-else-if="currentPage" class="editor-wrapper" :class="{ wide: pageWide, small: pageSmall }">
          <div
            v-if="currentPage.cover"
            class="page-cover"
            :class="{ 'is-gradient': coverIsGradient }"
            :style="coverIsGradient ? { background: coverGradientStyle } : {}"
          >
            <img
              v-if="!coverIsGradient"
              :src="currentPage.cover"
              alt="封面"
              class="cover-img"
              :style="{ objectPosition: 'center ' + (currentPage.cover_offset ?? 50) + '%' }"
              @mousedown="startCoverDrag"
            />
            <div class="cover-actions">
              <el-button size="small" @click="coverPickerVisible = !coverPickerVisible">更换封面</el-button>
              <el-button size="small" @click="removeCover">移除</el-button>
            </div>
          </div>
          <div class="title-row">
            <button
              class="page-icon-btn"
              :title="currentPage.icon ? '更换图标' : '添加图标'"
              @click="iconPickerVisible = !iconPickerVisible; coverPickerVisible = false"
            >
              <span v-if="currentPage.icon" class="page-icon">{{ currentPage.icon }}</span>
              <span v-else class="page-icon-add">＋</span>
            </button>
            <input
              v-model="currentPage.title"
              class="title-input"
              placeholder="无标题"
              @input="scheduleSave"
            />
            <el-button v-if="!currentPage.cover" link size="small" class="cover-add" @click="coverPickerVisible = !coverPickerVisible">＋ 封面</el-button>
          </div>

          <div v-if="coverPickerVisible" class="cover-picker">
            <button class="cover-upload" @click="pickCoverFromPicker">上传图片</button>
            <button
              v-for="g in GRADIENTS"
              :key="g.key"
              class="cover-swatch"
              :style="{ background: g.css }"
              :title="g.key"
              @click="setGradientCover(g.key)"
            ></button>
          </div>

          <div v-if="iconPickerVisible" class="icon-picker">
            <div v-for="grp in EMOJI_GROUPS" :key="grp.label" class="emoji-group">
              <div class="emoji-group-label">{{ grp.label }}</div>
              <div class="emoji-grid">
                <button
                  v-for="emo in grp.items"
                  :key="emo"
                  class="emoji-opt"
                  :class="{ active: currentPage.icon === emo }"
                  @click="setIcon(emo)"
                >{{ emo }}</button>
              </div>
            </div>
            <div class="emoji-foot">
              <button class="emoji-foot-btn" @click="randomIcon">🎲 随机</button>
              <button class="emoji-foot-btn" @click="setIcon('')">移除图标</button>
            </div>
          </div>
          <div v-loading="pageLoading" class="editor-body">
            <TipTapEditor
              ref="editorRef"
              :key="(collab ? 'c:' : 's:') + (currentPage?.id || '')"
              v-model="currentPage.content"
              :collab="collab"
              :page-updated-at="currentPage?.updated_at"
              @update:modelValue="scheduleSave"
              @collab-users="collabUsers = $event"
              @collab-status="collabConnected = $event"
              @collab-unavailable="handleCollabUnavailable"
              @wiki-link="onWikiLink"
              @citation-open="onCitationOpen"
              @global-search-locate="onGlobalSearchLocate"
            />
          </div>
          <div class="editor-footer">
            <span class="editor-hint">自动保存</span>
            <el-button size="small" @click="reindexCurrentPage" :loading="indexing">重新索引</el-button>
          </div>
        </div>
        <div v-else class="empty-state">
          <div class="empty-emoji">📝</div>
          <h2>开始记录你的知识</h2>
          <p>从左侧选择一个笔记本，或新建一个</p>
        </div>
        </div>
      </main>

      <!-- 大纲 -->
      <aside v-if="outlineOpen" class="outline-panel">
        <div class="outline-head">
          <span>大纲</span>
          <button class="icon-btn" title="关闭" @click="outlineOpen = false">✕</button>
        </div>
        <div class="outline-body">
          <div v-if="!outlineItems.length" class="muted-hint" style="padding: 8px 12px">暂无标题</div>
          <div
            v-for="(h, i) in outlineItems"
            :key="i"
            class="outline-item"
            :style="{ paddingLeft: (10 + (h.level - 1) * 12) + 'px' }"
            @click="scrollToHeading(h.text)"
          >{{ h.text }}</div>
        </div>
        <div class="link-panel-head">
          <span>反向链接</span>
          <span v-if="backlinks.length" class="count">{{ backlinks.length }}</span>
        </div>
        <div class="link-list">
          <div v-if="!backlinks.length" class="muted-hint" style="padding: 4px 12px">暂无</div>
          <div v-for="b in backlinks" :key="b.id" class="outline-item" @click="openPageById(b.id)">{{ b.title }}</div>
        </div>
      </aside>

      <!-- 评论 -->
      <aside v-if="commentsOpen" class="comments-panel">
        <div class="outline-head">
          <span>评论</span>
          <button class="icon-btn" title="关闭" @click="commentsOpen = false">✕</button>
        </div>
        <div class="comments-body">
          <div v-if="!comments.length" class="muted-hint" style="padding: 8px 12px">暂无评论</div>
          <div v-for="c in comments" :key="c.id" class="comment-item" :class="{ resolved: c.resolved }">
            <div class="comment-head">
              <span class="comment-author">{{ c.author }}</span>
              <span class="comment-time">{{ commentTime(c.created_at) }}</span>
              <button class="icon-btn" :title="c.resolved ? '重新打开' : '标记已解决'" @click="toggleResolveComment(c.id)">{{ c.resolved ? '↺' : '✓' }}</button>
              <button class="icon-btn" title="删除" @click="deleteComment(c.id)">✕</button>
            </div>
            <div class="comment-content">{{ c.content }}</div>
          </div>
        </div>
        <div class="comment-input">
          <el-input v-model="commentText" type="textarea" :rows="2" placeholder="写下评论…（Ctrl+Enter 发送）" @keydown.ctrl.enter="addComment" />
          <div style="margin-top: 6px; text-align: right">
            <el-button size="small" type="primary" :disabled="!commentText.trim()" @click="addComment">评论</el-button>
          </div>
        </div>
      </aside>
    </div>

    <!-- 新建笔记本对话框 -->
    <el-dialog v-model="showNewNotebook" title="新建笔记本" width="400px">
      <el-input v-model="newNotebookName" placeholder="笔记本名称" @keyup.enter="handleCreateNotebook" />
      <div style="margin-top: 12px">
        <div class="muted-hint" style="margin-bottom: 4px">可见性</div>
        <el-select v-model="newNotebookVisibility" style="width: 100%">
          <el-option label="仅本人可见" value="self" />
          <el-option label="部门可见" value="dept" />
          <el-option label="公开（所有登录用户）" value="public" />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="showNewNotebook = false">取消</el-button>
        <el-button type="primary" @click="handleCreateNotebook">创建</el-button>
      </template>
    </el-dialog>

    <!-- 笔记本设置对话框 -->
    <el-dialog v-model="showNotebookSettings" title="笔记本设置" width="520px">
      <el-form label-width="90px">
        <el-form-item label="图标">
          <div class="nb-icon-picker">
            <span
              v-for="e in NOTEBOOK_ICONS"
              :key="e"
              class="nb-icon-opt"
              :class="{ active: notebookForm.icon === e }"
              @click="notebookForm.icon = e"
            >{{ e }}</span>
            <el-input v-model="notebookForm.icon" placeholder="或输入 emoji" style="width: 120px" maxlength="4" />
          </div>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="notebookForm.name" @keyup.enter="saveNotebookSettings" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="notebookForm.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
        <el-form-item label="可见性">
          <el-select v-model="notebookForm.visibility" style="width: 100%">
            <el-option label="仅本人可见" value="self" />
            <el-option label="部门可见" value="dept" />
            <el-option label="公开（所有登录用户）" value="public" />
          </el-select>
          <div class="muted-hint">仅本人：只有自己可访问；部门：同组用户可访问；公开：所有登录用户可访问。</div>
        </el-form-item>
        <template v-if="canManageNotebook">
          <el-form-item label="可访问用户">
            <el-select
              v-model="notebookForm.acl_users"
              multiple
              filterable
              placeholder="额外授权具体用户"
              style="width: 100%"
            >
              <el-option
                v-for="u in aclUserOptions"
                :key="u.id"
                :label="`${u.name || u.username}（${u.username}）`"
                :value="u.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="可访问部门">
            <el-select
              v-model="notebookForm.acl_groups"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="额外授权部门/组"
              style="width: 100%"
            >
              <el-option v-for="g in aclGroupOptions" :key="g" :label="g" :value="g" />
            </el-select>
            <div class="muted-hint">在「仅本人 / 部门 / 公开」之外，额外授权所选用户与部门可访问。</div>
          </el-form-item>
        </template>
        <el-form-item label="嵌入模型">
          <el-select v-model="notebookForm.embedding_profile_id" clearable placeholder="默认档案" style="width: 100%">
            <el-option
              v-for="p in profiles"
              :key="p.id"
              :label="p.is_default ? `${p.name}（默认）` : p.name"
              :value="p.id"
            />
          </el-select>
          <div class="muted-hint">指定该笔记本使用的嵌入模型；更换后需对该笔记本的笔记重新索引。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showNotebookSettings = false">取消</el-button>
        <el-button type="primary" :loading="notebookSaving" @click="saveNotebookSettings">保存</el-button>
      </template>
    </el-dialog>

    <!-- 搜索结果对话框 -->
    <el-dialog v-model="showSearch" title="搜索结果" width="700px">
      <div v-if="searchResults.length > 0">
        <div v-for="result in searchResults" :key="result.id" class="search-result" @click="openFromSearch(result)">
          <div class="result-header">
            <span class="result-title">{{ result.title }}</span>
            <el-tag v-if="result.source" size="small" :type="getSourceTagType(result.source)">{{ result.source }}</el-tag>
          </div>
          <div v-if="result.content" class="result-content">{{ result.content }}</div>
          <div v-if="result.chunks && result.chunks.length" class="result-chunks">
            <div v-for="(c, ci) in result.chunks.slice(0, 2)" :key="ci" class="result-chunk">
              <span v-if="c.context" class="result-chunk-ctx">{{ c.context }}</span>
              {{ c.content }}
            </div>
          </div>
          <div v-if="typeof result.score === 'number'" class="result-footer">
            <el-tag size="small" type="info">得分: {{ result.score.toFixed(3) }}</el-tag>
          </div>
        </div>
      </div>
      <el-empty v-else description="未找到相关笔记" />
    </el-dialog>

    <!-- 导入知识对话框 -->
    <el-dialog v-model="importDialogVisible" title="导入知识" width="600px">
      <el-tabs v-model="importTab">
        <el-tab-pane label="文件上传" name="file">
          <el-upload
            :auto-upload="false"
            :limit="1"
            :on-change="handleFileChange"
            accept=".pdf,.txt,.md,.doc,.docx,.csv,.json"
          >
            <el-button>选择文件</el-button>
            <template #tip><div class="upload-tip">支持 PDF、Word、TXT、Markdown 等格式</div></template>
          </el-upload>
        </el-tab-pane>
        <el-tab-pane label="URL" name="url">
          <el-input v-model="importUrl" placeholder="输入网页 URL" />
        </el-tab-pane>
        <el-tab-pane label="文本" name="text">
          <el-input v-model="importText" type="textarea" :rows="6" placeholder="粘贴文本内容" />
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importLoading" @click="handleImport">分析并导入</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="confirmDialogVisible" title="确认导入" width="600px">
      <div v-if="!confirmForm.should_save" class="save-summary">LLM 判断该内容不值得保存（广告/无效内容等）</div>
      <template v-else>
        <el-form label-width="100px">
          <el-form-item label="操作">
            <el-tag v-if="confirmForm.action === 'create_notebook'" type="success">新建笔记本 + 笔记</el-tag>
            <el-tag v-else-if="confirmForm.action === 'update_note'" type="warning">更新已有笔记</el-tag>
            <el-tag v-else>新建笔记</el-tag>
          </el-form-item>
          <el-form-item label="标题">
            <el-input v-model="confirmForm.title" />
          </el-form-item>
          <el-form-item v-if="confirmForm.action === 'create_notebook'" label="新建笔记本">
            <el-input v-model="confirmForm.new_notebook_name" placeholder="新笔记本名称" />
          </el-form-item>
          <el-form-item v-else-if="confirmForm.action === 'update_note'" label="更新笔记">
            <el-select v-model="confirmForm.update_page_id" placeholder="选择要更新的笔记" style="width: 100%">
              <el-option v-for="p in confirmForm.pages" :key="p.id" :label="p.title" :value="p.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-else label="笔记本">
            <el-select v-model="confirmForm.notebook_id" clearable placeholder="选择笔记本" style="width: 100%">
              <el-option v-for="nb in confirmForm.notebooks" :key="nb.id" :label="nb.name" :value="nb.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="confirmForm.summary" label="摘要">
            <div class="save-summary">{{ confirmForm.summary }}</div>
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="confirmDialogVisible = false">取消</el-button>
        <el-button v-if="confirmForm.should_save" type="primary" :loading="confirmLoading" @click="confirmImport">确认保存</el-button>
      </template>
    </el-dialog>

    <!-- 分享 -->
    <el-dialog v-model="shareOpen" title="分享" width="520px">
      <template v-if="shareUrl">
        <p class="muted-hint" style="margin-bottom: 10px">任何获得链接的人都可以只读查看此页面（无需登录）。</p>
        <div class="share-row">
          <el-input v-model="shareUrl" readonly />
          <el-button type="primary" @click="copyShareUrl">复制</el-button>
        </div>
        <div style="margin-top: 12px">
          <el-button link type="danger" @click="cancelShare">取消分享</el-button>
        </div>
      </template>
      <template v-else>
        <p class="muted-hint" style="margin-bottom: 12px">发布后将生成只读链接，任何获得链接的人无需登录即可查看。</p>
        <el-button type="primary" @click="createShare">创建分享链接</el-button>
      </template>
    </el-dialog>

    <!-- 版本历史 -->
    <el-dialog v-model="historyOpen" title="版本历史" width="780px">
      <div class="history-wrap">
        <div class="history-list">
          <div v-if="!revisions.length" class="muted-hint" style="padding: 10px">暂无历史版本</div>
          <div
            v-for="r in revisions"
            :key="r.id"
            class="history-item"
            :class="{ active: revisionPreview?.id === r.id }"
            @click="previewRevision(r.id)"
          >
            <div class="history-time">{{ formatTime(r.created_at) }}</div>
            <div class="history-meta">{{ r.editor || '未知' }} · {{ r.size }} 字</div>
          </div>
        </div>
        <div class="history-preview">
          <template v-if="revisionPreview">
            <div class="history-preview-head">
              <span class="history-preview-title">{{ revisionPreview.title }}</span>
              <div class="history-head-actions">
                <el-radio-group v-model="historyView" size="small">
                  <el-radio-button value="diff">对比当前</el-radio-button>
                  <el-radio-button value="preview">原文</el-radio-button>
                </el-radio-group>
                <el-button size="small" type="primary" @click="restoreRevision(revisionPreview.id)">恢复此版本</el-button>
              </div>
            </div>
            <template v-if="historyView === 'diff'">
              <div class="history-diff-stat">
                <span>该版本 → 当前</span>
                <span class="stat-add">+{{ historyDiffStats.add }}</span>
                <span class="stat-del">-{{ historyDiffStats.del }}</span>
                <span class="stat-equal">未变 {{ historyDiffStats.equal }} 行</span>
              </div>
              <div class="history-diff-body">
                <div v-if="!revisionDiff.length" class="muted-hint" style="padding: 10px">内容一致，无差异</div>
                <div
                  v-for="(op, i) in revisionDiff"
                  :key="i"
                  class="diff-line"
                  :class="'diff-' + op.type"
                >
                  <span class="diff-sign">{{ op.type === 'add' ? '+' : op.type === 'del' ? '-' : ' ' }}</span>
                  <span class="diff-text">{{ op.text || ' ' }}</span>
                </div>
              </div>
            </template>
            <pre v-else class="history-content">{{ revisionPreview.content }}</pre>
          </template>
          <div v-else class="muted-hint" style="padding: 14px">选择左侧版本查看内容</div>
        </div>
      </div>
    </el-dialog>

    <!-- 从模板新建: 变量填充 -->
    <el-dialog v-model="tplVarOpen" :title="`从模板新建：${tplVarTemplate?.name || ''}`" width="520px">
      <p class="muted-hint" style="margin-bottom: 10px">
        模板内容含 {{ TPL_VAR_EXAMPLE }} 占位符，填写后将生成新笔记；留空则保留原占位符。
      </p>
      <el-form label-width="140px" label-position="left">
        <el-form-item v-for="v in tplVarList" :key="v" :label="v">
          <el-input v-model="tplVarValues[v]" :placeholder="tplVarPlaceholder(v)" @keyup.enter="confirmTemplateVars" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tplVarOpen = false">取消</el-button>
        <el-button type="primary" @click="confirmTemplateVars">生成笔记</el-button>
      </template>
    </el-dialog>

    <!-- 笔记本 zip 导出进度 -->
    <div v-if="zipExport.running" class="zip-progress">
      <div class="zip-progress-box">
        <div class="zip-progress-title">正在导出「{{ zipExport.notebook }}」…</div>
        <div class="zip-progress-bar"><div class="zip-progress-fill" :style="{ width: zipPercent + '%' }"></div></div>
        <div class="zip-progress-text">
          {{ zipExport.done }}/{{ zipExport.total }} 页<template v-if="zipExport.failed">（失败 {{ zipExport.failed }}）</template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElNotification, ElMessageBox } from 'element-plus'
import type { UploadFile as ElUploadFile } from 'element-plus'
import http from '../api/http'
import TipTapEditor from '../components/TipTapEditor.vue'
import { useAuthStore } from '../stores/auth'
import { PERM } from '../constants/perms'
import MarkdownIt from 'markdown-it'
import taskLists from 'markdown-it-task-lists'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import { diffLines, diffStats } from '../utils/diff'
import { loadDraft, saveDraft, clearDraft, shouldOfferDraft } from '../utils/draft'
import { extractTemplateVariables, renderTemplateContent } from '../utils/templateVars'
import {
  ARCHIVE_ERRORS_FILE, buildArchiveEntries, mapLimit, renderArchivePage, sanitizeFileSegment,
  type ArchiveTreePage,
} from '../utils/pageArchive'
import { inlineImagesInHtml } from '../utils/exportAssets'
import { extractHeadings } from '../utils/wikiAnchors'
import { asBlob as htmlToDocxBlob } from 'html-docx-js-typescript'
import JSZip from 'jszip'
import {
  isCollabFeatureEnabled, getCachedCollabProbe, setCachedCollabProbe,
  probeCollabServer, type CollabPeer,
} from '../utils/collab'

const exportMd = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  highlight(str: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      try { return (hljs.highlight(str, { language: lang }) as any).value } catch { /* ignore */ }
    }
    return (hljs.highlightAuto(str) as any).value
  },
}).use(taskLists, { enabled: false, label: true })

const route = useRoute()
const router = useRouter()

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

// ---- 协同编辑(Yjs over WebSocket) ----
// 默认开启; `VITE_RAG_COLLAB_ENABLED=false` 构建期关闭, `?collab=0` 运行期一键退回单人。
const auth = useAuthStore()
const collabFeatureEnabled = isCollabFeatureEnabled({
  query: window.location.search,
  envValue: import.meta.env.VITE_RAG_COLLAB_ENABLED,
})
const collabServerOk = ref(getCachedCollabProbe() === true)
const collabUsers = ref<CollabPeer[]>([])
const collabConnected = ref(false)
const wsBase = computed(() => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${API_BASE}/api/collab`
})
function colorFor(name: string) {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return `hsl(${h}, 70%, 50%)`
}
let collabProbePromise: Promise<boolean> | null = null
/** WS 探活(结果带缓存): 失败时编辑器以单人模式挂载, 不破坏编辑与保存。 */
const ensureCollabReady = (): Promise<boolean> => {
  if (!collabFeatureEnabled) return Promise.resolve(false)
  const cached = getCachedCollabProbe()
  if (cached !== null) {
    collabServerOk.value = cached
    return Promise.resolve(cached)
  }
  if (!collabProbePromise) {
    collabProbePromise = probeCollabServer(wsBase.value, 1500)
      .then(ok => {
        setCachedCollabProbe(ok)
        collabServerOk.value = ok
        return ok
      })
      .finally(() => { collabProbePromise = null })
  }
  return collabProbePromise
}
const collab = computed(() => {
  if (!collabFeatureEnabled || !collabServerOk.value || !currentPage.value) return null
  const name = auth.user?.name || auth.user?.display_name || auth.user?.username || '匿名'
  return { url: wsBase.value, room: `page-${currentPage.value.id}`, user: { name, color: colorFor(name) } }
})
const collabActive = computed(() => !!collab.value)
const collabTitle = computed(() => {
  if (!collabActive.value) return ''
  if (!collabConnected.value) return '协同连接中断，编辑仍会自动保存'
  const names = collabUsers.value.map(u => u.name).join('、')
  return names ? `协同编辑中：${names}` : '协同编辑中'
})
/** 探活通过但实际同步失败(服务假死/半通) → 回退单人编辑并短时禁止再次进入协同。 */
const handleCollabUnavailable = () => {
  if (!collabActive.value) return
  setCachedCollabProbe(false)
  collabServerOk.value = false
  collabUsers.value = []
  collabConnected.value = false
  ElMessage.warning('协作服务不可用，已切换为单人编辑（内容仍会自动保存）')
}
if (collabFeatureEnabled) void ensureCollabReady()

interface Notebook {
  id: string
  name: string
  description?: string
  icon?: string
  embedding_profile_id?: string | null
  position?: number
  section?: string
  visibility?: 'self' | 'dept' | 'public'
  owner_id?: string | null
  acl_users?: string[]
  acl_groups?: string[]
}

interface EmbeddingProfile {
  id: string
  name: string
  is_default: boolean
}

interface PageListItem {
  id: string
  title: string
  notebook_id: string | null
  parent_id?: string | null
  position?: number
  view_type?: string
  status?: string
  icon?: string
  updated_at: string
}

interface Page {
  id: string
  notebook_id: string | null
  title: string
  content: string
  icon?: string
  cover?: string
  cover_offset?: number
  parent_id?: string | null
  position?: number
  view_type?: string
  status?: string
  share_token?: string | null
  updated_at: string
}

const notebooks = ref<Notebook[]>([])
const treePages = ref<PageListItem[]>([])
/** 页面树缓存(按笔记本): 展开/收起来回切换不重复请求; 任何增删改/排序后由 loadTree 刷新 */
const treeCache = new Map<string, PageListItem[]>()
const currentNotebook = ref<Notebook | null>(null)
const currentPage = ref<Page | null>(null)
const saveStatus = ref<'saved' | 'saving' | 'unsaved' | 'error'>('saved')
const lastSavedAt = ref<Date | null>(null)
let saveRetryTimer: number | null = null
const saveText = computed(() => {
  if (saveStatus.value === 'saving') return '保存中…'
  if (saveStatus.value === 'unsaved') return '未保存'
  if (saveStatus.value === 'error') return '保存失败(点击重试)'
  if (!lastSavedAt.value) return '已保存'
  const t = lastSavedAt.value.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })
  return `已保存 ${t}`
})
const draftUid = () => auth.user?.id || 'anon'
/** 冷启动时 auth.user 可能尚未就绪, 先补拉一次 /api/auth/me, 保证草稿 key 用真实用户 id。 */
const ensureDraftUid = async (): Promise<string> => {
  if (!auth.user && auth.token) {
    try { await auth.fetchMe() } catch { /* ignore */ }
  }
  return draftUid()
}
/** 编辑器实例(用于把恢复的历史版本/离线草稿写入编辑器; 协同模式下必须显式写入 Yjs)。 */
const editorRef = ref<{
  applyExternalContent?: (markdown: string) => void
  prepareForPrint?: () => void
  finishPrint?: () => void
  findQuery?: (query: string) => void
} | null>(null)
const applyContentToEditor = (markdown: string) => {
  editorRef.value?.applyExternalContent?.(markdown || '')
}

// F6: 浏览器直接打印(Ctrl+P)兜底 —— 打印前强制展开惰渲染图片/mermaid,
// 打印后恢复; Safari 等不触发 beforeprint 的场景由 matchMedia('print') 变化覆盖。
const prepareEditorForPrint = () => { editorRef.value?.prepareForPrint?.() }
const finishEditorPrint = () => { editorRef.value?.finishPrint?.() }
const printMedia = window.matchMedia('print')
const onPrintMediaChange = (e: MediaQueryListEvent) => {
  if (e.matches) prepareEditorForPrint()
  else finishEditorPrint()
}

/** PUT 失败时备份最新内容(按用户隔离), 保存成功后清理。 */
const backupDraft = (page: Page) => {
  saveDraft(localStorage, draftUid(), page.id, {
    content: page.content || '',
    title: page.title || '',
    savedAt: Date.now(),
    baseUpdatedAt: page.updated_at || '',
  })
}

/** 打开页面后: 若本地草稿比服务端新, 提示「恢复/丢弃」。 */
const maybeOfferDraft = async (page: Page) => {
  const uid = await ensureDraftUid()
  const draft = loadDraft(localStorage, uid, page.id)
  if (!draft) return
  if (draft.content === (page.content || '')) {
    clearDraft(localStorage, uid, page.id)
    return
  }
  if (!shouldOfferDraft(draft, page.content, page.updated_at)) return
  const timeText = new Date(draft.savedAt).toLocaleString('zh-CN', { hour12: false })
  let action = ''
  try {
    await ElMessageBox.confirm(
      `检测到该笔记有未保存草稿（${timeText} 备份，上次自动保存失败），是否恢复？`,
      '未保存草稿',
      { confirmButtonText: '恢复草稿', cancelButtonText: '丢弃', type: 'warning', distinguishCancelAndClose: true },
    )
    action = 'confirm'
  } catch (e: any) {
    action = e === 'cancel' ? 'cancel' : 'close'
  }
  if (currentPage.value?.id !== page.id) return
  if (action === 'confirm') {
    page.content = draft.content
    if (draft.title) page.title = draft.title
    applyContentToEditor(page.content)
    scheduleSave()
    ElMessage.success('已恢复草稿，正在保存')
  } else if (action === 'cancel') {
    clearDraft(localStorage, uid, page.id)
  }
}

const searchQuery = ref('')
const sidebarCollapsed = ref(localStorage.getItem('rag-sidebar-collapsed') === '1')
const sidebarWidth = ref(Math.min(360, Math.max(208, Number(localStorage.getItem('rag-sidebar-width')) || 260)))
const collapsedPages = ref<string[]>([])
watch(sidebarCollapsed, (v) => localStorage.setItem('rag-sidebar-collapsed', v ? '1' : '0'))
watch(sidebarWidth, (v) => localStorage.setItem('rag-sidebar-width', String(v)))
const showNewNotebook = ref(false)
const newNotebookName = ref('')
const newNotebookVisibility = ref<'self' | 'dept' | 'public'>('dept')
const profiles = ref<EmbeddingProfile[]>([])
const showNotebookSettings = ref(false)
const notebookSaving = ref(false)
const NOTEBOOK_ICONS = ['📁', '📘', '📗', '📙', '📕', '🗂️', '🧭', '🧩', '⚙️', '🚀', '💡', '🧪', '🛠️', '🌐', '📊', '🤖']
const notebookForm = reactive({
  id: '', name: '', description: '', icon: '', embedding_profile_id: '' as string | null,
  visibility: 'dept' as 'self' | 'dept' | 'public',
  acl_users: [] as string[], acl_groups: [] as string[],
})
// 管理员(notebook.manage)可配置资源级追加授权
const canManageNotebook = computed(() => auth.hasPerm(PERM.notebook))
const aclUserOptions = ref<{ id: string; username: string; name: string }[]>([])
const aclGroupOptions = ref<string[]>([])
const loadAclOptions = async () => {
  if (!canManageNotebook.value) return
  try {
    const res = await http.get('/api/auth/users')
    aclUserOptions.value = res.data || []
  } catch {
    aclUserOptions.value = []
  }
  try {
    const res = await http.get('/api/admin/groups')
    aclGroupOptions.value = (res.data.items || []).map((g: any) => g.name)
  } catch {
    // 无 group.manage/user.manage 时列表为空, 下拉仍可 allow-create 手输组名
    aclGroupOptions.value = []
  }
}
const showSearch = ref(false)
const searchResults = ref<any[]>([])

const importDialogVisible = ref(false)
const importTab = ref('file')
const importUrl = ref('')
const importText = ref('')
const selectedFile = ref<File | null>(null)
const importLoading = ref(false)
const confirmDialogVisible = ref(false)
const confirmLoading = ref(false)
const confirmForm = ref<{ should_save: boolean; action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; summary: string; content: string; notebooks: { id: string; name: string }[]; pages: { id: string; title: string }[] }>({
  should_save: true, action: 'create_note', title: '', notebook_id: null, new_notebook_name: '', update_page_id: null, summary: '', content: '', notebooks: [], pages: [],
})
const organizing = ref(false)

let saveTimeout: number | null = null
const pageLoading = ref(false)
let pageLoadSeq = 0
let loadAbort: AbortController | null = null
const indexing = ref(false)
const tagOptions = ref<{ tag: string; count: number }[]>([])
const activeTag = ref('')
const tagPages = ref<PageListItem[]>([])

const loadTags = async () => {
  try {
    const res = await http.get('/api/pages/tags')
    tagOptions.value = res.data.tags || []
  } catch { /* ignore */ }
}

watch(activeTag, async (tag) => {
  if (!tag) {
    tagPages.value = []
    return
  }
  try {
    const res = await http.get('/api/pages', { params: { tag, page: 1, page_size: 100 } })
    tagPages.value = res.data.items
  } catch { /* ignore */ }
})

const loadNotebooks = async () => {
  try {
    const res = await http.get('/api/notebooks')
    notebooks.value = res.data.notebooks
  } catch (e) {
    ElMessage.error('加载笔记本失败')
  }
}

const loadProfiles = async () => {
  try {
    const res = await http.get('/api/embeddings/profiles')
    profiles.value = res.data
  } catch {
    profiles.value = []
  }
}

const openNotebookSettings = async (nb: Notebook) => {
  notebookForm.id = nb.id
  notebookForm.name = nb.name
  notebookForm.description = nb.description || ''
  notebookForm.icon = nb.icon || ''
  notebookForm.embedding_profile_id = nb.embedding_profile_id || ''
  notebookForm.visibility = (nb.visibility === 'self' || nb.visibility === 'public') ? nb.visibility : 'dept'
  notebookForm.acl_users = nb.acl_users || []
  notebookForm.acl_groups = nb.acl_groups || []
  showNotebookSettings.value = true
  void loadAclOptions()
  try {
    // 详情接口带 ACL 预填(列表行不含)
    const res = await http.get(`/api/notebooks/${nb.id}`)
    if (notebookForm.id !== nb.id) return
    const detail = res.data
    notebookForm.name = detail.name ?? notebookForm.name
    notebookForm.description = detail.description || ''
    notebookForm.icon = detail.icon || ''
    notebookForm.embedding_profile_id = detail.embedding_profile_id || ''
    notebookForm.visibility = (detail.visibility === 'self' || detail.visibility === 'public') ? detail.visibility : 'dept'
    notebookForm.acl_users = detail.acl_users || []
    notebookForm.acl_groups = detail.acl_groups || []
  } catch {
    // 拉取失败时保留列表行数据
  }
}

const saveNotebookSettings = async () => {
  if (!notebookForm.name.trim()) {
    ElMessage.warning('请输入笔记本名称')
    return
  }
  notebookSaving.value = true
  try {
    const payload: Record<string, unknown> = {
      name: notebookForm.name,
      description: notebookForm.description,
      icon: notebookForm.icon || '',
      embedding_profile_id: notebookForm.embedding_profile_id || '',
      visibility: notebookForm.visibility,
    }
    if (canManageNotebook.value) {
      payload.acl_users = notebookForm.acl_users
      payload.acl_groups = notebookForm.acl_groups
    }
    const res = await http.put(`/api/notebooks/${notebookForm.id}`, payload)
    const updated = res.data
    const idx = notebooks.value.findIndex(n => n.id === notebookForm.id)
    if (idx >= 0) notebooks.value[idx] = { ...notebooks.value[idx], ...updated }
    if (currentNotebook.value?.id === notebookForm.id) {
      currentNotebook.value = { ...currentNotebook.value, ...updated }
    }
    showNotebookSettings.value = false
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    notebookSaving.value = false
  }
}

const treeRows = computed(() => {
  const items = treePages.value
  const byId = new Map<string, PageListItem>()
  items.forEach(p => byId.set(p.id, p))
  const childrenOf = new Map<string | null, PageListItem[]>()
  for (const p of items) {
    const pid = p.parent_id && byId.has(p.parent_id) ? p.parent_id : null
    if (!childrenOf.has(pid)) childrenOf.set(pid, [])
    childrenOf.get(pid)!.push(p)
  }
  const rows: { page: PageListItem; depth: number; hasChildren: boolean; collapsed: boolean }[] = []
  const walk = (pid: string | null, depth: number) => {
    const kids = (childrenOf.get(pid) || []).slice().sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    for (const k of kids) {
      const hasChildren = (childrenOf.get(k.id) || []).length > 0
      const collapsed = collapsedPages.value.includes(k.id)
      rows.push({ page: k, depth, hasChildren, collapsed })
      if (!collapsed) walk(k.id, depth + 1)
    }
  }
  walk(null, 0)
  return rows
})

/** 渲染分批: 大树笔记本(如 255 篇)一次挂载全部行会卡顿 — 默认一批 100 条, 底部「加载更多」递增 */
const TREE_RENDER_BATCH = 100
const renderLimit = ref(TREE_RENDER_BATCH)
const treeRowsVisible = computed(() => treeRows.value.slice(0, renderLimit.value))
const treeRowsMore = computed(() => Math.max(0, treeRows.value.length - renderLimit.value))

const toggleCollapse = (id: string) => {
  if (collapsedPages.value.includes(id)) {
    collapsedPages.value = collapsedPages.value.filter(x => x !== id)
  } else {
    collapsedPages.value = [...collapsedPages.value, id]
  }
}

const loadTree = async () => {
  if (!currentNotebook.value) { treePages.value = []; return }
  const nbId = currentNotebook.value.id
  try {
    const res = await http.get('/api/pages/tree', { params: { notebook_id: nbId } })
    const items: PageListItem[] = res.data.items || []
    treeCache.set(nbId, items)
    treePages.value = items
  } catch {
    ElMessage.error('加载笔记失败')
  }
}

const selectNotebook = async (nb: Notebook) => {
  if (currentNotebook.value?.id === nb.id) {
    currentNotebook.value = null
    treePages.value = []
    return
  }
  currentNotebook.value = nb
  renderLimit.value = TREE_RENDER_BATCH
  const cached = treeCache.get(nb.id)
  if (cached) {
    treePages.value = cached          // 命中缓存: 展开不再重新请求(去掉「卡一下」的请求延迟)
  } else {
    await loadTree()
  }
  if (treePages.value.length === 0) {
    await createPage()
  }
}

// ---- 页面树拖拽排序/层级嵌套 ----
const dragPageId = ref<string | null>(null)
const dropTarget = ref<{ id: string; zone: 'before' | 'after' | 'inside' } | null>(null)

const onPageDragStart = (page: PageListItem, ev: DragEvent) => {
  dragPageId.value = page.id
  if (ev.dataTransfer) {
    ev.dataTransfer.effectAllowed = 'move'
    ev.dataTransfer.setData('text/plain', page.id)
  }
}

const onPageDragOver = (page: PageListItem, ev: DragEvent) => {
  if (!dragPageId.value || dragPageId.value === page.id) return
  ev.preventDefault()
  const el = ev.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  const y = ev.clientY - rect.top
  const zone: 'before' | 'after' | 'inside' =
    y < rect.height * 0.3 ? 'before' : y > rect.height * 0.7 ? 'after' : 'inside'
  dropTarget.value = { id: page.id, zone }
}

const onPageDrop = async (page: PageListItem, ev: DragEvent) => {
  ev.preventDefault()
  const dragId = dragPageId.value
  const target = dropTarget.value
  dragPageId.value = null
  dropTarget.value = null
  if (!dragId || !target || dragId === page.id) return

  let parentId: string | null = null
  let position = 0
  if (target.zone === 'inside') {
    parentId = page.id
    position = treePages.value.filter(p => p.parent_id === page.id).length
  } else {
    parentId = page.parent_id ?? null
    const siblings = treePages.value
      .filter(p => (p.parent_id ?? null) === parentId)
      .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    const idx = siblings.findIndex(p => p.id === page.id)
    position = target.zone === 'before' ? Math.max(0, idx) : idx + 1
  }
  try {
    await http.post(`/api/pages/${dragId}/move`, { parent_id: parentId, position })
    await loadTree()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '移动失败')
  }
}

const onPageDragEnd = () => {
  dragPageId.value = null
  dropTarget.value = null
}

// 侧边栏宽度拖拽
const startSidebarResize = (ev: MouseEvent) => {
  const startX = ev.clientX
  const startW = sidebarWidth.value
  const onMove = (e: MouseEvent) => {
    sidebarWidth.value = Math.min(360, Math.max(208, startW + (e.clientX - startX)))
  }
  const onUp = () => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'col-resize'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

const selectPage = async (page: PageListItem) => {
  if (saveTimeout) {
    clearTimeout(saveTimeout)
    saveTimeout = null
  }
  // Fire the pending save without blocking the switch; it captured its own
  // page reference, so it still writes to the note the user just left.
  const prev = currentPage.value
  if (saveStatus.value === 'unsaved' && prev) {
    savePage(prev)
  }
  // 协同探活: 结果未知时稍等(通常 <100ms), 避免先以单人模式挂载再中途切换
  if (getCachedCollabProbe() === null) await ensureCollabReady()

  // Switch the UI immediately (placeholder) and load content in the
  // background, so clicking a note never blocks on the network or parse.
  currentPage.value = {
    id: page.id,
    notebook_id: page.notebook_id ?? null,
    title: page.title,
    content: '',
    icon: '',
    cover: '',
    updated_at: page.updated_at,
  }
  pageLoading.value = true
  resetPerPageState()
  const seq = ++pageLoadSeq
  loadAbort?.abort()
  const controller = new AbortController()
  loadAbort = controller
  try {
    const res = await http.get(`/api/pages/${page.id}`, { signal: controller.signal })
    if (seq !== pageLoadSeq) return
    currentPage.value = res.data
    void maybeOfferDraft(res.data)
  } catch (e: any) {
    if (seq !== pageLoadSeq) return
    ElMessage.error('加载笔记内容失败')
  } finally {
    if (seq === pageLoadSeq) {
      pageLoading.value = false
      loadAbort = null
    }
  }
}

const openPageById = async (pageId: string, anchor = '') => {
  if (getCachedCollabProbe() === null) await ensureCollabReady()
  currentPage.value = { id: pageId, notebook_id: null, title: '加载中...', content: '', icon: '', cover: '', updated_at: '' }
  pageLoading.value = true
  resetPerPageState()
  const seq = ++pageLoadSeq
  loadAbort?.abort()
  const controller = new AbortController()
  loadAbort = controller
  try {
    const res = await http.get(`/api/pages/${pageId}`, { signal: controller.signal })
    if (seq !== pageLoadSeq) return
    const page = res.data
    currentPage.value = page
    void maybeOfferDraft(page)
    if (anchor) scrollToAnchorInEditor(anchor)
    if (page.notebook_id) {
      const nb = notebooks.value.find(n => n.id === page.notebook_id)
      if (nb) {
        currentNotebook.value = nb
        loadTree()
      }
    }
  } catch (e: any) {
    if (seq !== pageLoadSeq) return
    ElMessage.error('打开笔记失败')
  } finally {
    if (seq === pageLoadSeq) {
      pageLoading.value = false
      loadAbort = null
    }
  }
}

/**
 * [[页面#锚点]] 点击后滚动定位: 编辑器标题 id 由 TipTapEditor 装饰生成,
 * 页面切换后组件重挂载, 用有限重试等待标题渲染完成。
 */
const scrollToAnchorInEditor = (anchor: string) => {
  const slug = String(anchor || '').trim()
  if (!slug) return
  let attempts = 0
  const tryScroll = () => {
    attempts++
    let el: HTMLElement | null = null
    try {
      el = document.querySelector(`.ProseMirror [id="${CSS.escape(slug)}"]`) as HTMLElement | null
    } catch { /* ignore */ }
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      el.classList.add('wiki-anchor-flash')
      window.setTimeout(() => el?.classList.remove('wiki-anchor-flash'), 1700)
      return
    }
    if (attempts < 30) window.setTimeout(tryScroll, 150)
  }
  tryScroll()
}

/** 编辑器内点击 [[页面#锚点]]: 同页直接滚动, 跨页打开目标页后定位 */
const onWikiLink = async (payload: { pageId: string; title: string; anchor: string }) => {
  if (!payload?.pageId) return
  if (currentPage.value?.id === payload.pageId) {
    scrollToAnchorInEditor(payload.anchor)
    return
  }
  await openPageById(payload.pageId, payload.anchor)
}

/** 引用卡片 / 知识库检索结果点击: wiki → 跳阅读页; 笔记 → 编辑器内打开 */
const onCitationOpen = (payload: { id: string; kind: string }) => {
  if (!payload?.id) return
  if (payload.kind === 'wiki' || payload.id.startsWith('wiki:')) {
    router.push(`/wiki/${payload.id.replace(/^wiki:/, '')}`)
    return
  }
  void openPageById(payload.id)
}

/** 全库搜索结果定位: 当前页直接开查找条; 跨页打开后等编辑器就绪再填词定位 */
const onGlobalSearchLocate = async (payload: { pageId: string; query: string }) => {
  if (!payload?.pageId) return
  if (currentPage.value?.id !== payload.pageId) {
    await openPageById(payload.pageId)
    await nextTick()
  }
  // 编辑器实例可能尚未挂载/挂载中: 有限重试, 失败则放弃定位(页面已打开)
  const tryFocusFind = (attempts = 0) => {
    const fn = editorRef.value?.findQuery
    if (fn) { fn(payload.query); return }
    if (attempts < 10) window.setTimeout(() => tryFocusFind(attempts + 1), 150)
  }
  tryFocusFind()
}

/** 切换页面时重置保存状态与在线列表(避免上一页状态残留)。 */
const resetPerPageState = () => {
  saveStatus.value = 'saved'
  lastSavedAt.value = null
  collabUsers.value = []
  collabConnected.value = false
}

const handleCreateNotebook = async () => {
  if (!newNotebookName.value.trim()) {
    ElMessage.warning('请输入笔记本名称')
    return
  }
  try {
    const res = await http.post('/api/notebooks', {
      name: newNotebookName.value,
      visibility: newNotebookVisibility.value,
    })
    notebooks.value.unshift(res.data)
    newNotebookName.value = ''
    newNotebookVisibility.value = 'dept'
    showNewNotebook.value = false
    currentNotebook.value = res.data
    await createPage()
    ElMessage.success('创建成功')
  } catch (e) {
    ElMessage.error('创建失败')
  }
}

const handleNotebookCmd = async (cmd: string, nb: Notebook) => {
  if (cmd === 'newPage') {
    if (currentNotebook.value?.id !== nb.id) {
      currentNotebook.value = nb
      await loadTree()
    }
    await createPage()
    return
  }
  if (cmd === 'settings') {
    openNotebookSettings(nb)
    return
  }
  if (cmd === 'exportZip') {
    await exportNotebookZip(nb)
    return
  }
  if (cmd === 'delete') {
    try {
      await http.delete(`/api/notebooks/${nb.id}`)
      ElMessage.success('删除成功')
      if (currentNotebook.value?.id === nb.id) {
        currentNotebook.value = null
        treePages.value = []
        currentPage.value = null
      }
      treeCache.delete(nb.id)
      loadNotebooks()
    } catch {
      ElMessage.error('删除失败')
    }
  }
}

const handlePageCmd = async (cmd: string, page: PageListItem) => {
  if (cmd === 'delete') {
    try {
      await http.delete(`/api/pages/${page.id}`)
      ElMessage.success('删除成功')
      treePages.value = treePages.value.filter(p => p.id !== page.id)
      if (currentPage.value?.id === page.id) {
        currentPage.value = null
      }
      await loadTree()
    } catch {
      ElMessage.error('删除失败')
    }
  } else if (cmd === 'index') {
    try {
      ElMessage.info('正在索引...')
      await http.post(`/api/pages/${page.id}/index`)
      ElMessage.success('索引完成')
    } catch {
      ElMessage.error('索引失败')
    }
  } else if (cmd === 'child') {
    await createPage(page.id)
  }
}

const createPage = async (parentId: string | null = null) => {
  if (!currentNotebook.value) {
    ElMessage.warning('请先选择笔记本')
    return
  }
  try {
    const res = await http.post('/api/pages', {
      title: '无标题',
      content: '',
      notebook_id: currentNotebook.value.id,
      parent_id: parentId
    })
    pageLoadSeq++
    loadAbort?.abort()
    loadAbort = null
    currentPage.value = res.data
    pageLoading.value = false
    resetPerPageState()
    await loadTree()
    ElMessage.success('创建成功')
  } catch (e) {
    ElMessage.error('创建失败')
  }
}

const scheduleSave = () => {
  saveStatus.value = 'unsaved'
  if (saveTimeout) clearTimeout(saveTimeout)
  saveTimeout = window.setTimeout(() => savePage(), 1000)
}

/** 手动重试(状态指示点击触发); 仍失败会再次进入自动重试/草稿兜底。 */
const retrySave = () => {
  if (currentPage.value) void savePage(currentPage.value)
}

const savePage = async (target?: Page, isRetry = false) => {
  const page = target || currentPage.value
  if (!page) return
  if (saveTimeout) {
    clearTimeout(saveTimeout)
    saveTimeout = null
  }
  if (saveRetryTimer) {
    clearTimeout(saveRetryTimer)
    saveRetryTimer = null
  }
  if (currentPage.value?.id === page.id) saveStatus.value = 'saving'
  try {
    const res = await http.put(`/api/pages/${page.id}`, {
      title: page.title,
      content: page.content,
      icon: page.icon || '',
      cover: page.cover || ''
    })
    // 基准时间用于离线草稿的新旧判定(下次保存失败时记录)
    if (res.data?.updated_at) page.updated_at = res.data.updated_at
    // 保存成功 → 清理该页离线草稿
    clearDraft(localStorage, draftUid(), page.id)
    if (currentPage.value?.id === page.id) {
      saveStatus.value = 'saved'
      lastSavedAt.value = new Date()
    }
    const idx = treePages.value.findIndex(p => p.id === page.id)
    if (idx >= 0) {
      treePages.value[idx] = { ...treePages.value[idx], title: page.title, icon: page.icon || '' }
      // 同步树缓存(键路径同一数组; 防御性双写), 避免重新展开时命中旧的 icon/title
      if (currentNotebook.value) {
        const cached = treeCache.get(currentNotebook.value.id)
        const ci = cached ? cached.findIndex(p => p.id === page.id) : -1
        if (cached && ci >= 0) {
          cached[ci] = { ...cached[ci], title: page.title, icon: page.icon || '' }
        }
      }
      // 最近访问列表同步(仅内存; 服务端列表从 pages 表读取标题/图标)
      const ri = recentPages.value.findIndex(r => r.id === page.id)
      if (ri >= 0) {
        recentPages.value[ri] = { ...recentPages.value[ri], title: page.title, icon: page.icon || '' }
      }
    }
  } catch (e) {
    // 失败: 先落离线草稿兜底, 再自动重试一次; 仍失败则状态指示可点击手动重试
    backupDraft(page)
    if (!isRetry) {
      saveRetryTimer = window.setTimeout(() => {
        saveRetryTimer = null
        void savePage(page, true)
      }, 2000)
      return
    }
    ElMessage.error('保存失败，内容已备份到本地草稿')
    if (currentPage.value?.id === page.id) saveStatus.value = 'error'
  }
}

const iconPickerVisible = ref(false)
const coverPickerVisible = ref(false)
const EMOJI_GROUPS: { label: string; items: string[] }[] = [
  { label: '常用', items: ['📄', '📝', '📌', '📎', '📁', '📚', '📖', '✅', '⭐', '🔥', '💡', '📊'] },
  { label: '工作', items: ['🎯', '🚀', '📈', '🗂️', '🗓️', '📦', '🔧', '⚙️', '🧩', '🛠️', '🏷️', '📋'] },
  { label: '符号', items: ['❗', '❓', '⚠️', '⛔', '🔒', '🔑', '🧭', '🔗', '📐', '🧮', '🔔', '📣'] },
  { label: '其他', items: ['🧠', '🤖', '🐛', '🌱', '🌐', '🗺️', '💬', '🔍', '🧪', '🎨', '🏆', '🌈'] },
]
const GRADIENTS: { key: string; css: string }[] = [
  { key: 'sunset', css: 'linear-gradient(135deg, #f97316, #ec4899)' },
  { key: 'ocean', css: 'linear-gradient(135deg, #0ea5e9, #6366f1)' },
  { key: 'forest', css: 'linear-gradient(135deg, #10b981, #0ea5e9)' },
  { key: 'violet', css: 'linear-gradient(135deg, #8b5cf6, #6366f1)' },
  { key: 'rose', css: 'linear-gradient(135deg, #fb7185, #f59e0b)' },
  { key: 'mint', css: 'linear-gradient(135deg, #2dd4bf, #10b981)' },
  { key: 'peach', css: 'linear-gradient(135deg, #fdba74, #f472b6)' },
  { key: 'slate', css: 'linear-gradient(135deg, #334155, #0f172a)' },
]

const coverIsGradient = computed(() => !!currentPage.value?.cover?.startsWith('grad:'))
const coverGradientStyle = computed(() => {
  const c = currentPage.value?.cover || ''
  if (!c.startsWith('grad:')) return ''
  const key = c.slice(5)
  return GRADIENTS.find(g => g.key === key)?.css || GRADIENTS[0].css
})

const randomIcon = () => {
  const all = EMOJI_GROUPS.flatMap(g => g.items)
  setIcon(all[Math.floor(Math.random() * all.length)])
}

const setGradientCover = (key: string) => {
  if (!currentPage.value) return
  currentPage.value.cover = 'grad:' + key
  coverPickerVisible.value = false
  scheduleSave()
}

const pickCoverFromPicker = () => {
  coverPickerVisible.value = false
  pickCover()
}

const pickCover = () => {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/*'
  input.onchange = async (e: Event) => {
    const file = (e.target as HTMLInputElement).files?.[0]
    if (!file || !currentPage.value) return
    try {
      const formData = new FormData()
      formData.append('file', file)
      const res = await http.post('/api/upload/image', formData)
      currentPage.value.cover = res.data.url
      scheduleSave()
    } catch {
      ElMessage.error('封面上传失败')
    }
  }
  input.click()
}

const removeCover = () => {
  if (!currentPage.value) return
  currentPage.value.cover = ''
  scheduleSave()
}

const setIcon = (emoji: string) => {
  if (!currentPage.value) return
  currentPage.value.icon = emoji
  iconPickerVisible.value = false
  scheduleSave()
}

// ---------------- Notion 风格增强: 收藏 / 快速切换 / 大纲 / 页面菜单 / 回收站 ----------------
const favPages = ref<{ id: string; title: string }[]>([])
const recentPages = ref<{ id: string; title: string; icon?: string }[]>([])

// 最近访问请求序号: pushRecent/更新的 loadRecent 推进后, 在途 GET 回包作废
let recentSeq = 0
const loadRecent = async () => {
  const seq = ++recentSeq
  try {
    const res = await http.get('/api/me/recent-pages', { params: { limit: 5 } })
    if (seq !== recentSeq) return
    recentPages.value = (res.data.items || []).slice(0, 5)
  } catch {
    // 加载失败保持空列表, 不阻断编辑
  }
}

let recentPostedId = ''
const pushRecent = () => {
  const p = currentPage.value
  if (!p || !p.id) return
  const title = p.title && p.title !== '加载中...' ? p.title : ''
  if (!title) return
  recentSeq++
  recentPages.value = [{ id: p.id, title, icon: p.icon || '' }, ...recentPages.value.filter(r => r.id !== p.id)].slice(0, 5)
  // 打开页面时向服务端记录访问(每页每次打开仅一次); 失败静默, 不阻断阅读
  if (recentPostedId !== p.id) {
    recentPostedId = p.id
    http.post('/api/me/recent-pages', { page_id: p.id }).catch(() => {})
  }
}
const trashPages = ref<{ id: string; title: string; notebook_id: string | null; deleted_at: string }[]>([])
const trashOpen = ref(false)
const outlineOpen = ref(false)
const pageWide = ref(false)
const pageSmall = ref(false)

const loadFavorites = async () => {
  try {
    const res = await http.get('/api/me/favorites')
    favPages.value = (res.data.items || []).map((p: { id: string; title: string }) => ({ id: p.id, title: p.title }))
  } catch {
    // 加载失败保持空列表, 不阻断编辑
  }
}

const isFav = computed(() => !!currentPage.value && favPages.value.some(p => p.id === currentPage.value!.id))
const toggleFav = async () => {
  if (!currentPage.value) return
  const id = currentPage.value.id
  const title = currentPage.value.title || '无标题'
  if (favPages.value.some(p => p.id === id)) {
    favPages.value = favPages.value.filter(p => p.id !== id)
    try {
      await http.delete(`/api/me/favorites/${id}`)
    } catch {
      ElMessage.error('取消收藏失败')
      void loadFavorites() // 失败不猜状态, 以服务端为准重同步
    }
  } else {
    favPages.value = [{ id, title }, ...favPages.value]
    try {
      await http.post('/api/me/favorites', { page_id: id })
    } catch {
      ElMessage.error('收藏失败')
      void loadFavorites() // 失败不猜状态, 以服务端为准重同步
    }
  }
}

const backlinks = ref<{ id: string; title: string }[]>([])
const loadBacklinks = async (id?: string) => {
  if (!id) { backlinks.value = []; return }
  try {
    backlinks.value = (await http.get(`/api/pages/${id}/backlinks`)).data.items || []
  } catch { backlinks.value = [] }
}

watch(() => currentPage.value?.id, (id) => {
  if (!id) { pageWide.value = false; pageSmall.value = false; backlinks.value = []; return }
  pageWide.value = localStorage.getItem('rag-page-wide-' + id) === '1'
  pageSmall.value = localStorage.getItem('rag-page-small-' + id) === '1'
  loadBacklinks(id)
  loadComments(id)
  pushRecent()
})

watch(() => currentPage.value?.title, () => pushRecent())

const toggleWide = () => {
  if (!currentPage.value) return
  pageWide.value = !pageWide.value
  localStorage.setItem('rag-page-wide-' + currentPage.value.id, pageWide.value ? '1' : '0')
}
const toggleSmall = () => {
  if (!currentPage.value) return
  pageSmall.value = !pageSmall.value
  localStorage.setItem('rag-page-small-' + currentPage.value.id, pageSmall.value ? '1' : '0')
}

const copyText = async (text: string) => {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}
const copyLink = async () => {
  if (!currentPage.value) return
  await copyText(`${window.location.origin}${API_BASE}/notes?page=${currentPage.value.id}`)
  ElMessage.success('链接已复制')
}
const duplicatePage = async () => {
  if (!currentPage.value || !currentNotebook.value) return
  try {
    const res = await http.post('/api/pages', {
      title: (currentPage.value.title || '无标题') + ' 副本',
      content: currentPage.value.content,
      notebook_id: currentNotebook.value.id,
      parent_id: currentPage.value.parent_id ?? null,
    })
    await loadTree()
    currentPage.value = res.data
    ElMessage.success('已创建副本')
  } catch {
    ElMessage.error('复制失败')
  }
}
const exportMarkdown = () => {
  if (!currentPage.value) return
  const blob = new Blob([currentPage.value.content || ''], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = (currentPage.value.title || 'note').replace(/[\\/:*?"<>|]/g, '_') + '.md'
  a.click()
  URL.revokeObjectURL(a.href)
}
const deleteCurrent = async () => {
  if (!currentPage.value) return
  try {
    await ElMessageBox.confirm(`将「${currentPage.value.title || '无标题'}」移到回收站？`, '移到回收站', { type: 'warning' })
  } catch { return }
  await handlePageCmd('delete', currentPage.value as any)
}

// 回收站
const loadTrash = async () => {
  try {
    trashPages.value = (await http.get('/api/pages/trash')).data.items || []
  } catch { trashPages.value = [] }
}
const toggleTrash = async () => {
  trashOpen.value = !trashOpen.value
  if (trashOpen.value) await loadTrash()
}
const restoreTrash = async (id: string) => {
  try {
    await http.post(`/api/pages/${id}/restore`)
    ElMessage.success('已恢复')
    await loadTrash()
    if (currentNotebook.value) await loadTree()
  } catch { ElMessage.error('恢复失败') }
}
const purgeTrash = async (id: string, title: string) => {
  try {
    await ElMessageBox.confirm(`彻底删除「${title}」？此操作不可恢复。`, '彻底删除', { type: 'warning' })
  } catch { return }
  try {
    await http.delete(`/api/pages/${id}/purge`)
    ElMessage.success('已彻底删除')
    await loadTrash()
  } catch { ElMessage.error('删除失败') }
}

const onPageMenu = (cmd: string) => {
  if (cmd === 'fav') toggleFav()
  else if (cmd === 'link') copyLink()
  else if (cmd === 'dup') duplicatePage()
  else if (cmd === 'export') exportMarkdown()
  else if (cmd === 'wide') toggleWide()
  else if (cmd === 'small') toggleSmall()
  else if (cmd === 'history') openHistory()
  else if (cmd === 'share') openShare()
  else if (cmd === 'export-html') exportHtml()
  else if (cmd === 'export-docx') void exportDocx()
  else if (cmd === 'export-pdf') void exportPdf()
  else if (cmd === 'template') saveAsTemplate()
  else if (cmd.startsWith('view-')) setViewType(cmd.slice(5))
  else if (cmd === 'import') importDialogVisible.value = true
  else if (cmd === 'organize') handleOrganize()
  else if (cmd === 'delete') deleteCurrent()
}

// 分享(只读公开链接)
const shareOpen = ref(false)
const shareUrl = ref('')
const openShare = () => {
  if (!currentPage.value) return
  shareUrl.value = currentPage.value.share_token
    ? `${window.location.origin}${API_BASE}/share/${currentPage.value.share_token}`
    : ''
  shareOpen.value = true
}
const createShare = async () => {
  if (!currentPage.value) return
  try {
    const res = await http.post(`/api/pages/${currentPage.value.id}/share`)
    currentPage.value.share_token = res.data.token
    shareUrl.value = `${window.location.origin}${API_BASE}/share/${res.data.token}`
  } catch { ElMessage.error('创建分享失败') }
}
const cancelShare = async () => {
  if (!currentPage.value) return
  try {
    await http.delete(`/api/pages/${currentPage.value.id}/share`)
    currentPage.value.share_token = null
    shareUrl.value = ''
    ElMessage.success('已取消分享')
  } catch { ElMessage.error('取消失败') }
}
const copyShareUrl = async () => {
  if (!shareUrl.value) return
  await copyText(shareUrl.value)
  ElMessage.success('链接已复制')
}

// 版本历史
const historyOpen = ref(false)
const revisions = ref<{ id: string; title: string; editor: string; created_at: string; size: number }[]>([])
const revisionPreview = ref<{ id: string; title: string; content: string; editor: string; created_at: string } | null>(null)
const historyView = ref<'diff' | 'preview'>('diff')
const formatTime = (s: string) => (s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '')
/** 行级 diff: 该历史版本 → 当前内容(add 绿色=当前新增, del 红色=该版本有而当前没有)。 */
const revisionDiff = computed(() => {
  if (!revisionPreview.value || !currentPage.value) return []
  return diffLines(revisionPreview.value.content || '', currentPage.value.content || '')
})
const historyDiffStats = computed(() => diffStats(revisionDiff.value))
const openHistory = async () => {
  if (!currentPage.value) return
  historyOpen.value = true
  revisionPreview.value = null
  historyView.value = 'diff'
  try {
    revisions.value = (await http.get(`/api/pages/${currentPage.value.id}/revisions`)).data.items || []
  } catch { revisions.value = [] }
}
const previewRevision = async (id: string) => {
  if (!currentPage.value) return
  try {
    revisionPreview.value = (await http.get(`/api/pages/${currentPage.value.id}/revisions/${id}`)).data
    historyView.value = 'diff'
  } catch { ElMessage.error('加载版本失败') }
}
const restoreRevision = async (id: string) => {
  if (!currentPage.value) return
  try {
    const res = await http.post(`/api/pages/${currentPage.value.id}/revisions/${id}/restore`)
    currentPage.value = res.data
    applyContentToEditor(res.data?.content || '')
    await loadTree()
    historyOpen.value = false
    ElMessage.success('已恢复该版本')
  } catch { ElMessage.error('恢复失败') }
}

// 大纲(从正文标题提取)
const outlineItems = computed(() => {
  const c = currentPage.value?.content || ''
  const items: { level: number; text: string }[] = []
  for (const line of c.split('\n')) {
    const m = /^(#{1,6})\s+(.+?)\s*$/.exec(line)
    if (m) items.push({ level: m[1].length, text: m[2].replace(/[*_`]/g, '') })
  }
  return items
})
const scrollToHeading = (text: string) => {
  const els = document.querySelectorAll('.ProseMirror h1, .ProseMirror h2, .ProseMirror h3, .ProseMirror h4, .ProseMirror h5, .ProseMirror h6')
  for (const el of Array.from(els)) {
    if ((el.textContent || '').trim() === text) {
      (el as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
  }
}

// ---------------- 评论 ----------------
const comments = ref<{ id: string; author: string; author_id: string; content: string; resolved: boolean; created_at: string }[]>([])
const commentsOpen = ref(false)
const commentText = ref('')
const loadComments = async (id?: string) => {
  if (!id) { comments.value = []; return }
  try {
    comments.value = (await http.get(`/api/pages/${id}/comments`)).data.items || []
  } catch { comments.value = [] }
}
const toggleComments = () => {
  commentsOpen.value = !commentsOpen.value
  if (commentsOpen.value) {
    outlineOpen.value = false
    if (currentPage.value) loadComments(currentPage.value.id)
  }
}
const addComment = async () => {
  if (!currentPage.value || !commentText.value.trim()) return
  try {
    await http.post(`/api/pages/${currentPage.value.id}/comments`, { content: commentText.value.trim() })
    commentText.value = ''
    await loadComments(currentPage.value.id)
  } catch { ElMessage.error('评论失败') }
}
const toggleResolveComment = async (cid: string) => {
  if (!currentPage.value) return
  try {
    await http.post(`/api/pages/${currentPage.value.id}/comments/${cid}/resolve`)
    await loadComments(currentPage.value.id)
  } catch { ElMessage.error('操作失败') }
}
const deleteComment = async (cid: string) => {
  if (!currentPage.value) return
  try {
    await http.delete(`/api/pages/${currentPage.value.id}/comments/${cid}`)
    await loadComments(currentPage.value.id)
  } catch { ElMessage.error('删除失败') }
}
const commentTime = (s: string) => (s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '')

// ---------------- 导出 HTML / PDF / Word ----------------
const escapeHtmlText = (text: string) => String(text)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

const downloadBlob = (blob: Blob, filename: string) => {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  window.setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}

const safeFilename = (name: string) => (name || 'note').replace(/[\\/:*?"<>|]/g, '_')

const buildExportHtml = () => {
  const title = currentPage.value?.title || '无标题'
  const body = DOMPurify.sanitize(exportMd.render(currentPage.value?.content || ''), {
    ADD_TAGS: ['details', 'summary'],
    ADD_ATTR: ['data-callout', 'data-toggle', 'open', 'target'],
  })
  const css = "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:820px;margin:0 auto;padding:48px 24px;color:#1f2430;line-height:1.75}"
    + "h1{font-size:32px}h2{font-size:24px;margin-top:1.2em}h3{font-size:19px}img{max-width:100%;border-radius:10px}"
    + "pre{background:#282c34;color:#abb2bf;padding:14px 18px;border-radius:10px;overflow:auto}code{font-family:'Fira Code',monospace}"
    + "blockquote{border-left:3px solid #93c5fd;padding-left:14px;color:#4b5563;background:#f8fafc;margin:12px 0}"
    + "table{border-collapse:collapse;width:100%}th,td{border:1px solid #e5e7eb;padding:8px 12px}th{background:#f8fafc}"
    + "mark{background:#fef08a}.callout{border-left:4px solid #3b82f6;background:#eff6ff;padding:12px 16px;border-radius:10px;margin:14px 0}"
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtmlText(title)}</title><style>${css}</style></head><body><h1>${escapeHtmlText(title)}</h1>${body}</body></html>`
}

/** 打印版 HTML: 正文前插入 h1-h3 大纲页 + 打印分页样式(标题不孤行/表格代码块不跨页)。 */
const buildPrintHtml = () => {
  const title = currentPage.value?.title || '无标题'
  const content = currentPage.value?.content || ''
  const body = DOMPurify.sanitize(exportMd.render(content), {
    ADD_TAGS: ['details', 'summary'],
    ADD_ATTR: ['data-callout', 'data-toggle', 'open', 'target'],
  })
  const headings = extractHeadings(content).filter(h => h.level <= 3)
  const outlineItems = headings
    .map(h => `<li class="lv${h.level}"><span class="ol-text">${escapeHtmlText(h.text)}</span><span class="ol-dots"></span><span class="ol-page">&nbsp;</span></li>`)
    .join('')
  const outline = headings.length
    ? `<section class="pdf-outline"><h1>目录</h1><ol>${outlineItems}</ol>`
      + '<p class="pdf-outline-note">页码请在浏览器打印对话框「更多设置 → 页眉和页脚」中勾选显示。</p></section>'
    : ''
  const css = "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:820px;margin:0 auto;padding:48px 24px;color:#1f2430;line-height:1.75}"
    + "h1{font-size:32px}h2{font-size:24px;margin-top:1.2em}h3{font-size:19px}img{max-width:100%;border-radius:10px}"
    + "pre{background:#282c34;color:#abb2bf;padding:14px 18px;border-radius:10px;overflow:auto}code{font-family:'Fira Code',monospace}"
    + "blockquote{border-left:3px solid #93c5fd;padding-left:14px;color:#4b5563;background:#f8fafc;margin:12px 0}"
    + "table{border-collapse:collapse;width:100%}th,td{border:1px solid #e5e7eb;padding:8px 12px}th{background:#f8fafc}"
    + "mark{background:#fef08a}.callout{border-left:4px solid #3b82f6;background:#eff6ff;padding:12px 16px;border-radius:10px;margin:14px 0}"
    + ".pdf-outline{page-break-after:always;break-after:page;border-bottom:1px dashed #cbd5e1;padding-bottom:24px;margin-bottom:24px}"
    + ".pdf-outline h1{font-size:26px;margin-bottom:14px}.pdf-outline ol{list-style:none;padding:0;margin:0}"
    + ".pdf-outline li{display:flex;align-items:baseline;gap:6px;margin:7px 0;font-size:15px}"
    + ".pdf-outline li.lv2{padding-left:22px}.pdf-outline li.lv3{padding-left:44px;font-size:14px;color:#475569}"
    + ".ol-dots{flex:1;border-bottom:1px dotted #cbd5e1;transform:translateY(-3px)}"
    + ".ol-page{min-width:34px;text-align:right;color:#64748b}"
    + ".pdf-outline-note{margin-top:16px;font-size:12px;color:#94a3b8}"
    + "@media print{@page{margin:18mm 16mm}"
    + "h1,h2,h3,h4,h5,h6{break-after:avoid;page-break-after:avoid}"
    + "table,pre,blockquote,img,.callout,li{break-inside:avoid;page-break-inside:avoid}"
    + "tr,img{break-inside:avoid;page-break-inside:avoid}"
    + ".pdf-outline{break-after:page;page-break-after:always}}"
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtmlText(title)}</title><style>${css}</style></head><body>${outline}<h1>${escapeHtmlText(title)}</h1>${body}</body></html>`
}

const exportHtml = () => {
  if (!currentPage.value) return
  const blob = new Blob([buildExportHtml()], { type: 'text/html;charset=utf-8' })
  downloadBlob(blob, safeFilename(currentPage.value.title) + '.html')
}

/** Word (.docx): 当前内容 → HTML(图片尽力 base64 内嵌) → html-docx-js → .docx 下载。 */
const exportingDocx = ref(false)
const exportDocx = async () => {
  if (!currentPage.value || exportingDocx.value) return
  exportingDocx.value = true
  try {
    const { html, inlined, skipped } = await inlineImagesInHtml(buildExportHtml())
    const blob = await htmlToDocxBlob(html)
    if (!(blob instanceof Blob)) throw new Error('生成 docx 失败')
    downloadBlob(blob, safeFilename(currentPage.value.title) + '.docx')
    const imgHint = inlined ? `，内嵌 ${inlined} 张图片${skipped ? `（${skipped} 张失败已跳过）` : ''}` : ''
    ElMessage.success(`已导出 Word${imgHint}`)
  } catch (e: any) {
    ElMessage.error('导出 Word 失败：' + (e?.message || '未知错误'))
  } finally {
    exportingDocx.value = false
  }
}

const exportPdf = async () => {
  if (!currentPage.value) return
  const w = window.open('', '_blank')
  if (!w) { ElMessage.warning('请允许弹出窗口以导出 PDF'); return }
  w.document.write('<p style="font-family:sans-serif;padding:24px;color:#666">正在准备打印内容…</p>')
  const html = buildPrintHtml()
  // 图片尽力转 base64: 打印窗口与页面同源但无登录上下文, 相对地址/未签名地址会加载失败
  const inlined = await inlineImagesInHtml(html).catch(() => null)
  w.document.open()
  w.document.write(inlined?.html || html)
  w.document.close()
  w.focus()
  window.setTimeout(() => { w.print() }, 400)
}

// ---------------- 封面纵向位置 ----------------
const startCoverDrag = (ev: MouseEvent) => {
  if (!currentPage.value || coverIsGradient.value) return
  const startY = ev.clientY
  const startOffset = currentPage.value.cover_offset ?? 50
  const h = (ev.currentTarget as HTMLElement).getBoundingClientRect().height || 200
  const onMove = (e: MouseEvent) => {
    const delta = ((e.clientY - startY) / h) * 100
    if (currentPage.value) currentPage.value.cover_offset = Math.max(0, Math.min(100, Math.round(startOffset - delta)))
  }
  const onUp = () => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
    scheduleSave()
  }
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ---------------- 模板库(我的 / 跨用户共享 + 变量占位) ----------------
interface TemplateItem {
  id: string
  name: string
  content: string
  visibility?: string
  owner?: string
  owner_id?: string
  is_mine?: boolean
  updated_at?: string
}
const templates = ref<TemplateItem[]>([])
const myTemplates = computed(() => templates.value.filter(t => t.is_mine !== false))
const sharedTemplates = computed(() => templates.value.filter(t => t.is_mine === false))

const loadTemplates = async () => {
  try {
    const res = await http.get('/api/me/templates')
    templates.value = res.data.items || []
  } catch {
    // 加载失败保持空列表, 不阻断编辑
  }
}

const templateVarHint = (content: string) => {
  const vars = extractTemplateVariables(content || '')
  return vars.length ? `，识别到变量：${vars.join('、')}` : ''
}

const saveAsTemplate = async () => {
  if (!currentPage.value) return
  let name = currentPage.value.title || '未命名模板'
  try {
    const res = await ElMessageBox.prompt('模板名称', '另存为模板', { inputValue: name })
    name = res.value
  } catch { return }
  name = name.trim()
  if (!name) { ElMessage.warning('模板名称不能为空'); return }
  const content = currentPage.value.content || ''
  const existing = myTemplates.value.find(t => t.name === name)
  try {
    if (existing) {
      const res = await http.put(`/api/me/templates/${existing.id}`, { name, content })
      const idx = templates.value.findIndex(t => t.id === existing.id)
      if (idx >= 0) templates.value[idx] = res.data
      ElMessage.success('已更新模板' + templateVarHint(content))
    } else {
      const res = await http.post('/api/me/templates', { name, content })
      templates.value = [res.data, ...templates.value]
      ElMessage.success('已保存为模板' + templateVarHint(content))
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '保存模板失败')
  }
}

const toggleTemplateVisibility = async (t: TemplateItem) => {
  const next = t.visibility === 'public' ? 'private' : 'public'
  try {
    const res = await http.put(`/api/me/templates/${t.id}`, {
      name: t.name, content: t.content, visibility: next,
    })
    const idx = templates.value.findIndex(x => x.id === t.id)
    if (idx >= 0) templates.value[idx] = { ...templates.value[idx], ...res.data }
    ElMessage.success(next === 'public' ? '已共享给所有用户（只读）' : '已取消共享')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

// 从模板新建: 有 {{变量}} 时先弹窗收集, 填充后生成新页
const TPL_VAR_EXAMPLE = '{{变量名}}'
const tplVarPlaceholder = (name: string) => `{{${name}}}`
const tplVarOpen = ref(false)
const tplVarTemplate = ref<TemplateItem | null>(null)
const tplVarList = ref<string[]>([])
const tplVarValues = ref<Record<string, string>>({})

const createPageFromTemplate = async (t: TemplateItem, content: string) => {
  if (!currentNotebook.value) { ElMessage.warning('请先选择笔记本'); return }
  try {
    const res = await http.post('/api/pages', {
      title: t.name, content, notebook_id: currentNotebook.value.id,
    })
    currentPage.value = res.data
    await loadTree()
  } catch { ElMessage.error('创建失败') }
}

const newFromTemplate = async (t: TemplateItem) => {
  if (!currentNotebook.value) { ElMessage.warning('请先选择笔记本'); return }
  const vars = extractTemplateVariables(t.content || '')
  if (vars.length) {
    tplVarTemplate.value = t
    tplVarList.value = vars
    tplVarValues.value = Object.fromEntries(vars.map(v => [v, '']))
    tplVarOpen.value = true
    return
  }
  await createPageFromTemplate(t, t.content || '')
}

const confirmTemplateVars = async () => {
  const t = tplVarTemplate.value
  if (!t) return
  const content = renderTemplateContent(t.content || '', tplVarValues.value)
  tplVarOpen.value = false
  await createPageFromTemplate(t, content)
}

const deleteTemplate = async (id: string) => {
  templates.value = templates.value.filter(t => t.id !== id)
  try {
    await http.delete(`/api/me/templates/${id}`)
  } catch {
    ElMessage.error('删除模板失败')
    void loadTemplates() // 失败不猜状态, 以服务端为准重同步
  }
}

// ---------------- 整本笔记本导出(zip) ----------------
const zipExport = reactive({ running: false, notebook: '', done: 0, total: 0, failed: 0 })
const zipPercent = computed(() =>
  zipExport.total ? Math.round((zipExport.done / zipExport.total) * 100) : 0)

const exportNotebookZip = async (nb: Notebook) => {
  if (zipExport.running) return
  zipExport.running = true
  zipExport.notebook = nb.name
  zipExport.done = 0
  zipExport.total = 0
  zipExport.failed = 0
  try {
    const res = await http.get('/api/pages/tree', { params: { notebook_id: nb.id } })
    const pages: ArchiveTreePage[] = res.data.items || []
    if (!pages.length) { ElMessage.info('该笔记本还没有笔记'); return }
    zipExport.total = pages.length
    const paths = buildArchiveEntries(pages)
    const zip = new JSZip()
    const errors: string[] = []
    await mapLimit(pages, 4, async (page) => {
      const path = paths.get(page.id) || `${sanitizeFileSegment(page.title || '无标题')}.md`
      try {
        const detail = (await http.get(`/api/pages/${page.id}`)).data
        zip.file(path, renderArchivePage({
          title: detail.title || page.title,
          content: detail.content,
          updated_at: detail.updated_at || page.updated_at,
        }))
      } catch (e: any) {
        // 单页失败跳过(不阻断整本导出), 汇总写入 _errors.txt
        zipExport.failed++
        const reason = e?.response?.data?.detail || e?.message || '加载失败'
        errors.push(`${path}\t${e?.response?.status || ''} ${reason}`.trim())
      } finally {
        zipExport.done++
      }
    })
    if (errors.length) {
      zip.file(ARCHIVE_ERRORS_FILE, `导出失败 ${errors.length} 页：\n${errors.join('\n')}\n`)
    }
    const blob = await zip.generateAsync({ type: 'blob' })
    downloadBlob(blob, `${safeFilename(nb.name)}.zip`)
    if (errors.length) ElMessage.warning(`导出完成：${errors.length} 页失败，详见 ${ARCHIVE_ERRORS_FILE}`)
    else ElMessage.success(`已导出「${nb.name}」共 ${pages.length} 页`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '导出失败')
  } finally {
    zipExport.running = false
  }
}

// ---------------- 笔记本分组 + 拖拽排序 ----------------
const notebookGroups = computed(() => {
  const groups: { section: string; items: Notebook[] }[] = []
  for (const nb of notebooks.value) {
    const sec = nb.section || ''
    let g = groups.find(x => x.section === sec)
    if (!g) { g = { section: sec, items: [] }; groups.push(g) }
    g.items.push(nb)
  }
  return groups
})
const nbDragId = ref<string | null>(null)
const nbDrop = ref<{ id: string; zone: 'before' | 'after' } | null>(null)
const onNbDragStart = (nb: Notebook, ev: DragEvent) => {
  nbDragId.value = nb.id
  if (ev.dataTransfer) { ev.dataTransfer.effectAllowed = 'move'; ev.dataTransfer.setData('text/plain', nb.id) }
}
const onNbDragOver = (nb: Notebook, ev: DragEvent) => {
  if (!nbDragId.value || nbDragId.value === nb.id) return
  ev.preventDefault()
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  nbDrop.value = { id: nb.id, zone: (ev.clientY - rect.top) < rect.height / 2 ? 'before' : 'after' }
}
const onNbDrop = async (nb: Notebook, ev: DragEvent) => {
  ev.preventDefault()
  const dragId = nbDragId.value
  const t = nbDrop.value
  nbDragId.value = null
  nbDrop.value = null
  if (!dragId || !t || dragId === nb.id) return
  const section = nb.section || ''
  const sameSection = notebooks.value.filter(n => (n.section || '') === section && n.id !== dragId)
  const idx = sameSection.findIndex(n => n.id === nb.id)
  const position = t.zone === 'before' ? Math.max(0, idx) : idx + 1
  try {
    await http.post(`/api/notebooks/${dragId}/move`, { position, section })
    await loadNotebooks()
  } catch { ElMessage.error('移动失败') }
}
const onNbDragEnd = () => { nbDragId.value = null; nbDrop.value = null }

// ---------------- 数据库视图(子页面为数据行) ----------------
const STATUS_OPTIONS = ['待办', '进行中', '审核中', '完成', '']
const BOARD_COLUMNS = ['待办', '进行中', '审核中', '完成', '']
const STATUS_META: Record<string, { dot: string; bg: string }> = {
  待办: { dot: '#9b9a97', bg: '#f1f1ef' },
  进行中: { dot: '#dfab01', bg: '#fbf3db' },
  审核中: { dot: '#2383e2', bg: '#e7f1fb' },
  完成: { dot: '#0f7b6c', bg: '#e6f2e6' },
  '': { dot: '#c9c9c5', bg: '#f7f7f5' },
}
const isDatabase = computed(() => (currentPage.value?.view_type || 'doc') !== 'doc')
const dbChildren = computed(() =>
  treeRows.value.filter(r => (r.page.parent_id ?? null) === currentPage.value?.id).map(r => r.page)
)
const dbFilterStatus = ref('')
const dbSort = ref<'updated' | 'title'>('updated')
const dbChildrenFiltered = computed(() => {
  let arr = dbChildren.value.slice()
  if (dbFilterStatus.value) arr = arr.filter(c => (c.status || '') === dbFilterStatus.value)
  if (dbSort.value === 'title') arr.sort((a, b) => (a.title || '').localeCompare(b.title || '', 'zh'))
  else arr.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
  return arr
})
const calendar = computed(() => {
  const now = new Date()
  const year = now.getFullYear()
  const month = now.getMonth()
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const startDay = new Date(year, month, 1).getDay()
  const cells: { day: number; items: PageListItem[] }[] = []
  for (let d = 1; d <= daysInMonth; d++) {
    const items = dbChildrenFiltered.value.filter(c => {
      const dt = new Date(c.updated_at)
      return dt.getFullYear() === year && dt.getMonth() === month && dt.getDate() === d
    })
    cells.push({ day: d, items })
  }
  return { pad: Array.from({ length: startDay }, (_, i) => i), cells, label: `${year} 年 ${month + 1} 月` }
})
const setChildStatus = async (childId: string, status: string) => {
  try {
    await http.put(`/api/pages/${childId}`, { status })
    await loadTree()
  } catch { ElMessage.error('更新失败') }
}
const setViewType = async (vt: string) => {
  if (!currentPage.value) return
  try {
    await http.put(`/api/pages/${currentPage.value.id}/view`, { view_type: vt })
    currentPage.value.view_type = vt
  } catch { ElMessage.error('切换视图失败') }
}
const addRow = async () => { await createPage(currentPage.value?.id ?? null) }
const boardDragId = ref<string | null>(null)
const onBoardDrop = async (status: string, ev: DragEvent) => {
  ev.preventDefault()
  const id = boardDragId.value
  boardDragId.value = null
  if (!id) return
  await setChildStatus(id, status)
}

const reindexCurrentPage = async () => {
  if (!currentPage.value) return
  indexing.value = true
  try {
    await http.post(`/api/pages/${currentPage.value.id}/index`)
    ElMessage.success('索引完成')
  } catch {
    ElMessage.error('索引失败')
  } finally {
    indexing.value = false
  }
}

const getSourceTagType = (source: string) => {
  if (source.includes('reranker')) return 'danger'
  if (source.includes('graph')) return 'success'
  if (source.includes('keyword')) return 'warning'
  if (source.includes('vector')) return 'primary'
  return 'info'
}

const doSearch = async () => {
  const q = searchQuery.value.trim()
  if (!q) return
  // 按名称检索: 本地按标题过滤(不做语义检索)
  try {
    const res = await http.get('/api/pages', { params: { page: 1, page_size: 500 } })
    const lower = q.toLowerCase()
    searchResults.value = (res.data.items || [])
      .map((p: any) => ({ id: p.id, title: p.title || '无标题' }))
      .filter((p: { title: string }) => p.title.toLowerCase().includes(lower))
      .slice(0, 40)
    showSearch.value = true
  } catch (e) {
    ElMessage.error('搜索失败')
  }
}

const openFromSearch = async (result: any) => {
  showSearch.value = false
  currentPage.value = { id: result.id, notebook_id: null, title: result.title || '加载中...', content: '', icon: '', cover: '', updated_at: '' }
  pageLoading.value = true
  const seq = ++pageLoadSeq
  loadAbort?.abort()
  const controller = new AbortController()
  loadAbort = controller
  try {
    const res = await http.get(`/api/pages/${result.id}`, { signal: controller.signal })
    if (seq !== pageLoadSeq) return
    const page = res.data
    currentPage.value = page
    const nb = notebooks.value.find(n => n.id === page.notebook_id)
    if (nb && currentNotebook.value?.id !== nb.id) {
      currentNotebook.value = nb
      loadTree()
    }
  } catch (e: any) {
    if (seq !== pageLoadSeq) return
    ElMessage.error('打开笔记失败')
  } finally {
    if (seq === pageLoadSeq) {
      pageLoading.value = false
      loadAbort = null
    }
  }
}

const handleFileChange = (file: ElUploadFile) => {
  selectedFile.value = file.raw || null
}

const _executeSave = async (form: { action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; content: string }) => {
  const action = form.action || 'create_note'
  if (action === 'create_notebook' && form.new_notebook_name) {
    const nbRes = await http.post('/api/notebooks', { name: form.new_notebook_name })
    await http.post('/api/pages', { title: form.title, content: form.content, notebook_id: nbRes.data.id })
  } else if (action === 'update_note' && form.update_page_id) {
    await http.put(`/api/pages/${form.update_page_id}`, { content: form.content })
  } else {
    await http.post('/api/pages', { title: form.title, content: form.content, notebook_id: form.notebook_id || undefined })
  }
}

const handleImport = async () => {
  if (importTab.value === 'file' && !selectedFile.value) { ElMessage.warning('请选择文件'); return }
  if (importTab.value === 'url' && !importUrl.value.trim()) { ElMessage.warning('请输入URL'); return }
  if (importTab.value === 'text' && !importText.value.trim()) { ElMessage.warning('请输入文本'); return }
  importLoading.value = true
  try {
    let res
    if (importTab.value === 'file') {
      const formData = new FormData()
      formData.append('file', selectedFile.value!)
      res = await http.post('/api/chat/import/file', formData)
    } else if (importTab.value === 'url') {
      res = await http.post('/api/chat/import/url', { url: importUrl.value })
    } else {
      res = await http.post('/api/chat/import/text', { text: importText.value })
    }
    confirmForm.value = res.data
    confirmDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error('导入失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    importLoading.value = false
  }
}

const confirmImport = async () => {
  confirmLoading.value = true
  try {
    await _executeSave(confirmForm.value)
    ElMessage.success('知识已导入')
    confirmDialogVisible.value = false
    importDialogVisible.value = false
    selectedFile.value = null
    importUrl.value = ''
    importText.value = ''
    loadNotebooks()
  } catch (e: any) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    confirmLoading.value = false
  }
}

const handleOrganize = async () => {
  organizing.value = true
  try {
    const token = localStorage.getItem('rag_token')
    const resp = await fetch(`${API_BASE}/api/organize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    })
    if (!resp.ok) {
      const err = await resp.json()
      ElMessage.error(err.detail || '整理失败')
      return
    }
    const reader = resp.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const data = JSON.parse(line.slice(6))
          if (data.type === 'progress') ElMessage.info(data.message)
          else if (data.type === 'done') {
            const s = data.stats
            ElNotification({ title: '整理完成', message: `移动 ${s.moved} 篇 | 新建 ${s.created_notebooks} 个笔记本 | 更新 ${s.updated} 篇`, type: 'success' })
            loadNotebooks()
          } else if (data.type === 'error') ElMessage.error(data.content)
        } catch { /* skip */ }
      }
    }
  } catch (e: any) {
    ElMessage.error('整理失败: ' + e.message)
  } finally {
    organizing.value = false
  }
}

const handleKeydown = (e: KeyboardEvent) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault()
    if (currentPage.value && saveStatus.value !== 'saving') {
      savePage()
    }
  }
  if ((e.ctrlKey || e.metaKey) && e.key === '\\') {
    e.preventDefault()
    sidebarCollapsed.value = !sidebarCollapsed.value
  }
  if (e.key === 'Escape') {
    if (iconPickerVisible.value || coverPickerVisible.value) {
      iconPickerVisible.value = false
      coverPickerVisible.value = false
      return
    }
    if (commentsOpen.value) { commentsOpen.value = false; return }
    if (outlineOpen.value) { outlineOpen.value = false; return }
  }
}

// 点击空白处关闭图标/封面选择器
const onDocMousedown = (e: MouseEvent) => {
  const el = e.target as HTMLElement
  if (iconPickerVisible.value && !el.closest('.icon-picker') && !el.closest('.page-icon-btn')) {
    iconPickerVisible.value = false
  }
  if (coverPickerVisible.value && !el.closest('.cover-picker') && !el.closest('.cover-add') && !el.closest('.cover-actions')) {
    coverPickerVisible.value = false
  }
}

const selectTextInElement = (root: Element, needles: string[]): boolean => {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let node: Node | null
  while ((node = walker.nextNode())) {
    const text = node.textContent || ''
    for (const needle of needles) {
      const idx = text.indexOf(needle)
      if (idx >= 0) {
        const range = document.createRange()
        range.setStart(node, idx)
        range.setEnd(node, Math.min(idx + needle.length, text.length))
        const sel = window.getSelection()
        sel?.removeAllRanges()
        sel?.addRange(range)
        node.parentElement?.scrollIntoView({ behavior: 'smooth', block: 'center' })
        window.setTimeout(() => sel?.removeAllRanges(), 4000)
        return true
      }
    }
  }
  return false
}

const highlightCitation = () => {
  const m = (route.hash || '').match(/^#c(\d+)$/)
  if (!m) return
  let snippet = ''
  let pageId = ''
  try {
    const saved = JSON.parse(sessionStorage.getItem('cite-snippet') || 'null')
    if (saved && saved.text) {
      snippet = saved.text
      pageId = saved.pageId || ''
    }
  } catch { /* ignore */ }
  if (!snippet) return
  if (pageId && currentPage.value && currentPage.value.id !== pageId) return
  const needles = [
    snippet.slice(0, 40),
    snippet.replace(/[`#*_>]/g, '').replace(/\s+/g, ' ').slice(0, 40),
  ]
  let attempts = 0
  const tryHighlight = () => {
    attempts++
    const editorEl = document.querySelector('.ProseMirror')
    if (editorEl && selectTextInElement(editorEl, needles)) return
    if (attempts < 30) {
      window.setTimeout(tryHighlight, 200)
    }
  }
  tryHighlight()
}

onMounted(async () => {
  await loadNotebooks()
  await loadProfiles()
  await loadTags()
  loadRecent()
  loadFavorites()
  loadTemplates()
  auth.fetchMe().catch(() => {})
  // 协同编辑默认开启(见 collab 配置): 探活失败自动回退单人编辑; ?collab=0 可一键退回。
  window.addEventListener('keydown', handleKeydown)
  document.addEventListener('mousedown', onDocMousedown)
  window.addEventListener('beforeprint', prepareEditorForPrint)
  window.addEventListener('afterprint', finishEditorPrint)
  printMedia.addEventListener('change', onPrintMediaChange)
  const targetId = route.query.page as string | undefined
  if (targetId) {
    await openPageById(targetId)
  }
  highlightCitation()
})

onBeforeUnmount(() => {
  if (saveTimeout) {
    clearTimeout(saveTimeout)
    saveTimeout = null
  }
  if (saveRetryTimer) {
    clearTimeout(saveRetryTimer)
    saveRetryTimer = null
  }
  window.removeEventListener('keydown', handleKeydown)
  document.removeEventListener('mousedown', onDocMousedown)
  window.removeEventListener('beforeprint', prepareEditorForPrint)
  window.removeEventListener('afterprint', finishEditorPrint)
  printMedia.removeEventListener('change', onPrintMediaChange)
})
</script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; }
.app-container { height: 100vh; display: flex; flex-direction: column; background: #f0f2f5; }
.app-header {
  height: 56px;
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}
.search-box { width: 400px; }
.search-box :deep(.el-input__wrapper) {
  border-radius: 10px;
  background: #f8fafc;
  box-shadow: none;
  border: 1px solid #e2e8f0;
}
.header-actions { display: flex; align-items: center; gap: 10px; }
.app-body { flex: 1; display: flex; overflow: hidden; }
.sidebar {
  width: 280px;
  background: #fff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
}
.sidebar-header {
  padding: 16px 20px;
  font-weight: 600;
  font-size: 14px;
  color: #1e293b;
  border-bottom: 1px solid #e2e8f0;
  letter-spacing: -0.2px;
}
.notebook-list { flex: 1; overflow-y: auto; padding: 8px; }
.notebook-list::-webkit-scrollbar { width: 4px; }
.notebook-list::-webkit-scrollbar-thumb { background: #e2e8f0; border-radius: 2px; }
.notebook-item { margin-bottom: 2px; }
.notebook-info {
  display: flex;
  align-items: center;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.notebook-info:hover { background: #f1f5f9; }
.notebook-item.active > .notebook-info { background: #eff6ff; color: #2563eb; }
.notebook-icon { margin-right: 8px; font-size: 15px; }
.notebook-name { flex: 1; font-weight: 500; font-size: 14px; }
.notebook-info .el-button { margin-left: auto; }
.page-list { padding-left: 16px; }
.page-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}
.page-item:hover { background: #f1f5f9; }
.page-item.active { background: #eff6ff; }
.page-info { display: flex; align-items: center; flex: 1; min-width: 0; }
.page-ico { margin-right: 6px; font-size: 12px; }
.page-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #475569;
}
.page-item.active .page-title { color: #1e40af; font-weight: 400; }
.page-menu-btn { opacity: 0; padding: 0 4px; }
.page-item:hover .page-menu-btn { opacity: 1; }
.empty-tip { text-align: center; color: #94a3b8; padding: 30px 20px; font-size: 13px; }
.muted-hint { color: #909399; font-size: 12px; margin-top: 4px; line-height: 1.5; }
.main-content { flex: 1; padding: 24px 40px; overflow-y: auto; }
.editor-wrapper {
  max-width: 900px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  border: 1px solid #e2e8f0;
  min-height: calc(100vh - 104px);
  padding: 32px 44px;
}
.editor-wrapper { position: relative; }
.page-cover {
  position: relative;
  margin: -32px -44px 18px;
  height: 200px;
  overflow: hidden;
  border-radius: 12px 12px 0 0;
  background: #f1f5f9;
}
.page-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.cover-actions {
  position: absolute;
  right: 12px;
  bottom: 12px;
  display: flex;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.15s;
}
.page-cover:hover .cover-actions { opacity: 1; }
.title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-icon-btn {
  flex: 0 0 auto;
  width: 40px;
  height: 40px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  font-size: 28px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #cbd5e1;
}
.page-icon-btn:hover { background: #f1f5f9; }
.page-icon-add { font-size: 22px; color: #cbd5e1; }
.cover-add { margin-left: auto; color: #94a3b8; }
.icon-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px;
  margin-bottom: 12px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
}
.emoji-opt {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  border-radius: 7px;
  cursor: pointer;
}
.emoji-opt:hover { background: #f1f5f9; }
.emoji-clear { width: auto; padding: 0 10px; font-size: 12px; color: #94a3b8; }
.title-input {
  flex: 1 1 auto;
  width: 100%;
  font-size: 26px;
  font-weight: 700;
  border: none;
  outline: none;
  padding: 8px 0;
  margin-bottom: 20px;
  color: #0f172a;
  letter-spacing: -0.3px;
}
.title-input::placeholder { color: #cbd5e1; }
.editor-footer {
  margin-top: 28px;
  padding-top: 16px;
  border-top: 1px solid #f1f5f9;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.editor-hint { color: #94a3b8; font-size: 13px; }
.empty-state { text-align: center; color: #94a3b8; margin-top: 120px; }
.empty-state h2 { font-size: 20px; color: #475569; margin-bottom: 8px; }
.search-result {
  padding: 16px;
  border-bottom: 1px solid #f1f5f9;
  cursor: pointer;
  border-radius: 8px;
  transition: background 0.15s;
}
.search-result:hover { background: #f8fafc; }
.result-title { font-weight: 600; margin-bottom: 6px; color: #1e293b; }
.result-content { color: #64748b; font-size: 13px; margin-bottom: 8px; line-height: 1.5; }
.result-chunks { margin-bottom: 8px; display: flex; flex-direction: column; gap: 4px; }
.result-chunk {
  font-size: 12px;
  color: #64748b;
  background: #f8fafc;
  border-radius: 6px;
  padding: 6px 10px;
  line-height: 1.5;
  max-height: 72px;
  overflow: hidden;
  word-break: break-word;
}
.result-chunk-ctx { display: block; color: #2563eb; margin-bottom: 2px; }
.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.result-footer { margin-top: 6px; }
.save-summary { font-size: 13px; color: #475569; background: #f8fafc; padding: 10px 14px; border-radius: 8px; line-height: 1.5; }
.upload-tip { font-size: 12px; color: #94a3b8; margin-top: 4px; }
.dt-doc-list { max-height: 400px; overflow-y: auto; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px; }
.dt-doc-item { padding: 6px 8px; border-radius: 4px; transition: background 0.15s; }
.dt-doc-item:hover { background: #f8fafc; }
.dt-doc-title { font-size: 13px; font-weight: 500; color: #1e293b; }
.dt-doc-path { font-size: 12px; color: #94a3b8; margin-left: 8px; }
.editor-body { position: relative; min-height: 400px; }
.tag-filter {
  padding: 0 16px 8px;
  border-bottom: 1px solid #f1f5f9;
}
.tag-pages {
  margin-bottom: 6px;
}
.tag-pages-header {
  font-size: 12px;
  font-weight: 600;
  color: #2563eb;
  padding: 6px 12px;
}

/* ---------- 视觉打磨 ---------- */
.app-header {
  height: 58px;
  background: rgba(255, 255, 255, 0.85);
  backdrop-filter: saturate(180%) blur(10px);
  border-bottom: 1px solid var(--border, #e6e8f0);
  padding: 0 20px;
  position: relative;
  z-index: 10;
}
.search-box { width: 100%; max-width: 440px; }
.search-box :deep(.el-input__wrapper) {
  border-radius: 10px;
  background: var(--surface-2, #f2f4f9);
  box-shadow: none;
  border: 1px solid transparent;
  transition: border-color 0.15s, background 0.15s;
}
.search-box :deep(.el-input__wrapper.is-focus) {
  background: #fff;
  border-color: var(--primary, #4f46e5);
}
.search-box :deep(.el-input-group__append) {
  background: transparent;
  box-shadow: none;
}
.header-actions { gap: 8px; }
.header-actions :deep(.el-button) { height: 34px; }
.header-actions :deep(.el-tag) { height: 24px; }
.sidebar {
  width: 288px;
  background: var(--surface, #fff);
  border-right: 1px solid var(--border, #e6e8f0);
}
.sidebar-header {
  padding: 16px 18px 10px;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-3, #8b93a4);
  border-bottom: none;
}
.tag-filter { padding: 0 14px 10px; border-bottom: 1px solid var(--border, #e6e8f0); }
.notebook-list { padding: 10px; }
.notebook-info {
  padding: 8px 10px;
  border-radius: 9px;
  transition: background 0.15s, color 0.15s;
}
.notebook-info:hover { background: var(--surface-2, #f2f4f9); }
.notebook-item.active > .notebook-info {
  background: var(--primary-weak, #eef0ff);
  color: var(--primary, #4f46e5);
}
.notebook-name { font-weight: 600; font-size: 13.5px; }
.page-list { padding-left: 14px; margin-left: 6px; border-left: 1px solid var(--border, #e6e8f0); }
.page-item {
  padding: 6px 10px;
  border-radius: 8px;
  margin: 1px 0;
}
.page-item:hover { background: var(--surface-2, #f2f4f9); }
.page-item.active { background: var(--primary-weak, #eef0ff); }
.page-item.active .page-title { color: var(--primary, #4f46e5); font-weight: 400; }
.page-title { font-size: 13px; color: var(--text-2, #59616f); }
/* ================= Notion 风格 ================= */
.app-container { background: #fff; }
.app-body { height: 100%; }

/* 侧边栏 */
.sidebar {
  width: 260px;
  flex: 0 0 auto;
  background: #f7f7f5;
  border-right: 1px solid #ececea;
  transition: width 0.18s ease, opacity 0.18s ease;
}
.app-container.sidebar-collapsed .sidebar {
  width: 0;
  opacity: 0;
  border-right: none;
  overflow: hidden;
}
.icon-btn {
  border: none;
  background: transparent;
  color: #9b9a97;
  cursor: pointer;
  border-radius: 6px;
  min-width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  transition: background 0.12s, color 0.12s;
}
.icon-btn:hover { background: #ebebe9; color: #37352f; }
.icon-btn.expand { margin-right: 4px; }
.side-search { padding: 12px 10px 8px; }
.side-search :deep(.el-input__wrapper) {
  background: #fff;
  border: 1px solid #e9e9e7;
  box-shadow: none;
  border-radius: 6px;
}
.side-search :deep(.el-input__wrapper.is-focus) { border-color: #d3d3d0; background: #fff; }
.side-search :deep(.el-input__inner) { color: #37352f; }
.side-section {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px 2px 12px;
}
.side-section-label { font-size: 12px; font-weight: 600; color: #9b9a97; }
.tag-filter { padding: 0 10px 8px; border-bottom: none; }
.tag-filter :deep(.el-select__wrapper) { background: #fff; box-shadow: none; border: 1px solid #e9e9e7; }
.notebook-list { padding: 2px 8px 12px; }
.notebook-list::-webkit-scrollbar-thumb { background: #dededb; }
.notebook-item { margin-bottom: 1px; }
.notebook-info { padding: 6px 8px; border-radius: 6px; gap: 0; }
.notebook-info:hover { background: #ebebe9; }
.notebook-item.active > .notebook-info { background: #e8e8e6; color: #37352f; }
.notebook-info .el-button { color: #b9b9b6; }
.nb-chevron {
  width: 14px;
  color: #b9b9b6;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.15s;
}
.nb-chevron.open { transform: rotate(90deg); }
.notebook-icon { font-size: 14px; margin: 0 7px 0 3px; }
.notebook-name { font-weight: 500; font-size: 14px; color: #37352f; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.page-list { padding-left: 0; margin-left: 18px; border-left: 1px solid #e9e9e7; }
.page-item { padding: 5px 8px; border-radius: 6px; margin: 0; }
.page-item:hover { background: #ebebe9; }
.page-item.active { background: #e8e8e6; }
.page-info { gap: 0; }
.page-ico { margin: 0 6px 0 3px; font-size: 12px; line-height: 1.1; flex: none; }
.page-title { font-size: 14px; color: #37352f; font-weight: 400; }
.page-item.active .page-title { color: #37352f; font-weight: 400; }
.load-more {
  padding: 5px 8px 5px 22px;
  margin: 1px 0;
  color: #787774;
  font-size: 12.5px;
  border-radius: 6px;
  cursor: pointer;
}
.load-more:hover { background: #ebebe9; color: #37352f; }
.page-menu-btn { color: #b9b9b6; }
.empty-tip { color: #9b9a97; padding: 24px 16px; }

/* 主区 */
.main-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  padding: 0;
  overflow: hidden;
}
.doc-topbar {
  height: 45px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  position: relative;
  z-index: 5;
}
.doc-tab {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  max-width: 360px;
  padding: 4px 10px;
  border-radius: 7px;
  background: var(--surface-2);
  font-size: 13px;
  color: var(--text);
  flex: 0 1 auto;
}
.doc-tab-empty { color: var(--text-3); }
.doc-tab-icon { flex: 0 0 auto; font-size: 14px; }
.doc-tab-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
.crumb { display: flex; align-items: center; gap: 7px; font-size: 13px; min-width: 0; flex: 1; }
.crumb-nb { color: #37352f; font-weight: 500; white-space: nowrap; }
.crumb-sep { color: #d3d3d0; }
.crumb-page { color: #9b9a97; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.topbar-actions { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; margin-left: auto; }
.topbar-actions :deep(.el-button) { height: 30px; }
.topbar-text-btn {
  border: none;
  background: transparent;
  color: var(--text-2);
  font-size: 13px;
  padding: 5px 9px;
  border-radius: 6px;
  cursor: pointer;
}
.topbar-text-btn:hover { background: var(--surface-2); color: var(--text); }
.save-badge { font-size: 12px; color: #9b9a97; }
.save-badge.saving { color: #f59e0b; }
.save-badge.unsaved { color: #ef4444; }
.doc-scroll { flex: 1; overflow-y: auto; background: #fff; }
.doc-scroll::-webkit-scrollbar-thumb { background: #e0e0de; }

/* 页面主体(无卡片,全宽白纸) */
.editor-wrapper {
  max-width: 900px;
  margin: 0 auto;
  background: transparent;
  border: none;
  box-shadow: none;
  border-radius: 0;
  min-height: auto;
  padding: 54px 96px 180px;
  position: relative;
}
.page-cover {
  margin: -54px -96px 22px;
  height: 210px;
  border-radius: 0;
}
.title-row { gap: 6px; align-items: flex-start; }
.page-icon-btn {
  width: auto;
  height: auto;
  padding: 2px 4px;
  margin-top: 4px;
  border-radius: 8px;
  font-size: 42px;
  line-height: 1;
}
.page-icon { font-size: 42px; }
.page-icon-add { font-size: 24px; color: #d3d3d0; }
.title-input {
  font-size: 40px;
  font-weight: 700;
  color: #37352f;
  letter-spacing: -0.02em;
  padding: 2px 0 6px;
  margin-bottom: 6px;
  line-height: 1.2;
}
.title-input::placeholder { color: #d8d8d5; }
.cover-add { color: #b9b9b6; }
.editor-body { min-height: 300px; }
.editor-footer {
  margin-top: 20px;
  padding-top: 12px;
  border-top: 1px solid #f2f2f0;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.editor-hint { color: #b9b9b6; font-size: 12px; }
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #9b9a97;
}
.empty-emoji { font-size: 52px; margin-bottom: 14px; opacity: 0.85; }
.empty-state h2 { font-size: 20px; font-weight: 600; color: #37352f; margin-bottom: 6px; }
.empty-state p { font-size: 14px; color: #9b9a97; }

/* 页面树拖拽反馈 */
.page-item { position: relative; cursor: pointer; }
.page-item .page-info { cursor: pointer; }
.page-item.dragging { opacity: 0.45; }
.page-item.drop-before { box-shadow: inset 0 2px 0 var(--primary, #4f46e5); }
.page-item.drop-after { box-shadow: inset 0 -2px 0 var(--primary, #4f46e5); }
.page-item.drop-inside {
  background: var(--primary-weak, #eef0ff);
  outline: 1px dashed var(--primary, #4f46e5);
  outline-offset: -2px;
}

/* 封面渐变预设 */
.page-cover.is-gradient img { display: none; }
.cover-picker {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  margin: 4px 0 14px;
  background: #fff;
  border: 1px solid #ececea;
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
}
.cover-upload {
  border: 1px solid #e6e6e3;
  background: #f7f7f5;
  color: #37352f;
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 13px;
  cursor: pointer;
}
.cover-upload:hover { background: #efefed; }
.cover-swatch {
  width: 44px;
  height: 30px;
  border-radius: 7px;
  border: 2px solid transparent;
  cursor: pointer;
  padding: 0;
  transition: transform 0.12s, border-color 0.12s;
}
.cover-swatch:hover { transform: translateY(-1px); border-color: #d3d3d0; }

/* emoji 选择器 */
.icon-picker {
  display: block;
  padding: 12px;
  margin: 4px 0 14px;
  background: #fff;
  border: 1px solid #ececea;
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.1);
}
.emoji-group { margin-bottom: 8px; }
.emoji-group:last-of-type { margin-bottom: 4px; }
.emoji-group-label { font-size: 11px; color: #9b9a97; margin: 2px 2px 6px; font-weight: 600; }
.emoji-grid { display: flex; flex-wrap: wrap; gap: 4px; }
.emoji-opt {
  width: 34px;
  height: 34px;
  font-size: 20px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.12s, transform 0.12s, border-color 0.12s;
}
.emoji-opt:hover { background: #f1f1ef; transform: translateY(-1px); }
.emoji-opt.active { background: var(--primary-weak); border-color: var(--primary-weak-2); }
.emoji-foot { display: flex; gap: 8px; margin-top: 8px; padding-top: 8px; border-top: 1px solid #f2f2f0; }
.emoji-foot-btn {
  flex: 1;
  border: 1px solid #ececea;
  background: #f7f7f5;
  color: #37352f;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
  cursor: pointer;
}
.emoji-foot-btn:hover { background: #efefed; }

/* 笔记本图标选择 */
.nb-icon-picker { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.nb-icon-opt {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}
.nb-icon-opt:hover { background: var(--surface-2); }
.nb-icon-opt.active { background: var(--primary-weak); border-color: var(--primary-weak-2); }

/* 侧边栏拖拽调宽 + 页面树折叠 */
.sidebar { position: relative; }
.sidebar-resizer {
  position: absolute;
  top: 0;
  right: -3px;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 6;
}
.sidebar-resizer::after {
  content: '';
  position: absolute;
  top: 0;
  left: 2px;
  width: 2px;
  height: 100%;
  background: transparent;
  transition: background 0.15s;
}
.sidebar-resizer:hover::after { background: var(--primary, #4f46e5); opacity: 0.45; }
.page-chevron {
  width: 14px;
  height: 14px;
  margin-right: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #b9b9b6;
  font-size: 12px;
  border-radius: 4px;
  transition: transform 0.15s, background 0.12s, color 0.12s;
  flex: 0 0 auto;
}
.page-chevron.open { transform: rotate(90deg); }
.page-chevron:not(.placeholder):hover { background: #e3e3e0; color: #37352f; }
.page-chevron.placeholder { visibility: hidden; }

.fav-list { padding: 0 8px 6px; }
.page-item.fav { padding: 5px 8px; }
.fav-star { color: #f59e0b; margin-right: 7px; font-size: 12px; }
.side-foot { border-top: 1px solid #ececea; padding: 6px 8px; }
.side-foot-btn {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  border: none;
  background: transparent;
  color: #9b9a97;
  font-size: 13px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.side-foot-btn:hover { background: #ebebe9; color: #37352f; }
.side-foot-btn .count { background: #e3e3e0; color: #6b6b68; border-radius: 8px; padding: 0 7px; font-size: 11px; }
.trash-list { max-height: 200px; overflow-y: auto; padding: 0 8px 8px; }
.trash-item { display: flex; align-items: center; gap: 2px; padding: 4px 6px; border-radius: 6px; }
.trash-item:hover { background: #ebebe9; }
.trash-item .page-title { flex: 1; font-size: 13px; color: #6b6b68; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 大纲 */
.outline-panel {
  width: 224px;
  flex: 0 0 auto;
  border-left: 1px solid #f0f0ef;
  background: #fff;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.outline-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 8px;
  font-size: 12px;
  font-weight: 600;
  color: #9b9a97;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.outline-body { flex: 1; overflow-y: auto; padding-bottom: 16px; }
.outline-item {
  font-size: 13px;
  color: #59616f;
  padding: 4px 12px;
  cursor: pointer;
  border-radius: 6px;
  margin: 0 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.outline-item:hover { background: #f1f1ef; color: #37352f; }
.icon-btn.is-on { background: var(--primary-weak); color: var(--primary); }

/* 全宽 / 小字号(按页面) */
.editor-wrapper.wide { max-width: 100%; padding-left: 64px; padding-right: 64px; }
.editor-wrapper.wide .page-cover { margin-left: -64px; margin-right: -64px; }
.editor-wrapper.small .editor-content .ProseMirror { font-size: 15px; }

/* 反向链接 */
.link-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px 6px;
  font-size: 12px;
  font-weight: 600;
  color: #9b9a97;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-top: 1px solid #f0f0ef;
}
.link-panel-head .count { background: #e3e3e0; color: #6b6b68; border-radius: 8px; padding: 0 7px; font-size: 11px; }
.link-list { max-height: 220px; overflow-y: auto; padding-bottom: 14px; }

/* 评论 */
.icon-btn { position: relative; }
.icon-btn .badge {
  position: absolute;
  top: -5px;
  right: -5px;
  background: var(--primary);
  color: #fff;
  border-radius: 8px;
  font-size: 10px;
  line-height: 1;
  padding: 1px 5px;
}
.comments-panel {
  width: 304px;
  flex: 0 0 auto;
  border-left: 1px solid #f0f0ef;
  background: #fff;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.comments-body { flex: 1; overflow-y: auto; padding: 4px 10px 12px; }
.comment-item {
  padding: 8px 10px;
  border-radius: 10px;
  border: 1px solid #f0f0ef;
  margin-bottom: 8px;
  background: #fff;
}
.comment-item.resolved { opacity: 0.55; }
.comment-head { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.comment-author { font-size: 13px; font-weight: 600; color: #37352f; }
.comment-time { font-size: 11px; color: #b9b9b6; flex: 1; }
.comment-content { font-size: 13px; color: #4b5563; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.comment-input { border-top: 1px solid #f0f0ef; padding: 10px; }

/* 分享 */
.share-row { display: flex; gap: 8px; }

/* 版本历史 */
.history-wrap { display: flex; gap: 14px; height: 520px; }
.history-list { width: 220px; flex: 0 0 auto; overflow-y: auto; border-right: 1px solid #f0f0ef; padding-right: 8px; }
.history-item { padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px; }
.history-item:hover { background: #f1f1ef; }
.history-item.active { background: var(--primary-weak); }
.history-time { font-size: 13px; color: #37352f; font-weight: 500; }
.history-meta { font-size: 11px; color: #9b9a97; margin-top: 2px; }
.history-preview { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.history-preview-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.history-preview-title { font-weight: 600; color: #37352f; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-head-actions { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.history-diff-stat { display: flex; align-items: center; gap: 10px; font-size: 12px; color: #64748b; margin-bottom: 8px; }
.history-diff-stat .stat-add { color: #16a34a; font-weight: 600; }
.history-diff-stat .stat-del { color: #dc2626; font-weight: 600; }
.history-diff-stat .stat-equal { color: #94a3b8; }
.history-diff-body {
  flex: 1;
  overflow: auto;
  border: 1px solid #eceef2;
  border-radius: 10px;
  background: #fff;
  padding: 6px 0;
  font-family: ui-monospace, SFMono-Regular, Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.65;
}
.diff-line { display: flex; gap: 8px; padding: 0 10px; white-space: pre-wrap; word-break: break-word; }
.diff-line .diff-sign { flex: 0 0 10px; color: #94a3b8; user-select: none; }
.diff-line .diff-text { flex: 1; min-width: 0; }
.diff-add { background: #ecfdf3; color: #166534; }
.diff-del { background: #fef2f2; color: #991b1b; }
.diff-equal { color: #475569; }
.history-content {
  flex: 1;
  overflow: auto;
  background: #f8f9fc;
  border: 1px solid #eceef2;
  border-radius: 10px;
  padding: 12px;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  color: #374151;
  margin: 0;
}

/* 封面拖动 */
.cover-img { cursor: grab; }
.cover-img:active { cursor: grabbing; }

/* 模板 */
.tpl-list { padding: 0 8px 6px; }
.page-item.tpl { padding: 5px 8px; }
.tpl-icon { margin-right: 7px; font-size: 13px; }
.page-item.tpl .icon-btn { opacity: 0; }
.page-item.tpl:hover .icon-btn { opacity: 1; }
.tpl-owner {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 8px;
  background: #f1f5f9;
  color: #64748b;
  font-size: 11px;
  flex: none;
}

/* 笔记本 zip 导出进度 */
.zip-progress {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 3000;
  min-width: 280px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.16);
  padding: 14px 16px;
}
.zip-progress-title { font-size: 13px; color: #1e293b; margin-bottom: 8px; }
.zip-progress-bar { height: 6px; background: #f1f5f9; border-radius: 4px; overflow: hidden; }
.zip-progress-fill { height: 100%; background: #4f46e5; transition: width 0.2s; }
.zip-progress-text { margin-top: 6px; font-size: 12px; color: #64748b; text-align: right; }

/* 笔记本拖拽/分组 */
.side-section.nb-group { padding-top: 10px; color: #59616f; font-weight: 700; }
.notebook-item.dragging { opacity: 0.5; }
.notebook-item.nb-drop-before > .notebook-info { box-shadow: inset 0 2px 0 var(--primary, #4f46e5); }
.notebook-item.nb-drop-after > .notebook-info { box-shadow: inset 0 -2px 0 var(--primary, #4f46e5); }

/* 数据库视图 */
.db-view { max-width: 1180px; margin: 0 auto; padding: 44px 56px 140px; }
.db-hero { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.db-hero-icon { font-size: 40px; line-height: 1; }
.db-hero-title { font-size: 40px; font-weight: 700; color: var(--text); letter-spacing: -0.02em; margin: 0; }
.db-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.db-tabs { display: flex; gap: 2px; }
.db-tab {
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: 14px;
  font-weight: 500;
  padding: 7px 10px;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  cursor: pointer;
}
.db-tab:hover { color: var(--text-2); }
.db-tab.active { color: var(--text); border-bottom-color: var(--text); }
.db-toolbar-right { display: flex; align-items: center; gap: 8px; }
.db-table { border: 1px solid #eceef2; border-radius: 12px; overflow: hidden; }
.db-row {
  display: grid;
  grid-template-columns: 1fr 150px 180px;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid #f2f2f0;
}
.db-row:last-child { border-bottom: none; }
.db-row-head { background: #f8f9fc; font-size: 12px; color: #9b9a97; font-weight: 600; }
.db-link { color: #37352f; cursor: pointer; }
.db-link:hover { color: var(--primary); text-decoration: underline; }
.db-c-time { font-size: 12px; color: #9b9a97; }
.db-board { display: flex; gap: 12px; align-items: flex-start; overflow-x: auto; padding-bottom: 8px; }
.db-col { flex: 1 1 0; min-width: 244px; border-radius: 10px; padding: 8px; }
.db-col-head { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 8px; padding: 2px 4px; }
.db-col-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
.db-col-name { flex: 0 0 auto; }
.db-col-count { color: var(--text-3); font-weight: 500; }
.db-col-body { min-height: 20px; display: flex; flex-direction: column; gap: 6px; }
.db-card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font-size: 14px; color: var(--text); cursor: pointer; box-shadow: var(--shadow-sm); transition: box-shadow 0.12s, border-color 0.12s; }
.db-card:hover { box-shadow: var(--shadow); }
.db-col-add { color: var(--text-3); font-size: 13px; padding: 6px 8px; border-radius: 6px; cursor: pointer; }
.db-col-add:hover { background: rgba(55, 53, 47, 0.06); color: var(--text-2); }
.db-cal-head { font-size: 15px; font-weight: 600; color: #37352f; margin-bottom: 10px; }
.db-cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
.db-cal-dow { text-align: center; font-size: 12px; color: #9b9a97; padding: 4px 0; }
.db-cal-cell { min-height: 78px; border: 1px solid #f0f0ef; border-radius: 8px; padding: 4px 6px; font-size: 11px; overflow: hidden; }
.db-cal-cell.empty { border: none; }
.db-cal-day { color: #b9b9b6; margin-bottom: 2px; }
.db-cal-item { background: var(--primary-weak); color: var(--primary); border-radius: 5px; padding: 1px 5px; margin-bottom: 2px; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---- 交互细节打磨 ---- */
.save-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #94a3b8;
  margin: 0 6px 0 2px;
  white-space: nowrap;
}
.save-state.error { color: #dc2626; }
.save-state.clickable { cursor: pointer; }
.save-state.clickable:hover { text-decoration: underline; }
.save-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d3d3d0;
  flex: 0 0 auto;
}
.save-dot.saved { background: #10b981; }
.save-dot.saving { background: #f59e0b; animation: dotPulse 1s infinite; }
.save-dot.unsaved { background: #ef4444; }
.save-dot.error { background: #ef4444; }
@keyframes dotPulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

.collab-peers { display: inline-flex; align-items: center; padding-left: 6px; }
.collab-peers.is-offline { opacity: 0.5; }
.collab-avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  color: #fff;
  font-size: 11px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 2px solid #fff;
  margin-left: -7px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.15);
}

@keyframes panelIn {
  from { opacity: 0; transform: translateX(8px); }
  to { opacity: 1; transform: none; }
}
.outline-panel, .comments-panel { animation: panelIn 0.18s ease; }

.db-row:not(.db-row-head):hover { background: #fafafe; }
.db-card { transition: box-shadow 0.15s, border-color 0.15s, transform 0.15s; }
.db-card:hover { box-shadow: 0 6px 16px rgba(16, 24, 40, 0.08); transform: translateY(-1px); }
.db-col.drag-over { outline: 2px dashed var(--primary-weak-2); }

/* 分区标题 hover 显示新建 */
.side-section { border-radius: 6px; }
.side-section:hover { background: #efefed; }

/* 统一过渡 */
.tb-btn, .icon-btn, .nav-link, .page-item, .notebook-info { transition: background 0.14s, color 0.14s; }

/* 浏览器直接打印编辑页(Ctrl+P): 隐藏侧栏/工具条/面板, 标题不孤行, 表格/代码块不跨页 */
@media print {
  .sidebar, .sidebar-resizer, .doc-topbar, .outline-panel, .comments-panel,
  .editor-toolbar, .editor-footer, .cover-actions, .zip-progress,
  .icon-picker, .cover-picker, .page-icon-btn, .cover-add, .find-bar,
  .block-handle, .table-toolbar, .bubble-menu { display: none !important; }
  .main-content, .doc-scroll { overflow: visible !important; height: auto !important; padding: 0 !important; }
  .editor-wrapper { max-width: none !important; margin: 0 !important; padding: 0 !important; }
  .editor-wrapper .ProseMirror { padding: 0 !important; }
  h1, h2, h3, h4, h5, h6 { break-after: avoid; page-break-after: avoid; }
  table, pre, blockquote, img { break-inside: avoid; page-break-inside: avoid; }
  /* 折叠的代码块/表格在打印时全部展开 */
  .code-block.is-collapsed pre.is-collapsed-pre { max-height: none !important; overflow: visible !important; }
  table.table-folded tr { display: table-row !important; }
  /* 长文渲染优化兜底: content-visibility 跳渲的块打印时强制完整渲染(否则打印稿可能缺内容) */
  .ProseMirror .cv-auto { content-visibility: visible !important; contain-intrinsic-size: none !important; }
  /* 展开后折叠按钮不再有意义, 打印时隐藏(内容已由上方规则全部展开) */
  .table-fold-toggle, .code-fold { display: none !important; }
}
</style>
