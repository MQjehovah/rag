import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务器端口与 API 代理目标通过环境变量注入，不再写死 3000/8000。
// - VITE_DEV_PORT           前端端口，默认 3000
// - VITE_API_PROXY_TARGET   /api 代理目标，默认 http://127.0.0.1:8000
// acceptance 模式（--mode acceptance）加载 .env.acceptance：3001 → 8001（副本库）。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const devPort = Number(env.VITE_DEV_PORT || 3000)
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [vue()],
    server: {
      port: devPort,
      strictPort: true, // 端口被占用时直接失败，禁止自动换端口
      proxy: {
        '/api': {
          target: apiProxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
