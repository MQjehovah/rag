<template>
  <div class="login-page">
    <div class="login-bg"></div>
    <div class="login-card">
      <div class="login-logo">
        <img class="login-logo-img" :src="logoUrl" alt="Rosiwit" />
        <h1>企业知识库</h1>
        <p class="login-subtitle">RAG · 企业智能知识库</p>
      </div>
      <el-form @submit.prevent="handleLogin" class="login-form">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" size="large" prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" size="large" prefix-icon="Lock" show-password @keyup.enter="handleLogin" />
        </el-form-item>
        <el-button type="primary" size="large" class="login-btn" @click="handleLogin" :loading="loading">登 录</el-button>
        <el-button size="large" class="login-btn sso-btn" @click="handleSsoLogin" :loading="ssoLoading">企业 SSO 登录</el-button>
      </el-form>
      <p v-if="error" class="error-text">{{ error }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import logoUrl from '../assets/logo.svg'

const username = ref('')
const password = ref('')
const loading = ref(false)
const ssoLoading = ref(false)
const error = ref('')

const route = useRoute()
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

const handleSsoLogin = () => {
  ssoLoading.value = true
  error.value = ''
  authStore.loginWithSso()
}

/** SSO 回调: 带 sso_token 回本页 → 落盘后进入主页; 带 error 则原样展示 */
onMounted(async () => {
  const ssoError = route.query.error
  if (typeof ssoError === 'string' && ssoError) error.value = ssoError
  const ssoToken = route.query.sso_token
  if (typeof ssoToken !== 'string' || !ssoToken) return
  loading.value = true
  try {
    await authStore.adoptSsoToken(ssoToken)
    router.replace('/')
  } catch {
    error.value = 'SSO 登录失败,请重试'
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
  position: relative;
  overflow: hidden;
}
.login-bg { display: none; }
.login-card {
  width: 400px;
  padding: 40px 36px;
  background: var(--surface);
  border-radius: 12px;
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
}
.login-logo {
  text-align: center;
  margin-bottom: 28px;
}
.login-logo-img {
  width: 48px;
  height: 48px;
  display: block;
  object-fit: contain;
  border-radius: 10px;
  margin: 0 auto 14px;
}
.login-logo h1 {
  color: var(--text);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.3px;
}
.login-subtitle {
  color: var(--text-3);
  font-size: 13px;
  margin-top: 4px;
}
.login-form :deep(.el-input__wrapper) {
  background: var(--surface);
  border: 1px solid var(--border);
  box-shadow: none;
  border-radius: 8px;
}
.login-form :deep(.el-input__wrapper:hover),
.login-form :deep(.el-input__wrapper.is-focus) {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-weak);
}
.login-form :deep(.el-input__inner) {
  color: var(--text);
}
.login-form :deep(.el-input__inner::placeholder) {
  color: var(--text-3);
}
.login-btn {
  width: 100%;
  height: 42px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
}
.sso-btn {
  margin-top: 10px;
  margin-left: 0;
  width: 100%;
  height: 42px;
  border-radius: 8px;
  font-size: 14px;
}
.error-text {
  color: var(--danger);
  text-align: center;
  margin-top: 14px;
  font-size: 13px;
}
</style>
