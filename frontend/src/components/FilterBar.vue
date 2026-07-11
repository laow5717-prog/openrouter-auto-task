<template>
  <div class="filter-bar">
    <slot />
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  background: #fafafa;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}

.filter-bar :deep(.filter-input) {
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  background: white;
  transition: border 0.2s;
}
.filter-bar :deep(.filter-input:focus) {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(243, 128, 32, 0.1);
}
.filter-bar :deep(.filter-select) {
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  background: white;
  cursor: pointer;
}
.filter-bar :deep(.filter-date) {
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  background: white;
}
.filter-bar :deep(.filter-btn) {
  padding: 7px 16px;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
.filter-bar :deep(.filter-btn-primary) { background: var(--primary); color: white; }
.filter-bar :deep(.filter-btn-primary:hover) { background: var(--primary-hover); }
.filter-bar :deep(.filter-btn-reset) {
  background: white;
  color: var(--text-sub);
  border: 1px solid var(--border);
}
.filter-bar :deep(.filter-btn-reset:hover) { background: #f3f4f6; }
.filter-bar :deep(.filter-sep) {
  width: 1px;
  height: 24px;
  background: var(--border);
}
</style>
