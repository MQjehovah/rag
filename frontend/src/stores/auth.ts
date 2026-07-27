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
  const token = ref<string>(localStorage.getItem('token') || '')
  const user = ref<User | null>(null)

  const isLoggedIn = computed(() => !!token.value)

  async function login(username: string, password: string) {
    const res = await axios.post('/api/auth/login', { username, password })
    token.value = res.data.token
    user.value = res.data.user
    localStorage.setItem('token', res.data.token)
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('token')
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

  return { token, user, isLoggedIn, login, logout, fetchMe }
})