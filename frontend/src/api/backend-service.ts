import { fetchJson, request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ClosureVersion, MigrationStatus, SlowReport } from '@/data/elapsed'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 耗时迁移 / 慢请求结案 ----

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? payload.message ?? '操作失败')
  return payload as T
}

export async function loadMigrationStatus(): Promise<MigrationStatus> {
  return await fetchJson<MigrationStatus>('/api/api_logger/migration')
}

export async function runMigration(batchSize = 2, stopAfter?: number): Promise<Record<string, unknown>> {
  return await postJson('/api/api_logger/migration', { batch_size: batchSize, stop_after: stopAfter })
}

export async function rollbackMigration(batchNo?: number): Promise<Record<string, unknown>> {
  return await postJson('/api/api_logger/migration/rollback', { batch_no: batchNo })
}

export async function loadSlowReport(apiName?: string, thresholdMs = 100): Promise<SlowReport> {
  const query = new URLSearchParams({ threshold_ms: String(thresholdMs) })
  if (apiName) query.set('api_name', apiName)
  return await fetchJson<SlowReport>(`/api/api_logger/slow-requests?${query}`)
}

export async function correctElapsed(logId: number, value: unknown): Promise<Record<string, unknown>> {
  return await postJson(`/api/api_logger/${logId}/elapsed`, { elapsed_ms: value })
}

export async function closeRun(
  schedulerId: number,
  revisionOf?: number,
  slowThresholdMs = 100,
): Promise<{ closure: ClosureVersion; reissued?: boolean }> {
  return await postJson(`/api/api_scheduler/${schedulerId}/close`, {
    revision_of: revisionOf,
    slow_threshold_ms: slowThresholdMs,
  })
}

export async function loadClosures(schedulerId: number): Promise<{ versions: ClosureVersion[] }> {
  return await fetchJson(`/api/api_scheduler/${schedulerId}/closures`)
}
