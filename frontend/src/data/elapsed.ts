/** 耗时迁移 / 慢请求结案面板用到的接口类型。 */
export type MigrationStatus = {
  state: string
  processed: number
  total: number
  remaining: number
  row_total: number
  batch_count: number
  scanned: number
}

export type SlowStats = {
  total: number
  valid_samples: number
  unknown: number
  unknown_reasons: Record<string, number>
  fast_responses: number
  slow_count: number
  slow_threshold_ms: number
  slow_sample_denominator: number
  slow_rate_percent: number | null
  unknown_rate_percent: number
  avg_ms: number | null
  min_ms: number | null
  max_ms: number | null
  has_valid_sample: boolean
}

export type SlowReport = {
  api_name: string
  threshold_ms: number
  stats: SlowStats
  elapsed_view: {
    id: number
    raw: unknown
    elapsed_ms: number | null
    state: string
    unknown_reason: string | null
    representation: string
  }[]
}

export type ClosureVersion = {
  version: number
  revision_of: number | null
  closed_at: string
  input_count: number
  snapshot_fingerprint: string
  superseded_by?: number
  stats: SlowStats
  stats_match_snapshot: boolean
  stats_mismatches: string[]
  corrected_since_previous: { id: number; before: unknown; after: unknown }[]
}
