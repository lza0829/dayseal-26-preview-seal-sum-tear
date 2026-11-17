<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const preview = ref(null)
const declared = ref(0)
const batches = ref([])
const integrity = ref(null)
const msg = ref('')
const err = ref('')

const inconsistent = computed(() => integrity.value?.batches?.filter(b => b.mismatches?.length) || [])
const sealed = computed(() => !!preview.value?.sealed)

async function load() {
  err.value = ''
  // 展示性预览：仅呈现当前合计与签发快照，不带实盘声明
  preview.value = await api.closePreview()
  declared.value = preview.value.system_total
  batches.value = await api.batches()
  integrity.value = await api.integrity()
}

async function seal() {
  msg.value = ''; err.value = ''
  try {
    // 封账瞬间按当前实盘签发权威快照；服务端会在同一写事务内以现行流水复核，
    // 任一不符（流水变更/预览过期/口径变更）都会 409 且不落批。
    const snap = await api.closePreview(preview.value.business_day, declared.value)
    const r = await api.seal({
      preview_id: snap.preview_id,
      business_day: snap.business_day,
      declared_cash: declared.value,
    })
    msg.value = `已封账 ${r.batch_no}：系统 ${r.system_total.toFixed(2)} / 实盘 ${r.declared_cash.toFixed(2)} / 长短款 ${r.variance.toFixed(2)}（${r.disposition}）`
  } catch (e) {
    err.value = e.message
  }
  await load()
}

async function voidBatch(b) {
  msg.value = ''; err.value = ''
  try {
    const r = await api.voidBatch(b.batch_id, '批次与现行流水不一致，作废并恢复写闸')
    msg.value = `已作废 ${b.batch_no}（${r.business_day}），写闸已恢复：${r.writable ? '可写' : '仍锁定'}`
  } catch (e) {
    err.value = e.message
  }
  await load()
}

onMounted(load)
</script>

<template>
  <h2>日结封账</h2>
  <div v-if="preview" class="panel">
    <p>
      营业日 {{ preview.business_day }} · 笔数 {{ preview.line_count }} · 系统合计 {{ preview.system_total.toFixed(2) }}
      <span class="tag" :class="sealed ? 'ok' : 'warn'">{{ sealed ? '已封' : '未封' }}</span>
    </p>
    <p class="msg">
      预览快照 {{ preview.preview_id }} · 口径 {{ preview.caliber_version }} · {{ preview.ttl_seconds }}s 内一次性有效。
      封账以现行流水净额与流水指纹为权威：预览后若流水、笔数、合计任一变化，旧快照即失效，必须重新预览。
    </p>
    <div class="row">
      <label>实盘现金 <input type="number" step="0.01" v-model.number="declared" /></label>
      <button class="btn" :disabled="sealed" @click="seal">封账</button>
      <button class="btn ghost" @click="load">刷新</button>
    </div>
    <p class="msg">
      预计长短款 {{ (Number(declared || 0) - preview.system_total).toFixed(2) }}
      （实盘 - 系统合计）
    </p>
  </div>
  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>

  <div v-if="inconsistent.length" class="panel">
    <h2>一致性告警</h2>
    <p class="msg err">
      以下已封批次与现行流水/预览/长短款不同源，应作废并恢复写闸后再重新预览封账。
    </p>
    <table>
      <thead><tr><th>批次</th><th>营业日</th><th>批头合计</th><th>撕裂原因</th><th></th></tr></thead>
      <tbody>
        <tr v-for="b in inconsistent" :key="b.batch_id">
          <td>{{ b.batch_no }}</td><td>{{ b.business_day }}</td>
          <td>{{ b.system_total.toFixed(2) }}</td>
          <td>{{ b.mismatches.join('；') }}</td>
          <td><button class="btn" @click="voidBatch(b)">作废</button></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="panel">
    <h2>封账批次</h2>
    <table>
      <thead><tr><th>批次</th><th>营业日</th><th>系统</th><th>实盘</th><th>差额</th><th>预览</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td><td>{{ b.business_day }}</td>
          <td>{{ b.system_total.toFixed(2) }}</td><td>{{ b.declared_cash.toFixed(2) }}</td>
          <td>{{ b.variance.toFixed(2) }}</td>
          <td class="msg">{{ b.preview_id || '无绑定（历史）' }}</td>
          <td>
            <span class="tag" :class="b.status === 'sealed' ? 'ok' : 'warn'">{{ b.status }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
