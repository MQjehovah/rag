<template>
  <div class="error-page">
    <div class="error-code">⚙️</div>
    <div class="error-title">功能未启用</div>
    <div class="error-desc">该功能当前未开启，请联系管理员通过 Feature Flag 启用。</div>
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
.error-code { font-size: 72px; }
.error-title { font-size: 20px; font-weight: 600; color: #374151; }
.error-desc { font-size: 14px; }
</style>
