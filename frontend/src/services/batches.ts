import api from './api'

export interface BatchItemInput {
  client_key: string
  file_name: string
  file_hash?: string
  mine_id: string
  mine_name: string
  checklist_type: string
  reporting_period: string
  extraction_engine?: string
}
export interface BatchItem {
  client_key: string
  file_name: string
  file_hash?: string | null
  metadata: Omit<BatchItemInput, 'client_key' | 'file_name' | 'file_hash'>
  item_id: string
  document_id?: string | null
  parse_run_id?: string | null
  status: string
  version: number
  attempts: number
  error_message?: string | null
}
export interface BatchSummary {
  batch_id: string
  name: string
  version: number
  status: string
  total: number
  completed: number
  failed: number
  cancelled: number
  progress_percent: number
  items?: BatchItem[]
}
export const createBatch = (payload: { name: string; items: BatchItemInput[] }): Promise<BatchSummary> => api.post('/v1/batches', payload)
export const listBatches = (params?: { page?: number; page_size?: number }): Promise<{ total: number; items: BatchSummary[] }> => api.get('/v1/batches', { params })
export const getBatch = (id: string): Promise<BatchSummary> => api.get(`/v1/batches/${id}`)
export const uploadBatchItem = (batchId: string, item: BatchItem, file: File) => {
  const data = new FormData()
  data.append('version', String(item.version))
  data.append('file', file)
  return api.post(`/v1/batches/${batchId}/items/${item.item_id}/upload`, data)
}
export const retryBatchItem = (batchId: string, item: BatchItem) => api.post(`/v1/batches/${batchId}/items/${item.item_id}/retry`, { version: item.version })
export const cancelBatchItem = (batchId: string, item: BatchItem) => api.post(`/v1/batches/${batchId}/items/${item.item_id}/cancel`, { version: item.version })
export const batchExportUrl = (params: Record<string, string | undefined>, format: 'json' | 'csv' = 'csv') => {
  const query = new URLSearchParams({ ...params, format } as Record<string, string>)
  return `${import.meta.env.VITE_API_BASE_URL || '/api'}/v1/batches/export?${query}`
}
