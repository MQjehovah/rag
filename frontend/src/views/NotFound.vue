<template>
  <div class="error-page">
    <div class="error-code">404</div>
    <div class="error-title">页面不存在</div>
    <div class="error-desc">你访问的页面不存在或已被移除。</div>
    <el-button type="primary" @click="goHome">回到首页</el-button>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
function goHome() {
  router.push('/assistant')
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
.error-desc { font-size: 14px; }
</style>
