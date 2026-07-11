import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'dashboard', component: () => import('../views/Dashboard.vue') },
  { path: '/cardmode', name: 'cardmode', component: () => import('../views/CardMode.vue') },
  { path: '/accounts', name: 'accounts', component: () => import('../views/Accounts.vue') },
  { path: '/card-history', name: 'cardHistory', component: () => import('../views/CardHistory.vue') },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
