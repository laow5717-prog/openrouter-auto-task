<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay" @click.self="$emit('close')">
      <div class="modal-content" :class="{ wide }">
        <div class="modal-header">
          <div class="modal-title">{{ title }}</div>
          <button class="modal-close" @click="$emit('close')">&times;</button>
        </div>
        <div class="modal-body">
          <slot />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
defineProps({
  visible: Boolean,
  title: { type: String, default: '' },
  wide: Boolean,
})
defineEmits(['close'])
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal-content {
  background: var(--bg-card);
  border-radius: var(--radius);
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
  width: 560px;
  max-width: 90vw;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 24px;
  border-bottom: 1px solid var(--border);
}
.modal-title { font-size: 16px; font-weight: 600; }
.modal-close {
  width: 32px; height: 32px;
  border: none; background: none;
  font-size: 20px; color: var(--text-sub);
  cursor: pointer; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
}
.modal-close:hover { background: #f3f4f6; color: var(--text-main); }
.modal-body { padding: 24px; overflow-y: auto; flex: 1; }
.modal-content.wide { width: 720px; }
</style>
