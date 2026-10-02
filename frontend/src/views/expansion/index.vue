<template>
  <section class="page" data-module="expansion">
    <header class="page-head">
      <div>
        <h2>伸缩缝管理</h2>
        <p class="page-desc">
          缝台账维护伸缩缝登记、清理与修补；成组更换统一按施工批次提交，
          材料预占、缝台账、施工工作台共用同一份进度。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出伸缩缝管理清单</button>
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
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
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
          <td :colspan="columns.length + 1" class="empty-state">暂无伸缩缝管理数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条伸缩缝管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="replacement-panel">
      <header class="panel-head">
        <h3 class="section-title">成组更换工作台</h3>
        <p class="page-desc">
          按施工批次幂等推进：提交预占 → 安全评估（紧急&gt;严重&gt;一般）→ 更换落账。
          重试不会重复预占、重复建单或重复扣料；更换结论回写缝台账，且保留此前缝宽记录。
        </p>
      </header>

      <form class="batch-form" @submit.prevent="submitBatch">
        <label class="filter-item">
          <span>施工批次号</span>
          <input v-model="form.施工批次号" placeholder="如 BATCH-202610-01" required />
        </label>
        <label class="filter-item">
          <span>缝id（逗号分隔）</span>
          <input v-model="form.seamText" placeholder="如 1,2" required />
        </label>
        <label class="filter-item">
          <span>新材料id</span>
          <input v-model.number="form.materialId" type="number" placeholder="如 1" required />
        </label>
        <label class="filter-item">
          <span>预占数量</span>
          <input v-model.number="form.quantity" type="number" min="1" placeholder="如 4" required />
        </label>
        <button class="btn primary" type="submit">提交并预占</button>
      </form>

      <div v-if="workbench.length" class="workbench">
        <h4 class="section-title">待更换工单（按安全优先顺序）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>安全顺序</th><th>施工批次号</th><th>缝编号</th><th>所属桥梁</th><th>评估意见</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="order in workbench" :key="`${order.施工批次号}-${order.缝id}`">
              <td>{{ order.安全顺序 }}</td>
              <td>{{ order.施工批次号 }}</td>
              <td>{{ order.缝编号 }}</td>
              <td>{{ order.所属桥梁 ?? '—' }}</td>
              <td>{{ order.评估意见 }}</td>
              <td class="row-actions">
                <button class="link" type="button" @click="assessBatch(order.施工批次号)">重新评估</button>
                <button class="link" type="button" @click="confirmBatch(order.施工批次号)">更换落账</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4 class="section-title">施工批次</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>施工批次号</th><th>批次阶段</th><th>缝数量</th><th>已更换</th><th>工单进度</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.施工批次号">
            <td>{{ batch.施工批次号 }}</td>
            <td>{{ batch.批次阶段 }}</td>
            <td>{{ batch.缝数量 }}</td>
            <td>{{ batch.已更换数量 }}</td>
            <td>
              <span v-for="order in batch.工单列表" :key="order.id" class="order-chip">
                {{ order.缝编号 }}：{{ order.工单状态
                  }}{{ order.安全顺序 ? `（第${order.安全顺序}位）` : '' }}
              </span>
            </td>
            <td class="row-actions">
              <button
                v-if="batch.批次阶段 === '已提交'"
                class="link"
                type="button"
                @click="assessBatch(batch.施工批次号)"
              >安全评估</button>
              <button
                v-if="batch.批次阶段 === '已评估待更换'"
                class="link"
                type="button"
                @click="confirmBatch(batch.施工批次号)"
              >更换落账</button>
              <span v-else-if="batch.批次阶段 === '已完成'" class="muted">已完成</span>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="6" class="empty-state">暂无施工批次，可先在上方提交一组伸缩缝</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface Order {
  id: number
  缝id: number
  缝编号: string
  所属桥梁?: string
  施工批次号: string
  工单状态: string
  评估意见: string
  安全顺序: number
}
interface Batch {
  施工批次号: string
  批次名称?: string
  批次阶段: string
  缝数量: number
  已更换数量: number
  工单列表: Order[]
}

const ENDPOINT = '/api/expansion'
const REPLACE_ENDPOINT = '/api/replacements'
const columns = ["缝编号", "所属桥梁", "缝类型", "设计伸缩量", "当前缝宽", "堵塞情况", "锚固状态", "缝状态"]
// 更换伸缩缝已收拢到成组更换，单缝只保留清理与修补。
const actions = ["清理堵塞", "修补锚固"]
const stats = [{"label": "正常伸缩缝", "value": 0}, {"label": "堵塞伸缩缝", "value": 0}, {"label": "损坏伸缩缝", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const batches = ref<Batch[]>([])
const workbench = ref<Order[]>([])
const form = reactive({ 施工批次号: '', seamText: '', materialId: 1, quantity: 2 })

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function postAction(path: string, body: Record<string, unknown>) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || '操作未生效，请稍后重试')
  }
  return payload
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    await postAction(`${ENDPOINT}/${row.id}/actions`, { action })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '伸缩缝管理操作失败'
  }
}

async function submitBatch() {
  errorMessage.value = ''
  const seamIds = form.seamText.split(/[,，]/).map((s) => Number(s.trim())).filter(Boolean)
  try {
    await postAction(`${REPLACE_ENDPOINT}/submit`, {
      values: {
        施工批次号: form.施工批次号.trim(),
        缝id列表: seamIds,
        材料列表: [{ 材料id: form.materialId, 数量: form.quantity }],
      },
    })
    await Promise.all([reload(), reloadBatches()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交成组更换失败'
  }
}

async function assessBatch(batchNo: string) {
  errorMessage.value = ''
  const opinions = await promptOpinions(batchNo)
  if (!opinions) return
  try {
    await postAction(`${REPLACE_ENDPOINT}/assess`, { values: { 施工批次号: batchNo, 评估意见: opinions } })
    await Promise.all([reloadBatches(), reloadWorkbench()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '安全评估失败'
  }
}

async function confirmBatch(batchNo: string) {
  errorMessage.value = ''
  try {
    await postAction(`${REPLACE_ENDPOINT}/confirm`, { values: { 施工批次号: batchNo } })
    await Promise.all([reload(), reloadBatches(), reloadWorkbench()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '更换落账失败'
  }
}

async function promptOpinions(batchNo: string): Promise<Record<string, string> | null> {
  const batch = batches.value.find((item) => item.施工批次号 === batchNo)
  if (!batch) return null
  const opinions: Record<string, string> = {}
  for (const order of batch.工单列表) {
    const previous = order.评估意见 || '一般'
    const input = window.prompt(`缝 ${order.缝编号} 的评估意见（紧急/严重/一般）`, previous)
    if (input === null) return null
    const text = input.trim()
    if (!text) return null
    opinions[String(order.缝id)] = text
  }
  return opinions
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
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '伸缩缝管理列表读取失败'
  }
}

async function reloadBatches() {
  try {
    const response = await request(`${REPLACE_ENDPOINT}?page=1&size=100`)
    if (!response.ok) return
    const payload = await response.json()
    batches.value = payload.items ?? []
  } catch {
    // 批次列表失败不阻塞台账阅读
  }
}

async function reloadWorkbench() {
  try {
    const response = await request(`${REPLACE_ENDPOINT}/workbench`)
    if (!response.ok) return
    const payload = await response.json()
    workbench.value = payload.items ?? []
  } catch {
    // 工作台失败不阻塞台账阅读
  }
}

onMounted(() => {
  void reload()
  void reloadBatches()
  void reloadWorkbench()
})
</script>

<style scoped>
.section-title {
  margin: 16px 0 8px;
  font-size: 15px;
}

.replacement-panel {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--border-color, #e5e7eb);
}

.batch-form {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
  margin-bottom: 12px;
}

.order-chip {
  display: inline-block;
  margin: 2px 6px 2px 0;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(59, 130, 246, 0.12);
  font-size: 12px;
  white-space: nowrap;
}

.muted {
  color: #9ca3af;
}
</style>
