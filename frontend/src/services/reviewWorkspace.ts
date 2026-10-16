import api from './api'

export type ReviewState = 'UNREVIEWED' | 'HUMAN_REVIEWED' | 'REJECTED'
export interface ReviewWorkspaceItem {
  parse_run_id: string
  document_id: string
  file_name: string
  mine_name?: string | null
  checklist_type?: string | null
  reporting_period?: string | null
  run_status: string
  created_at?: string | null
  field_key: string
  field_name: string
  field_value?: string | null
  required: boolean
  target_type?: string
  target_status?: string
  status_reason?: Record<string, unknown>
  validation_status?: string | null
  fact_count: number
  risk_codes: string[]
  risk_count: number
  latest_review?: {
    review_id: string
    revision: number
    action: string
    review_status: ReviewState
    reviewer: string
    reason: string
    facts_count: number
    created_at?: string | null
  } | null
  result_url: string
}

export interface ReviewWorkspaceQuery {
  state: ReviewState
  checklist_type?: string
  mine_name?: string
  page?: number
  page_size?: number
}

export const getReviewWorkspace = async (params: ReviewWorkspaceQuery): Promise<{
  total: number; page: number; page_size: number; items: ReviewWorkspaceItem[]
}> => api.get('/v1/review-workspace', { params })

export const getReviewAudit = async (runId: string): Promise<Record<string, unknown>> =>
  api.get(`/v1/reviews/runs/${runId}/export`)
