<template>
  <section class="elapsed-panel" data-testid="elapsed-panel">
    <header class="elapsed-head">
      <div>
        <h3>耗时迁移与慢请求结案</h3>
        <p class="page-desc">
          整数文本按毫秒读取，合法的 0 是快速响应；空值和文字占位进入未知明细，不补 0。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="migration-run" type="button" :disabled="busy" @click="migrate">
          迁移一批
        </button>
        <button class="btn" data-testid="migration-rollback" type="button" :disabled="busy" @click="rollback">
          回滚迁移
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">迁移状态</span>
        <strong class="stat-value" data-testid="migration-state">{{ migration?.state ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已迁移 / 剩余</span>
        <strong class="stat-value">{{ migration?.processed ?? 0 }} / {{ migration?.remaining ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">有效样本</span>
        <strong class="stat-value" data-testid="valid-samples">{{ report?.stats.valid_samples ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未知数量</span>
        <strong class="stat-value" data-testid="unknown-count">{{ report?.stats.unknown ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">慢请求（≥{{ threshold }}ms）</span>
        <strong class="stat-value" data-testid="slow-count">{{ report?.stats.slow_count ?? 0 }}</strong>
      </article>
    </div>

    <p class="elapsed-note" data-testid="slow-denominator">
      慢请求率 {{ report?.stats.slow_rate_percent ?? '—' }}%，
      分母为有效样本 {{ report?.stats.slow_sample_denominator ?? 0 }} 条；
      未知 {{ report?.stats.unknown ?? 0 }} 条（{{ reasonText }}）不参与分母。
      有效样本中 0ms 快速响应 {{ report?.stats.fast_responses ?? 0 }} 条。
    </p>

    <table class="data-table">
      <thead>
        <tr><th>日志</th><th>原始耗时</th><th>毫秒值</th><th>状态</th><th>未知原因</th><th>补正</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in report?.elapsed_view ?? []" :key="item.id" :data-testid="`elapsed-${item.id}`">
          <td>#{{ item.id }}</td>
          <td>{{ item.raw === null || item.raw === '' ? '（空）' : item.raw }}</td>
          <td>{{ item.elapsed_ms === null ? '—' : item.elapsed_ms }}</td>
          <td>{{ item.state }}</td>
          <td>{{ item.unknown_reason ?? '—' }}</td>
          <td>
            <button class="link" type="button" :data-testid="`correct-${item.id}`" @click="correct(item.id)">
              补正
            </button>
          </td>
        </tr>
        <tr v-if="!report?.elapsed_view?.length">
          <td colspan="6" class="empty-state">暂无日志</td>
        </tr>
      </tbody>
    </table>

    <p v-if="message" class="elapsed-note">{{ message }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  correctElapsed,
  loadMigrationStatus,
  loadSlowReport,
  rollbackMigration,
  runMigration,
} from '@/api/backend-service'
import type { MigrationStatus, SlowReport } from '@/data/elapsed'

const threshold = 100
const migration = ref<MigrationStatus | null>(null)
const report = ref<SlowReport | null>(null)
const message = ref('')
const errorMessage = ref('')
const busy = ref(false)

const reasonText = computed(() => {
  const reasons = report.value?.stats.unknown_reasons ?? {}
  const entries = Object.entries(reasons)
  return entries.length ? entries.map(([reason, count]) => `${reason} ${count}`).join('、') : '无'
})

async function refresh() {
  const [status, slow] = await Promise.all([
    loadMigrationStatus(),
    loadSlowReport(undefined, threshold),
  ])
  migration.value = status
  report.value = slow
}

async function withBusy(task: () => Promise<string>) {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    message.value = await task()
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function migrate() {
  void withBusy(async () => {
    const result = await runMigration(2)
    return `迁移：${result.message}（本批转换 ${result.converted_this_run} 条，未知 ${result.unknown_this_run} 条）`
  })
}

function rollback() {
  void withBusy(async () => {
    const result = await rollbackMigration()
    return `回滚：${result.message}`
  })
}

function correct(logId: number) {
  const input = window.prompt('输入真实耗时（毫秒），留空表示仍未测得', '200')
  if (input === null) return
  void withBusy(async () => {
    await correctElapsed(logId, input)
    return `日志 #${logId} 已补正`
  })
}

onMounted(() => {
  refresh().catch((error) => {
    errorMessage.value = error instanceof Error ? error.message : '迁移面板读取失败'
  })
})
</script>

<style scoped>
.elapsed-panel { margin-top: 20px; padding-top: 12px; border-top: 2px solid var(--border); }
.elapsed-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.elapsed-note { color: var(--muted); font-size: 12px; }
</style>
