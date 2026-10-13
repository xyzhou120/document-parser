import api from './api'
import type {
  ChecklistDistributionItem,
  ConfidenceBucket,
  FieldQualityItem,
  ParseRun,
  ServiceOverview,
  StageDurationItem,
  StatsOverview,
  TrendResponse,
} from '@/types/api'

export const getOverview = async (): Promise<StatsOverview> => api.get('/v1/stats/overview')

export const getTrend = async (days = 7): Promise<TrendResponse> =>
  api.get('/v1/stats/trend', { params: { days } })

export const getChecklistDistribution = async (): Promise<{
  items: ChecklistDistributionItem[]
  total_documents: number
}> => api.get('/v1/stats/checklist-distribution')

export const getConfidenceDistribution = async (): Promise<{
  items: ConfidenceBucket[]
  total: number
}> => api.get('/v1/stats/confidence-distribution')

export const getFieldQuality = async (): Promise<{
  items: FieldQualityItem[]
  completed_runs: number
}> => api.get('/v1/stats/field-quality')

export const getStageDuration = async (): Promise<{
  stages: StageDurationItem[]
  avg_total_seconds: number | null
}> => api.get('/v1/stats/stage-duration')

export const getRecentRuns = async (limit = 8): Promise<{ items: ParseRun[] }> =>
  api.get('/v1/stats/recent-runs', { params: { limit } })

export const getServices = async (): Promise<ServiceOverview> => api.get('/v1/services')
