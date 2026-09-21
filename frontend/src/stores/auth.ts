import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import http from '../api/http'

interface User {
  id: string
  username: string
  email: string
  display_name: string
  is_local: boolean
  groups: string[]
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem('rag_token') || '')
  const user = ref<User | null>(null)

  const isLoggedIn = computed(() => !!token.value)

  async function login(username: string, password: string) {
    const res = await axios.post((import.meta.env.BASE_URL || '/').replace(/\/$/, '') + '/api/auth/login', { username, password })
    token.value = res.data.token
    user.value = res.data.user
    localStorage.setItem('rag_token', res.data.token)
  }

  /** SSO 登录:跳统一认证授权页(回调会带 sso_token 回到登录页) */
  function loginWithSso() {
    window.location.href = (import.meta.env.BASE_URL || '/').replace(/\/$/, '') + '/api/auth/sso/start'
  }

  /** SSO 回调带回来的本系统 token:落盘并拉取用户信息 */
  async function adoptSsoToken(ssoToken: string) {
    token.value = ssoToken
    localStorage.setItem('rag_token', ssoToken)
    await fetchMe()
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('rag_token')
  }

  async function fetchMe() {
    if (!token.value) return
    try {
      const res = await http.get('/api/auth/me')
      user.value = res.data
    } catch {
      logout()
    }
  }

  return { token, user, isLoggedIn, login, loginWithSso, adoptSsoToken, logout, fetchMe }
})