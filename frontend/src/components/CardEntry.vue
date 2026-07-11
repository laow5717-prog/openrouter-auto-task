<template>
  <div class="card-entry">
    <div class="card-entry-header">
      <span class="card-entry-label">#{{ index + 1 }}</span>
      <button class="action-btn remove-btn" @click="$emit('remove')">删除</button>
    </div>
    <div class="section-label">卡片信息</div>
    <div class="row">
      <input v-model="card.number" class="ctrl-input" placeholder="卡号 *" style="flex:3">
      <input v-model="card.cvc" class="ctrl-input" placeholder="CVC *" maxlength="4" style="flex:1">
    </div>
    <div class="row">
      <input v-model="card.expiry_month" class="ctrl-input" placeholder="月(MM) *" maxlength="2" style="flex:1">
      <input v-model="card.expiry_year" class="ctrl-input" placeholder="年(YYYY) *" maxlength="4" style="flex:1">
    </div>
    <div class="section-label">账单地址</div>
    <div class="row">
      <input v-model="card.first_name" class="ctrl-input" placeholder="First name *" style="flex:1">
      <input v-model="card.last_name" class="ctrl-input" placeholder="Last name *" style="flex:1">
    </div>
    <input v-model="card.country" class="ctrl-input" placeholder="Country *">
    <input v-model="card.address" class="ctrl-input" placeholder="Address line 1 *">
    <input v-model="card.address2" class="ctrl-input" placeholder="Address line 2 (optional)">
    <div class="row">
      <input v-model="card.city" class="ctrl-input" placeholder="City *" style="flex:1">
      <input v-model="card.state" class="ctrl-input" placeholder="State *" style="flex:1">
    </div>
    <div class="row">
      <input v-model="card.zip" class="ctrl-input" placeholder="ZIP code *" style="flex:1">
      <input v-model="card.company" class="ctrl-input" placeholder="Organization (optional)" style="flex:1">
    </div>
  </div>
</template>

<script setup>
defineProps({ card: Object, index: Number })
defineEmits(['remove'])
</script>

<style scoped>
.card-entry {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px;
  margin-bottom: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.card-entry:hover { border-color: var(--primary); }
.card-entry-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.card-entry-label { font-size: 12px; font-weight: 600; color: var(--primary); }
.remove-btn { font-size: 11px; color: var(--danger); border-color: #fecaca; }
.section-label { font-size: 11px; color: var(--text-sub); font-weight: 500; }
.row { display: flex; gap: 6px; }
</style>
