<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try { rows.value = await api.variances() } catch (e) { err.value = e.message }
})

// 处置状态原样展示：缺失就是“未知”，不得默认成 balanced 掩盖未处置差额
function label(r) {
  return r.disposition || 'unknown'
}

const pendingCount = computed(() => rows.value.filter(r => r.disposition === 'pending').length)
const voidedCount = computed(() => rows.value.filter(r => r.disposition === 'voided').length)
</script>

<template>
  <h2>长短款</h2>
  <p v-if="err" class="msg err">{{ err }}</p>
  <p class="msg">
    未处置 {{ pendingCount }} 笔 · 随批作废 {{ voidedCount }} 笔。
    长短款只能由封账事务写入；批次作废时本条同源置为 voided。
  </p>
  <div class="panel">
    <table>
      <thead><tr><th>营业日</th><th>批次</th><th>差额</th><th>处置</th><th>时间</th></tr></thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.business_day }}</td>
          <td>#{{ r.batch_id }}</td>
          <td>{{ Number(r.variance || 0).toFixed(2) }}</td>
          <td>
            <span class="tag" :class="r.disposition === 'balanced' ? 'ok' : 'warn'">{{ label(r) }}</span>
          </td>
          <td>{{ r.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
