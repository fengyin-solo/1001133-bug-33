<template>
  <section class="page" data-module="expansion">
    <header class="page-head">
      <div>
        <h2>伸缩缝管理</h2>
        <p class="page-desc">成组更换按施工批次统一推进：评估 → 预占 → 施工 → 验收，更换结论同步落到缝台账、缝详情与物资清单。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记伸缩缝</button>
        <button class="btn" type="button" @click="exportRows">导出伸缩缝清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <h3 class="section-title">缝台账</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th class="row-select"><input type="checkbox" :checked="allSelected" @change="toggleAll" /></th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td class="row-select"><input v-model="selected" type="checkbox" :value="Number(row.id)" /></td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无伸缩缝数据，可先登记伸缩缝</td>
        </tr>
      </tbody>
    </table>

    <div class="batch-bar">
      <button class="btn primary" type="button" :disabled="!selected.length" @click="createBatch">
        成组更换（{{ selected.length }} 条）
      </button>
      <button class="btn ghost" type="button" @click="reloadBatches">刷新施工批次</button>
    </div>

    <h3 class="section-title">施工工作台（按评估意见安全优先顺序）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次号</th>
          <th>阶段</th>
          <th>序号</th>
          <th>缝编号</th>
          <th>所属桥梁</th>
          <th>评估意见</th>
          <th>工单状态</th>
          <th>更换结论</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="order in workOrders" :key="`${order.批次号}-${order.缝id}`">
          <td>{{ order.批次号 }}</td>
          <td>{{ order.阶段 }}</td>
          <td>{{ order.安全优先顺序 }}</td>
          <td>{{ order.缝编号 }}</td>
          <td>{{ order.所属桥梁 }}</td>
          <td>{{ order.评估意见 }}</td>
          <td>{{ order.状态 }}</td>
          <td>{{ order.更换结论 ?? '—' }}</td>
        </tr>
        <tr v-if="!workOrders.length">
          <td colspan="8" class="empty-state">暂无施工工单，勾选缝台账后开立成组更换批次</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">施工批次</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次号</th>
          <th>阶段</th>
          <th>状态</th>
          <th>来源</th>
          <th>缝数</th>
          <th>开立日期</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="String(batch.id)">
          <td>{{ batch.批次号 }}</td>
          <td>{{ batch.stage }}</td>
          <td>{{ batch.status }}</td>
          <td>{{ batch.来源 }}</td>
          <td>{{ batch.缝ids?.length ?? 0 }}</td>
          <td>{{ batch.开立日期 }}</td>
          <td class="row-actions">
            <button
              v-if="batch.stage !== '验收'"
              class="link"
              type="button"
              :disabled="advancingId === batch.id"
              @click="advanceBatch(batch)"
            >
              推进至{{ nextStage(batch.stage) }}
            </button>
            <span v-else>已完成验收</span>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="7" class="empty-state">尚未开立施工批次</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条伸缩缝记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Batch = {
  id: number
  批次号: string
  stage: string
  status: string
  来源: string
  开立日期: string
  缝ids: number[]
}
type WorkOrder = Record<string, string | number | null>

const ENDPOINT = '/api/expansion'
const STAGES = ['评估', '预占', '施工', '验收']
const columns = ['缝编号', '所属桥梁', '缝类型', '设计伸缩量', '当前缝宽', '评估意见', '缝状态', '最近批次号', '更换结论']
const actions = ['清理堵塞', '修补锚固', '更换伸缩缝']
const stats = ref([
  { label: '待更换缝', value: 0 },
  { label: '施工中批次', value: 0 },
  { label: '已完成批次', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ['缝编号', '所属桥梁', '缝类型']
const selected = ref<number[]>([])
const batches = ref<Batch[]>([])
const workOrders = ref<WorkOrder[]>([])
const advancingId = ref<number | null>(null)

const allSelected = computed(
  () => rows.value.length > 0 && selected.value.length === rows.value.length,
)

function nextStage(stage: string): string {
  return STAGES[Math.min(STAGES.indexOf(stage) + 1, STAGES.length - 1)]
}

function toggleAll(event: Event) {
  selected.value = (event.target as HTMLInputElement).checked
    ? rows.value.map((row) => Number(row.id))
    : []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '伸缩缝登记入口尚未接入审批流'
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('伸缩缝列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = rows.value.filter((row) => row.缝状态 !== '已更换').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '伸缩缝管理列表读取失败'
  }
}

async function reloadBatches() {
  try {
    const response = await request(`${ENDPOINT}/workbench`)
    if (response.ok) {
      const payload = await response.json()
      workOrders.value = payload.items ?? []
    }
    const batchResponse = await request(`${ENDPOINT}/batches`)
    if (batchResponse.ok) {
      const payload = await batchResponse.json()
      batches.value = payload.items ?? []
      stats.value[1].value = batches.value.filter((batch) => batch.stage !== '验收').length
      stats.value[2].value = batches.value.filter((batch) => batch.stage === '验收').length
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '施工批次读取失败'
  }
}

async function createBatch() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/batches`, {
      method: 'POST',
      body: JSON.stringify({ values: { 缝ids: selected.value } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail ?? payload.message ?? '施工批次开立失败')
    }
    selected.value = []
    await Promise.all([reloadBatches(), reload()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '施工批次开立失败'
  }
}

async function advanceBatch(batch: Batch) {
  errorMessage.value = ''
  advancingId.value = batch.id
  try {
    const response = await request(`${ENDPOINT}/batches/${batch.id}/advance`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      const hint = response.status === 409 ? '，并发提交只允许一个批次推进' : ''
      throw new Error(`${payload.detail ?? payload.message ?? '阶段推进失败'}${hint}`)
    }
    await Promise.all([reloadBatches(), reload()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '阶段推进失败'
  } finally {
    advancingId.value = null
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || payload?.ok === false) {
      throw new Error(payload?.detail ?? payload?.message ?? '伸缩缝管理动作未生效，请稍后重试')
    }
    await Promise.all([reload(), reloadBatches()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '伸缩缝管理操作失败'
  }
}

onMounted(() => {
  void reload()
  void reloadBatches()
})
</script>
