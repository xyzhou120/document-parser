import api from './api'
import type { UploadResponse, ParseResultResponse, PaginatedResponse, ParseRun, FieldConfig, FactViewResponse } from '@/types/api'
export type ExtractionEngine = 'reliable' | 'checklist_agent' | 'rules'
export interface UploadParams { file: File; mine_id: string; mine_name: string; checklist_type: string; reporting_period: string; extraction_engine: ExtractionEngine }
export interface UploadSubmissionResponse {
  parse_run_id?: string | null
  document_id: string
  build_id?: string | null
  status: string
  message: string
  contract_version?: string | null
  extraction_engine?: ExtractionEngine | string | null
  artifact_id?: string | null
  parent_parse_run_id?: string | null
  mode?: string | null
  cache_status?: string | null
}
export interface ParseCapabilities {
  reliable_enabled: boolean
  assistant_enabled: boolean
  default_extraction_engine: ExtractionEngine | string
  extraction_engines: string[]
}
export interface QueryParseResultsParams { mine_name?: string; checklist_type?: string; status?: string; page?: number; page_size?: number }
export const uploadAndParse = async (params: UploadParams): Promise<UploadSubmissionResponse> => {
  const formData = new FormData()
  Object.entries(params).forEach(([key, value]) => formData.append(key, value))
  return api.post('/v1/parse/upload-and-parse', formData)
}
export const getParseCapabilities = (): Promise<ParseCapabilities> => api.get('/v1/parse/capabilities')
export const getParseRun = async (id: string): Promise<ParseResultResponse> => api.get(`/v1/parse/runs/${id}`)
export const getRunFacts = async (id: string, view: 'automatic' | 'reviewed' = 'automatic'): Promise<FactViewResponse> =>
  api.get(`/v1/parse/runs/${id}/facts`, { params: { view } })
export const exportRunFacts = async (id: string, view: 'automatic' | 'reviewed', format: 'json' | 'csv'): Promise<Blob> =>
  api.get(`/v1/parse/runs/${id}/facts/export`, { params: { view, format }, responseType: 'blob' })
export interface ExtractDocumentParams { field_keys?: string[]; force_refresh?: boolean; extraction_engine?: 'rules' | 'checklist_agent'; rule_bundle_id?: string; agent_limits?: Record<string, number> }
export const resumeDocument = async (id: string, params: ExtractDocumentParams = {}): Promise<UploadResponse> =>
  api.post(`/v1/parse/documents/${id}/resume`, params)
export const extractDocument = async (id: string, params: ExtractDocumentParams = {}): Promise<UploadResponse> =>
  api.post(`/v1/parse/documents/${id}/extract`, params)
export const queryParseResults = async (params: QueryParseResultsParams): Promise<PaginatedResponse<ParseRun>> => api.get('/v1/parse/results', { params })
export const getFieldRules = async (): Promise<FieldConfig[]> => api.get('/v1/parse/rules')
export const getDocumentFile = (id: string) => `${import.meta.env.VITE_API_BASE_URL || '/api'}/v1/parse/documents/${id}/file`
