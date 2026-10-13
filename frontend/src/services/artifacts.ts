import api from './api'

export interface SourceArtifact {
  artifact_id: string
  document_id: string
  file_hash: string
  status: string
  source_review_status: string
  parent_artifact_id?: string | null
  source_revision_id?: string | null
  created_at: string
}

export interface SourceRevision {
  revision_id: string
  document_id: string
  parent_artifact_id: string
  derived_artifact_id?: string | null
  parent_revision_id?: string | null
  revision_type: 'CORRECT' | 'CONFIRM' | 'UNREADABLE'
  page_number: number
  start_offset?: number | null
  end_offset?: number | null
  original_text?: string | null
  replacement_text?: string | null
  comment: string
  affected_fields: string[]
  source_review_status: string
  extraction_run_id?: string | null
  created_by: string
  created_at: string
}

export interface ArtifactHistory {
  artifacts: SourceArtifact[]
  revisions: SourceRevision[]
  latest_revision_id: string | null
}

export interface ArtifactPage {
  artifact_id: string
  file_hash: string
  source_review_status: string
  page: {
    page_number: number
    text: string
    width?: number
    height?: number
    rotation?: number
    page_rotation?: number
    coordinate_origin?: string
    source_unreadable?: boolean
    source_review_status?: string
  }
}

export interface SourceRevisionPayload {
  document_id: string
  parent_artifact_id: string
  expected_parent_revision_id: string | null
  expected_file_hash: string
  page_number: number
  revision_type: SourceRevision['revision_type']
  start_offset?: number
  end_offset?: number
  original_text?: string
  replacement_text?: string
  bbox?: { x0: number; y0: number; x1: number; y1: number }
  comment: string
  created_by: string
  start_extraction: boolean
  parent_run_id?: string
  rule_bundle_id?: string
}

export interface SourcePreview {
  mode: string
  dependency_precision: string
  affected_fields: string[]
  source_review_status: string
  raw_facts_status: string
  original_text?: string
  derived_text?: string
  invalidated_structures: string[]
  extraction_available: boolean
}

export interface SourceRun {
  parse_run_id: string
  status: string
  error_message?: string | null
  artifact_id?: string
  mode?: string
}

const root = '/v1/artifacts'
export const getArtifactHistory = async (documentId: string): Promise<ArtifactHistory> =>
  api.get(`${root}/documents/${documentId}/history`)
export const getArtifactPage = async (artifactId: string, pageNumber: number): Promise<ArtifactPage> =>
  api.get(`${root}/${artifactId}/pages/${pageNumber}`)
export const previewSourceRevision = async (payload: SourceRevisionPayload): Promise<SourcePreview> =>
  api.post(`${root}/source-revisions/preview`, payload)
export const createSourceRevision = async (payload: SourceRevisionPayload): Promise<{ revision: SourceRevision; run: SourceRun | null }> =>
  api.post(`${root}/source-revisions`, payload)
export const extractSourceRevision = async (revisionId: string, payload: { request_id: string; parent_run_id?: string; rule_bundle_id?: string }): Promise<SourceRun> =>
  api.post(`${root}/source-revisions/${revisionId}/extract`, payload)
export const retrySourceExtraction = async (revisionId: string): Promise<SourceRun> =>
  api.post(`${root}/source-revisions/${revisionId}/retry`)
