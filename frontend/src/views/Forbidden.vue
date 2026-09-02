<template>
  <div class="error-page">
    <div class="error-code">403</div>
    <div class="error-title">无权访问</div>
    <div class="error-desc">你没有权限访问此页面。如认为这是错误，请联系知识管理员。</div>
    <el-button type="primary" @click="goBack">返回</el-button>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
function goBack() {
  router.back()
}
</script>

<style scoped>
.error-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #64748b;
}
.error-code { font-size: 72px; font-weight: 700; color: #e2e8f0; }
.error-title { font-size: 20px; font-weight: 600; color: #374151; }
.error-desc { font-size: 14px; max-width: 420px; text-align: center; }
</style>
