import api from './api'
import type { ExecutionJob, ExecutionResponse } from '@/types/api'

export const getExecution = (runId: string): Promise<ExecutionResponse> =>
  api.get(`/v1/parse/runs/${runId}/execution`, { params: { limit: 100 } })

export const getFieldJobs = (runId: string, cursor: string): Promise<{ jobs: ExecutionJob[]; next_cursor?: string | null }> =>
  api.get(`/v1/parse/runs/${runId}/field-jobs`, { params: { cursor, limit: 100 } })

export const controlExecution = (runId: string, action: 'resume' | 'pause' | 'cancel', retryFailed = false): Promise<{ status: string; execution_generation: number }> =>
  api.post(`/v1/parse/runs/${runId}/${action}`, undefined, { params: { retry_failed: retryFailed } })

export const startReliableExtraction = (documentId: string, parentRunId: string, requestId: string): Promise<{ parse_run_id?: string; build_id?: string; status: string }> =>
  api.post(`/v1/parse/documents/${documentId}/reliable-extract`, { parent_run_id: parentRunId, request_id: requestId })

export interface ContentBuildStatus {
  build_id: string
  document_id: string
  phase: string
  dispatch_state: string
  parse_run_id?: string | null
  content_revision_id?: string | null
  status: 'PREPARING_CONTENT' | 'CONTENT_READY' | string
  last_error?: string | null
  last_wait_reason?: string | null
}

export const getContentBuildStatus = (documentId: string, buildId: string): Promise<ContentBuildStatus> =>
  api.get(`/v1/parse/documents/${documentId}/content-status`, { params: { build_id: buildId } })

export const resumeContentBuild = (documentId: string, buildId: string): Promise<{ build_id: string; status: string; parse_run_id?: string | null; task_id?: string | null }> =>
  api.post(`/v1/parse/documents/${documentId}/content-build/${buildId}/resume`)

export const executionStatusNames: Record<string, string> = {
  QUEUED: '等待调度', PENDING: '等待处理', RUNNING: '正在处理', PROCESSING: '正在处理',
  RETRYING: '等待重试', COMPLETED: '全部规则已完成', PARTIAL: '部分规则尚未完成',
  WAITING_QUOTA: '等待外部额度', WAITING_SERVICE: '等待服务恢复', PAUSED: '已暂停',
  WAITING_EXTERNAL_QUOTA: '等待外部额度', YIELDED: '内部轮转，自动续排', PLANNED: '规则已规划',
  COMPLETED_WITH_REVIEW: '执行结束，仍需人工核对',
  CANCELLED: '已取消', FAILED: '执行失败', SUPERSEDED: '源文已更新',
}

export const executionStateNames: Record<string, string> = {
  queued: '等待调度', running: '执行中', yielded: '内部轮转，自动续排',
  waiting_quota: '等待外部额度', waiting_service: '等待服务恢复', paused: '已暂停',
  waiting_external_quota: '等待外部额度', failed_terminal: '执行失败，需处理',
  failed: '执行失败', cancelled: '已取消', superseded: '源文已更新', done: '执行结束',
}

export const semanticStateNames: Record<string, string> = {
  pending: '尚未完成核验', completed: '范围内提取已完成', not_found: '范围内未找到（检索已闭合）',
  partial_candidates: '仍有候选待核验', incomplete: '范围未处理完', review_required: '需要人工核对',
}
