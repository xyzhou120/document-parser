import api from './api'
import type { DownstreamFact } from '@/types/api'

export interface ResultStats {
  targets: number; completed_search: number; found: number; available: number
  review_required: number; not_found: number; incomplete: number; mean_confidence: number | null
}
export interface ResultField {
  field_key: string; semantic_state: string; run_id: string; inherited_from_version_id?: string
  field?: { field_name?: string; field_value?: string; evidence?: unknown }
  facts: { automatic: DownstreamFact[]; reviewed: DownstreamFact[] }
}
export interface ResultVersion {
  id: string; number: number; operation: string; state: string; parent_id?: string; run_id?: string; build_id?: string
  actor: string; reason: string; created_at: string; completed_at?: string; last_error?: string
  stats?: ResultStats; duration_seconds?: number
  snapshot?: { fields: ResultField[]; stats: ResultStats; configuration: Record<string, unknown>
    metadata: Record<string, unknown>; content_revision_id: string; configuration_hash: string }
}
export interface Attachment {
  id: string; source_system: string; source_id: string; attachment_name: string; format: string
  mine_code: string; mine_name: string; file_name_id: string; file_name: string; report_time_raw: string
  period_type: string; period_key: string; normalization_status: string; update_rate: number; cycle: string
  checklist_type?: string; document_id?: string; status: string; download_state: string; last_error?: string
  created_at: string; updated_at: string; current?: ResultVersion; latest?: ResultVersion; current_validity: string
}
export interface ImportRow {
  id: string; sheet: string; row_number: number; state: string; errors: string[]; attachment_id?: string
  data: Record<string, string | number>
}
export interface ImportBatch {
  id: string; file_name: string; source_system: string; state: string; actor: string; created_at: string
  counts: Record<string, number>; mappings: Record<string, string>; last_error?: string
  summary?: Record<string, Record<string, number>>; total?: number; items?: ImportRow[]
}
export const attachmentStates: Record<string, string> = {
  REGISTERED: '已登记', WAITING_MAPPING: '待清单映射', START_BLOCKED: '启动受阻', READY: '已就绪',
  QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已留存', FAILED_RETRYABLE: '待重试', FAILED_TERMINAL: '获取失败',
  TYPE_UNSUPPORTED: '暂不支持解析', ID_CONTENT_CONFLICT: '附件 ID 与内容冲突', PAUSED: '已暂停', CANCELLED: '已取消',
  COMPLETED: '流程完成', FAILED: '失败', INCOMPLETE: '流程未完整', SUPERSEDED: '已被替代', PREPARING_CONTENT: '准备内容中',
  PREVIEW_QUEUED: '校验中', PREVIEW_READY: '待确认', PREVIEW_FAILED: '校验失败', REGISTERING: '登记中', ACCEPTED: '登记结束',
  VALID: '可登记', INVALID: '无效', SELECTED: '待登记', REUSED: '复用已有附件', CONFLICT: '业务冲突',
}
export const operationNames: Record<string, string> = {
  INITIAL_EXTRACT: '初提', INCREMENTAL_EXTRACT: '新增目标补提', FULL_REEXTRACT: '全量重提',
}
export const listAttachments = (params: Record<string, unknown>) => api.get<never, { total: number; items: Attachment[] }>('/v1/attachments', { params })
export const getAttachment = (id: string) => api.get<never, Attachment>(`/v1/attachments/${id}`)
export const getVersions = (id: string, page: number) => api.get<never, { total: number; current_version_id?: string; items: ResultVersion[] }>(`/v1/attachments/${id}/versions`, { params: { page } })
export const getVersion = (id: string, version: string) => api.get<never, ResultVersion>(`/v1/attachments/${id}/versions/${version}`)
export const startOperation = (id: string, operation: string, reason: string, request_id: string) => api.post<never, ResultVersion>(`/v1/attachments/${id}/operations`, { operation, reason, request_id })
export const controlAttachment = (id: string, action: string, reason: string) => api.post(`/v1/attachments/${id}/control`, { action, reason })
export const listImports = () => api.get<never, { total: number; items: ImportBatch[] }>('/v1/imports')
export const getImport = (id: string, params: Record<string, unknown>) => api.get<never, ImportBatch>(`/v1/imports/${id}`, { params })
export const uploadExcel = (file: File, source: string) => {
  const data = new FormData(); data.append('file', file); data.append('source_system', source)
  return api.post<never, ImportBatch>('/v1/imports/excel', data)
}
export const getImportGroups = (id: string, page: number) => api.get<never, { total: number; items: { file_name_id: string; file_name: string; count: number; checklist_type?: string }[] }>(`/v1/imports/${id}/groups`, { params: { page } })
export const saveMappings = (id: string, mappings: Record<string, string>) => api.post(`/v1/imports/${id}/mappings`, { mappings })
export const confirmImport = (id: string, row_ids?: string[]) => api.post(`/v1/imports/${id}/confirm`, row_ids ? { row_ids } : {})
export const controlImport = (id: string, action: string, file_name_id?: string) => api.post(`/v1/imports/${id}/control`, { action, file_name_id, reason: '导入批次运营操作' })
export async function downloadArtifact(path: string, name: string) {
  const blob = await api.get<never, Blob>(path, { responseType: 'blob' })
  const url = URL.createObjectURL(blob); const link = document.createElement('a')
  link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
