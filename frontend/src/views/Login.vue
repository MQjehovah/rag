<template>
  <div class="login-page">
    <div class="login-bg"></div>
    <div class="login-card">
      <div class="login-logo">
        <div class="logo-dot"></div>
        <h1>Notes RAG</h1>
        <p class="login-subtitle">企业智能知识库</p>
      </div>
      <el-form @submit.prevent="handleLogin" class="login-form">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" size="large" prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" size="large" prefix-icon="Lock" show-password @keyup.enter="handleLogin" />
        </el-form-item>
        <el-button type="primary" size="large" class="login-btn" @click="handleLogin" :loading="loading">登 录</el-button>
      </el-form>
      <p v-if="error" class="error-text">{{ error }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

const router = useRouter()
const authStore = useAuthStore()

const handleLogin = async () => {
  if (!username.value || !password.value) {
    error.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    await authStore.login(username.value, password.value)
    router.push('/')
  } catch (e: any) {
    error.value = e.response?.data?.detail || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0f172a;
  position: relative;
  overflow: hidden;
}
.login-bg {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 80% 60% at 50% 0%, rgba(56,189,248,0.15), transparent),
    radial-gradient(ellipse 60% 40% at 80% 80%, rgba(99,102,241,0.1), transparent);
}
.login-card {
  width: 420px;
  padding: 48px 40px;
  background: rgba(30, 41, 59, 0.8);
  backdrop-filter: blur(20px);
  border-radius: 20px;
  border: 1px solid rgba(255,255,255,0.08);
  box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  position: relative;
  z-index: 1;
}
.login-logo {
  text-align: center;
  margin-bottom: 36px;
}
.logo-dot {
  width: 12px;
  height: 12px;
  background: #38bdf8;
  border-radius: 50%;
  margin: 0 auto 16px;
  box-shadow: 0 0 20px rgba(56,189,248,0.4);
}
.login-logo h1 {
  color: #f1f5f9;
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.5px;
}
.login-subtitle {
  color: #64748b;
  font-size: 14px;
  margin-top: 6px;
}
.login-form :deep(.el-input__wrapper) {
  background: rgba(15, 23, 42, 0.6);
  border: 1px solid rgba(255,255,255,0.08);
  box-shadow: none;
  border-radius: 10px;
}
.login-form :deep(.el-input__wrapper:hover),
.login-form :deep(.el-input__wrapper.is-focus) {
  border-color: rgba(56,189,248,0.4);
}
.login-form :deep(.el-input__inner) {
  color: #e2e8f0;
}
.login-form :deep(.el-input__inner::placeholder) {
  color: #475569;
}
.login-btn {
  width: 100%;
  height: 44px;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 600;
  background: linear-gradient(135deg, #38bdf8, #6366f1);
  border: none;
  letter-spacing: 4px;
}
.login-btn:hover {
  opacity: 0.9;
}
.error-text {
  color: #f87171;
  text-align: center;
  margin-top: 16px;
  font-size: 13px;
}
</style>
