<template>
  <svg
    class="icon"
    :width="size" :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-for="(d, i) in paths" :key="i">
      <path v-if="d.d" :d="d.d" />
      <circle v-else-if="d.c" :cx="d.c[0]" :cy="d.c[1]" :r="d.c[2]" />
      <rect v-else-if="d.r" :x="d.r[0]" :y="d.r[1]" :width="d.r[2]" :height="d.r[3]" :rx="d.r[4] || 0" />
      <line v-else-if="d.l" :x1="d.l[0]" :y1="d.l[1]" :x2="d.l[2]" :y2="d.l[3]" />
    </template>
  </svg>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 18 },
  strokeWidth: { type: [Number, String], default: 1.75 },
})

// 线性图标库 (stroke-based, 24x24)
const ICONS = {
  // 运行监控 — 仪表盘
  dashboard: [{ d: 'M3 13a9 9 0 0 1 18 0' }, { d: 'M12 13l4-3' }, { c: [12, 13, 0.5] }],
  // 卡片管理
  cards: [{ r: [2, 5, 20, 14, 2.5] }, { l: [2, 10, 22, 10] }, { l: [6, 15, 10, 15] }],
  // 导入绑卡
  bind: [{ r: [2, 5, 20, 14, 2.5] }, { l: [2, 9.5, 22, 9.5] }, { d: 'M17 15h2m-1-1v2' }],
  // 账号管理
  accounts: [{ c: [9, 8, 3.2] }, { d: 'M3.5 19a6 6 0 0 1 11 0' }, { d: 'M16 7.5a3 3 0 0 1 0 5.8' }, { d: 'M17.5 19a6 6 0 0 0-2-4' }],
  // 绑卡记录
  history: [{ d: 'M3 12a9 9 0 1 0 3-6.7' }, { d: 'M3 4.5V9h4.5' }, { d: 'M12 8v4l3 2' }],
  // 充值记录 — 钱包
  wallet: [{ d: 'M3 7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2' }, { r: [3, 7, 18, 12, 2.5] }, { d: 'M16 12.5h.01' }, { d: 'M21 10h-4a2 2 0 0 0 0 4h4' }],
  // 闪电 — 每日任务
  bolt: [{ d: 'M13 2 4 14h7l-1 8 9-12h-7l1-8z' }],
  play: [{ d: 'M6 4.5v15l13-7.5-13-7.5z' }],
  stop: [{ r: [5, 5, 14, 14, 2.5] }],
  upload: [{ d: 'M12 16V5' }, { d: 'M7 10l5-5 5 5' }, { d: 'M4 18h16' }],
  download: [{ d: 'M12 5v11' }, { d: 'M7 11l5 5 5-5' }, { d: 'M4 20h16' }],
  trash: [{ d: 'M4 7h16' }, { d: 'M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2' }, { d: 'M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13' }],
  search: [{ c: [11, 11, 7] }, { l: [16.5, 16.5, 21, 21] }],
  plus: [{ l: [12, 5, 12, 19] }, { l: [5, 12, 19, 12] }],
  monitor: [{ r: [3, 4, 18, 13, 2.5] }, { l: [8, 21, 16, 21] }, { l: [12, 17, 12, 21] }],
  terminal: [{ r: [3, 4, 18, 16, 2.5] }, { d: 'M7 9l3 3-3 3' }, { l: [12, 15, 16, 15] }],
  check: [{ d: 'M5 12.5l4.5 4.5L19 7.5' }],
  refresh: [{ d: 'M20 11a8 8 0 1 0-.5 4' }, { d: 'M20 5v6h-6' }],
  clock: [{ c: [12, 12, 8.5] }, { d: 'M12 7.5V12l3 1.8' }],
}

const paths = computed(() => ICONS[props.name] || [])
</script>

<style scoped>
.icon { display: block; flex-shrink: 0; }
</style>
