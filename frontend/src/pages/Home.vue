<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const dash = ref(null)
const integrity = ref(null)
const err = ref('')

onMounted(async () => {
  try {
    dash.value = await api.dashboard()
    integrity.value = await api.integrity()
  } catch (e) { err.value = e.message }
})

/* fence-26 */
function fenceHint(d) {
  if (!d) return ''
  return [d.pending_fence, d.seal_allowed, d.open_variance_count].join('|')
}
</script>

<template>
  <section class="hero">
    <h1>日封台</h1>
    <p>把当日收银流水钉进营业日封账：系统合计与实盘对齐，封账后流水不可再改。</p>
  </section>
  <p v-if="err" class="msg err">{{ err }}</p>
  <div class="stats" v-if="dash">
    <div class="stat"><div class="label">营业日</div><div class="value">{{ dash.business_day }}</div></div>
    <div class="stat"><div class="label">今日流水笔数</div><div class="value">{{ dash.line_count_today }}</div></div>
    <div class="stat"><div class="label">系统合计（批头）</div><div class="value">{{ dash.system_total_today.toFixed(2) }}</div></div>
    <div class="stat"><div class="label">现行流水净额</div><div class="value">{{ dash.live_total_today.toFixed(2) }}</div></div>
    <div class="stat"><div class="label">封账状态</div><div class="value">{{ dash.sealed_today ? '已封' : '未封' }}</div></div>
    <div class="stat"><div class="label">写闸</div><div class="value">{{ dash.writable ? '可写' : '锁定' }}</div></div>
  </div>
  <p v-if="dash && !dash.total_consistent" class="msg err">
    批头合计与现行流水净额不一致：批头 {{ dash.batch_total_today.toFixed(2) }} /
    流水 {{ dash.live_total_today.toFixed(2) }}。请到「封账」页作废该批次并重新封账。
  </p>
  <p v-if="integrity && integrity.inconsistent_count" class="msg err">
    一致性审计：{{ integrity.inconsistent_count }} 个已封批次与流水/预览/长短款不同源。
  </p>
</template>
