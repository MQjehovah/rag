import axios from 'axios'

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
const http = axios.create({ baseURL: API_BASE })

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('rag_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('rag_token')
      window.location.href = API_BASE + '/login'
    }
    return Promise.reject(error)
  }
)

export default http