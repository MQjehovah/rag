<template>
  <div class="section-dispatch">
    <!-- Phase 8B：仅 endpoint role + 合法 display 时结构化展示；其余（历史/未知/非法
         /普通章节）一律回退 Markdown 原文，且结构化有效时不再重复渲染整段 Markdown。 -->
    <ApiEndpointSection
      v-if="section.section_role === 'endpoint' && section.display"
      :section-id="section.id"
      :display="section.display"
    />
    <MarkdownPreview v-else :content="section.content" />
  </div>
</template>

<script setup lang="ts">
import type { WikiSection } from '../../api/wiki'
import MarkdownPreview from '../MarkdownPreview.vue'
import ApiEndpointSection from './ApiEndpointSection.vue'

defineProps<{
  section: WikiSection
}>()
</script>
