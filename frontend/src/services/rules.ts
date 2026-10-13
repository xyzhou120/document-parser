import api from './api'
import type {
  ChecklistType,
  ExtractionRule,
  ImportResult,
  RuleChangeLog,
  RuleMeta,
} from '@/types/api'

export const getRuleMeta = async (): Promise<RuleMeta> => api.get('/v1/rules/meta')

export const getChecklistTypes = async (onlyActive = false): Promise<ChecklistType[]> =>
  api.get('/v1/rules/checklist-types', { params: { only_active: onlyActive } })

export const createChecklistType = async (payload: {
  checklist_type: string
  checklist_name: string
  description?: string
  is_active?: boolean
  sort_order?: number
}): Promise<ChecklistType> => api.post('/v1/rules/checklist-types', payload)

export const patchChecklistType = async (
  checklistType: string,
  payload: Partial<Pick<ChecklistType, 'checklist_name' | 'description' | 'is_active' | 'sort_order'>>,
): Promise<ChecklistType> => api.patch(`/v1/rules/checklist-types/${checklistType}`, payload)

export const deleteChecklistType = async (checklistType: string) =>
  api.delete(`/v1/rules/checklist-types/${checklistType}`)

export const getRules = async (checklistType?: string): Promise<ExtractionRule[]> =>
  api.get('/v1/rules', { params: { checklist_type: checklistType } })

export type RuleDraft = Omit<ExtractionRule, 'rule_id' | 'version' | 'created_at' | 'updated_at'>

export const createRule = async (payload: RuleDraft): Promise<ExtractionRule> =>
  api.post('/v1/rules', payload)

export const updateRule = async (
  ruleId: string,
  payload: Partial<RuleDraft>,
): Promise<ExtractionRule> => api.patch(`/v1/rules/${ruleId}`, payload)

export const deleteRule = async (ruleId: string) => api.delete(`/v1/rules/${ruleId}`)

export const toggleRule = async (ruleId: string, enabled: boolean): Promise<ExtractionRule> =>
  api.post(`/v1/rules/${ruleId}/toggle`, { enabled })

export const importRulesFile = async (
  checklistType: string,
  file: File,
): Promise<ImportResult> => {
  const formData = new FormData()
  formData.append('checklist_type', checklistType)
  formData.append('file', file)
  return api.post('/v1/rules/import-file', formData)
}

export const seedRules = async (force = false): Promise<Record<string, unknown>> =>
  api.post('/v1/rules/seed', null, { params: { force } })

export const getChangeLog = async (checklistType?: string): Promise<RuleChangeLog[]> =>
  api.get('/v1/rules/change-log', { params: { checklist_type: checklistType, limit: 100 } })
