import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'workbench', component: () => import('../views/Workbench.vue') },
  { path: '/monitor', name: 'monitor', component: () => import('../views/Dashboard.vue') },
  { path: '/card-pool', name: 'cardPool', component: () => import('../views/CardPool.vue') },
  { path: '/accounts', name: 'accounts', component: () => import('../views/Accounts.vue') },
  { path: '/card-history', name: 'cardHistory', component: () => import('../views/CardHistory.vue') },
  { path: '/recharge-logs', name: 'rechargeLogs', component: () => import('../views/RechargeLogs.vue') },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
