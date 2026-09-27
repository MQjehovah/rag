import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/global.css'
import App from './App.vue'

// 主题: 默认亮色, 可切换深色/跟随系统(挂载前应用避免闪烁)
const savedTheme = localStorage.getItem('rag-theme') || 'light'
const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
if (savedTheme === 'dark' || (savedTheme === 'system' && prefersDark)) {
  document.documentElement.classList.add('dark')
}
import { createRouter, createWebHistory } from 'vue-router'
import Editor from './views/Editor.vue'
import KnowledgeGraph from './views/KnowledgeGraph.vue'
import Login from './views/Login.vue'
import Chat from './views/Chat.vue'
import Wiki from './views/Wiki.vue'
import Sources from './views/Sources.vue'
import Pipelines from './views/Pipelines.vue'
import CompileTemplates from './views/CompileTemplates.vue'
import Embeddings from './views/Embeddings.vue'
import SharedPage from './views/SharedPage.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/login', component: Login, meta: { public: true } },
    { path: '/share/:token', component: SharedPage, meta: { public: true } },
    { path: '/', component: Chat },
    { path: '/notes', component: Editor },
    { path: '/graph', component: KnowledgeGraph },
    { path: '/wiki', component: Wiki },
    { path: '/wiki/:id', component: Wiki },
    { path: '/sources', component: Sources },
    { path: '/pipelines', component: Pipelines },
    { path: '/templates', component: CompileTemplates },
    { path: '/embeddings', component: Embeddings },
  ]
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  const token = localStorage.getItem('rag_token')
  if (!token) return { path: '/login' }
  return true
})

const app = createApp(App)
app.use(createPinia())
app.use(ElementPlus)
app.use(router)
app.mount('#app')
