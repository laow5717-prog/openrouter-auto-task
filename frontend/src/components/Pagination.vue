<template>
  <div v-if="total > 0" class="pagination">
    <div class="pagination-info">
      <span>共 {{ total }} 条，第 {{ start }}-{{ end }} 条</span>
      <select :value="pageSize" @change="$emit('update:pageSize', Number($event.target.value)); $emit('change', 1)">
        <option v-for="s in [10, 20, 50, 100]" :key="s" :value="s">{{ s }} 条/页</option>
      </select>
    </div>
    <div class="pagination-buttons">
      <button :disabled="page <= 1" @click="$emit('change', page - 1)">&lsaquo;</button>
      <template v-for="p in pages" :key="p">
        <span v-if="p === '...'" class="page-ellipsis">...</span>
        <button v-else :class="{ active: p === page }" @click="$emit('change', p)">{{ p }}</button>
      </template>
      <button :disabled="page >= totalPages" @click="$emit('change', page + 1)">&rsaquo;</button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
})

defineEmits(['change', 'update:pageSize'])

const totalPages = computed(() => Math.ceil(props.total / props.pageSize) || 1)
const start = computed(() => (props.page - 1) * props.pageSize + 1)
const end = computed(() => Math.min(props.page * props.pageSize, props.total))

const pages = computed(() => {
  const tp = totalPages.value
  const cur = props.page
  const maxVisible = 5
  let s = Math.max(1, cur - Math.floor(maxVisible / 2))
  let e = Math.min(tp, s + maxVisible - 1)
  if (e - s < maxVisible - 1) s = Math.max(1, e - maxVisible + 1)

  const result = []
  if (s > 1) { result.push(1); if (s > 2) result.push('...') }
  for (let i = s; i <= e; i++) result.push(i)
  if (e < tp) { if (e < tp - 1) result.push('...'); result.push(tp) }
  return result
})
</script>

<style scoped>
.pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: #fafafa;
  border-top: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-sub);
}
.pagination-info {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pagination-info select {
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 12px;
  background: white;
  cursor: pointer;
}
.pagination-buttons {
  display: flex;
  align-items: center;
  gap: 4px;
}
.pagination-buttons button {
  min-width: 32px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: white;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
  color: var(--text-main);
}
.pagination-buttons button:hover:not(:disabled) { background: #f3f4f6; }
.pagination-buttons button.active {
  background: var(--primary);
  color: white;
  border-color: var(--primary);
}
.pagination-buttons button:disabled { opacity: 0.4; cursor: not-allowed; }
.page-ellipsis {
  min-width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
