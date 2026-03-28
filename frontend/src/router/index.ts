import { createRouter, createWebHistory } from 'vue-router'
import Documents from '../views/Documents.vue'
import Search from '../views/Search.vue'
import Editor from '../views/Editor.vue'

const routes = [
  { path: '/', redirect: '/documents' },
  { path: '/documents', component: Documents },
  { path: '/search', component: Search },
  { path: '/editor', component: Editor }
]

export const router = createRouter({
  history: createWebHistory(),
  routes
})