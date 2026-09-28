import axios from 'axios'

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
// 默认 60s 超时兜底:网络挂起时请求不至于无限等待;
// 长同步调用点(图谱重建/试编译/嵌入连通测试)在调用处传 { timeout: 0 } 豁免
const http = axios.create({ baseURL: API_BASE, timeout: 60000 })

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