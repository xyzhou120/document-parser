import api from './api'
import type { AssistantCitation, AssistantMessage, AssistantStatus, FactViewResponse } from '@/types/api'

export interface AssistantSession {
  session_id: string
  conversation_id: string
  document_id: string
  revision_id: string
  generation_id: string
  status: string
}

export interface AssistantLocations {
  items?: AssistantCitation[]
  locations?: AssistantCitation[]
  citations?: AssistantCitation[]
  next_cursor?: string | null
  search_finished: boolean
  remaining_work?: boolean | Record<string, unknown> | null
}

export const getAssistantStatus = (documentId: string): Promise<AssistantStatus> => api.get(`/v1/documents/${documentId}/assistant/status`)
export const createAssistantSession = (documentId: string, requestId: string): Promise<AssistantSession> =>
  api.post(`/v1/documents/${documentId}/assistant/sessions`, { request_id: requestId })
export const askAssistant = (sessionId: string, input: { request_id: string; question: string; mode: 'answer' | 'all_locations'; parent_message_id?: string }): Promise<AssistantMessage> =>
  api.post(`/v1/assistant/sessions/${sessionId}/messages`, input)
export const getAssistantMessage = (id: string): Promise<AssistantMessage> => api.get(`/v1/assistant/messages/${id}`)
export const getAssistantLocations = (id: string, cursor?: string | null): Promise<AssistantLocations> =>
  api.get(`/v1/assistant/messages/${id}/locations`, { params: { cursor: cursor || undefined, limit: 30 } })
export const resumeAssistantMessage = (id: string): Promise<AssistantMessage> => api.post(`/v1/assistant/messages/${id}/resume`)

export interface DocumentCard {
  card_id?: string | null
  document_id: string
  revision_id?: string | null
  generation_id?: string | null
  status: string
  summary?: string | null
  outline?: { items?: Array<{ title: string; page_number?: number; citation_ids?: string[] }>; summary_items?: Array<{ text: string; citation_ids?: string[] }> }
  topics?: Array<{ text: string; citation_ids?: string[] }>
  citations?: AssistantCitation[]
  risks?: string[]
  error?: Record<string, unknown> | null
}

export interface AssistantComparison {
  version_mismatch: boolean
  warnings: string[]
  writes_facts: false
  source_answer: AssistantMessage & { source: string }
  fact_view: FactViewResponse
}

export interface AssistantFeedback {
  request_id: string
  expected_correction: { type: string; text: string }
  reason: string
  submitted_by: string
  parse_run_id?: string
  field_key?: string
  locator?: { page_number: number; block_id?: string; citation_id?: string; bbox?: number[]; coordinate_system?: 'PDF_POINT' | 'PIXEL' | 'NORMALIZED'; quote?: string }
}

export const getDocumentCard = (id: string, versions: { revision_id?: string | null; generation_id?: string | null }): Promise<DocumentCard> =>
  api.get(`/v1/documents/${id}/assistant/card`, { params: versions })
export const requestDocumentCard = (id: string, input: { request_id: string; revision_id?: string | null; generation_id?: string | null }): Promise<DocumentCard> =>
  api.post(`/v1/documents/${id}/assistant/card`, input)
export const compareAssistantMessage = (id: string, input: { parse_run_id: string; view: 'automatic' | 'human' }): Promise<AssistantComparison> =>
  api.post(`/v1/assistant/messages/${id}/compare`, input)
export const submitAssistantFeedback = (id: string, input: AssistantFeedback): Promise<{ feedback_id: string; adjudication_status: string }> =>
  api.post(`/v1/assistant/messages/${id}/feedback`, input)
