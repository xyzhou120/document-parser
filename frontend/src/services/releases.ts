import api from './api'

export interface RuleBundle {
  bundle_id: string
  checklist_type: string
  version: number
  base_generation: number
  content_hash: string
  knowledge_hash: string
  rules: Record<string, unknown>[]
  note: string
  operator: string
  created_at: string
  is_active: boolean
  has_been_published: boolean
  current_generation: number
}

export interface ReleaseEvent {
  event_id: string
  checklist_type: string
  bundle_id: string
  previous_bundle_id?: string | null
  action: string
  generation: number
  operator: string
  reason: string
  sample_runs: Record<string, unknown>[]
  sample_report?: Record<string, unknown> | null
  created_at: string
}

export interface ReleaseValidation {
  bundle_id: string
  valid: boolean
  errors: string[]
  enabled_rule_count: number
  verification_status: string
  warnings: string[]
}

export interface ReleasePreview {
  mode: string
  model_called: boolean
  validation: ReleaseValidation
  diff: {
    baseline_bundle_id?: string | null
    bundle_id: string
    added: string[]
    removed: string[]
    changed: Array<{ field_key: string; changed_properties: string[]; before: Record<string, unknown>; after: Record<string, unknown> }>
  }
  knowledge_hash: string
  runtime_rules: Record<string, unknown>[]
  fields: Array<{ field_key: string; methods: string[]; identity_dimensions: string[]; multi_instance: boolean }>
}

export interface ReleaseCommand {
  expected_generation: number
  operator: string
  reason: string
  sample_run_ids?: string[]
  sample_report?: Record<string, unknown>
}

export const createDraft = (payload: {
  checklist_type: string
  rules: Record<string, unknown>[]
  operator: string
  note?: string
}): Promise<RuleBundle> => api.post('/v1/rule-releases/drafts', payload)

export const getBundle = (bundleId: string): Promise<RuleBundle> => api.get(`/v1/rule-releases/drafts/${bundleId}`)

export const getActiveBundle = (checklistType: string): Promise<RuleBundle> =>
  api.get(`/v1/rule-releases/${encodeURIComponent(checklistType)}/active`)

export const listBundles = (checklistType: string): Promise<RuleBundle[]> =>
  api.get(`/v1/rule-releases/${encodeURIComponent(checklistType)}/bundles`)

export const listReleaseHistory = (checklistType: string): Promise<ReleaseEvent[]> =>
  api.get(`/v1/rule-releases/${encodeURIComponent(checklistType)}/history`)

export const validateDraft = (bundleId: string): Promise<ReleaseValidation> =>
  api.post(`/v1/rule-releases/drafts/${bundleId}/validate`)

export const previewDraft = (bundleId: string, baselineId?: string): Promise<ReleasePreview> =>
  api.get(`/v1/rule-releases/drafts/${bundleId}/preview`, { params: { baseline_id: baselineId } })

export const publishBundle = (bundleId: string, command: ReleaseCommand): Promise<ReleaseEvent> =>
  api.post(`/v1/rule-releases/drafts/${bundleId}/publish`, command)

export const rollbackBundle = (bundleId: string, command: ReleaseCommand): Promise<ReleaseEvent> =>
  api.post(`/v1/rule-releases/drafts/${bundleId}/rollback`, command)

export const runBundleExtraction = (documentId: string, ruleBundleId: string, forceRefresh = false): Promise<Record<string, unknown>> =>
  api.post(`/v1/parse/documents/${documentId}/extract`, {
    rule_bundle_id: ruleBundleId,
    force_refresh: forceRefresh,
  })

export const compareRuns = (runId: string, otherRunId: string): Promise<Record<string, unknown>> =>
  api.get(`/v1/parse/runs/${runId}/compare`, { params: { other_run_id: otherRunId } })
