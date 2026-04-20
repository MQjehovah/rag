import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { createRouter, createWebHistory } from 'vue-router'
import Editor from './views/Editor.vue'
import KnowledgeGraph from './views/KnowledgeGraph.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Editor },
    { path: '/graph', component: KnowledgeGraph },
  ]
})

const app = createApp(App)
app.use(createPinia())
app.use(ElementPlus)
app.use(router)
app.mount('#app')
