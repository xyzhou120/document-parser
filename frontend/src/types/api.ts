export interface FactEvidence {
  evidence_id?: string
  page_number?: number | null
  bbox?: number[] | { x0: number; y0: number; x1: number; y1: number } | null
  coordinate_system?: string | null
  coordinate_origin?: string | null
  coordinate_rotation?: number | null
  page_rotation?: number | null
  page_width?: number | null
  page_height?: number | null
  image_width?: number | null
  image_height?: number | null
  role?: string
  quote?: string | null
  scope?: string
  location_status?: string | null
  location_precision?: string | null
  status?: string
}

export interface DownstreamFact {
  fact_id: string
  field_key: string
  field_name: string
  raw: string
  raw_values: string[]
  normalized: string
  value: string
  unit: string | null
  dimensions: Record<string, unknown>
  value_kind: string
  source_role: string
  evidence: FactEvidence[]
  confidence: { score: number | null; kind: string; version: string; calibration_scope?: Record<string, unknown> | null }
  checks: Record<string, unknown>
  risk: Array<{ code: string; [key: string]: unknown }>
  coverage: Record<string, unknown>
  provenance: Record<string, unknown>
  available_for_downstream: boolean | null
  review_status: string
  field_review_status?: string
}

export interface FactViewResponse {
  contract_version: string
  parse_run_id: string
  document_id: string
  view: 'automatic' | 'reviewed'
  facts: DownstreamFact[]
  reviews: Array<{ field_key?: string; review_status?: string; revision?: number; [key: string]: unknown }>
  missing_fields: Array<Record<string, unknown>>
  warnings: string[]
  availability_policy: string
  availability_meaning: string
}

export interface ParseRun {
  parse_run_id: string
  document_id: string
  file_name: string
  mine_id?: string | null
  mine_name?: string | null
  checklist_type?: string
  reporting_period?: string
  status: string
  duration_seconds?: number | null
  fields_extracted?: number
  mean_confidence?: number | null
  error_message?: string
  created_at: string
}

export interface FieldInstance {
  raw_value?: string | number | null
  evidence_ids?: string[]
  evidence_roles?: Record<string, string[]>
  source_role?: string
  occurrence_id?: string
  value: string | null
  methods?: string[]
  page_num?: number | null
  context?: string
  unit?: string | null
  confidence?: number | null
  source?: string | null
  start_offset?: number | null
  end_offset?: number | null
  bbox?: { x0: number; y0: number; x1: number; y1: number } | null
  location_status?: 'unavailable' | 'text_offset' | 'text_block' | 'table_cell' | string | null
  coordinate_system?: string | null
  coordinate_origin?: string | null
  page_rotation?: number | null
  image_width?: number | null
  image_height?: number | null
  table_id?: string | null
  table_title?: string | null
  row_index?: number | null
  column_index?: number | null
  dimensions?: Record<string, { value?: string | null; evidence?: string | null } | string | null>
  value_kind?: 'original' | 'design' | 'actual' | 'calculated' | 'measured' | 'threshold' | 'reported' | 'unknown' | string | null
  binding_status?: 'bound' | 'unknown' | 'not_required' | string | null
  quality_status?: string | null
  quality_issues?: Array<{ code: string; [key: string]: unknown }>
}

export interface ExtractedField {
  field_key: string
  field_name: string
  field_value: string | null
  extraction_method?: string
  confidence?: number | null
  confidence_kind?: 'heuristic_uncalibrated' | string | null
  data_type?: string | null
  unit?: string | null
  required?: boolean
  validation_status?: string | null
  instances?: FieldInstance[] | null
  dimensions?: Record<string, { value?: string | null; evidence?: string | null } | string | null> | null
  value_kind?: FieldInstance['value_kind']
  quality_status?: string | null
  quality_issues?: Array<{ code: string; [key: string]: unknown }>
  extraction_detail?: ExtractionDetail | null
  evidence?: {
    page_number?: number
    quote?: string
    location_status?: string
    locator?: FactEvidence & { coordinate_system: string }
  } | null
}

export interface ExtractionDetail {
  evidence_registry?: Record<string, FactEvidence>
  value_groups?: Array<{ value: string | null; unit?: string | null; identity?: Record<string, unknown>; evidence_refs?: Array<string | { evidence_id?: string; page_number?: number; location_status?: string; bbox?: unknown }>; evidence_ids?: string[]; raw_values?: Array<string | number | null>; review_status?: string; status?: string }>
  source_review_status?: string
  extraction_review_status?: string
  unresolved?: unknown[]
  coverage?: Record<string, unknown>
  // Plan B/C (2026-10-10): stable reason codes and per-field candidate statistics.
  incomplete_reasons?: string[]
  retrieval?: {
    candidates_total?: number
    candidates_sent?: number
    dropped?: Record<string, number>
    strengths?: Record<string, number>
    unreadable_pages?: number[]
    calls?: number
    tokens?: number
    accepted?: number
    rejected?: number
    negative?: number
  } | null
  field_key?: string
  field_name?: string
  required?: boolean
  data_type?: string | null
  unit?: string | null
  validation_status?: string
  instances?: FieldInstance[]
  dimensions?: Record<string, { value?: string | null; evidence?: string | null } | string | null>
  value_kind?: FieldInstance['value_kind']
  fusion?: {
    status?: string
    candidate_groups?: { value: string; methods?: string[]; pages?: number[] }[]
    rejected_candidates?: { value: string; source?: string; page_num?: number; validation_status?: string }[]
  }
  rejected_candidates?: { value: string; source?: string; page_num?: number; validation_status?: string }[]
}

export interface MissingField extends ExtractionDetail {
  field_key: string
}

export interface ParseResultResponse {
  contract_version?: string | null
  content_revision_id?: string | null
  execution_generation?: number
  file_hash?: string | null
  parse_run_id: string
  document_id: string
  document_name?: string
  mine_name?: string
  checklist_type?: string
  reporting_period?: string
  status: string
  duration_seconds?: number | null
  fields_extracted: number
  mean_confidence?: number | null
  error_message?: string
  quality_status?: string | null
  extraction_profile?: Record<string, unknown> & { agent_runtime?: AgentRuntime; rules?: Array<{ field_key: string; dimension_schema?: Record<string, DimensionDefinition> }> } | null
  missing_fields?: MissingField[]
  warnings?: string[]
  fields: ExtractedField[]
  facts?: DownstreamFact[]
  artifact_id?: string | null
  artifact_status?: string | null
  parent_parse_run_id?: string | null
  mode?: string | null
  cache_status?: string | null
}

export interface AgentRuntime {
  engine?: string | null
  model?: string | null
  model_version?: string | null
  coverage?: { visited_pages?: number[]; viewed_pages?: number[]; unvisited_pages?: number[] }
  trace?: Array<{ action?: string; page?: number; [key: string]: unknown }>
  budget?: Record<string, unknown>
  token_usage?: Record<string, number>
  status?: string
  failures?: string[]
  field_coverage?: Record<string, unknown>
}

export interface UploadResponse {
  parse_run_id: string
  document_id: string
  status: string
  message: string
  artifact_id?: string | null
  parent_parse_run_id?: string | null
  mode?: string | null
  cache_status?: string | null
}

export interface ExecutionJob {
  job_id: string
  run_id: string
  field_key: string
  rule_hash: string
  revision_id: string
  execution_state: string
  semantic_state: string
  phase: string
  checkpoint?: Record<string, unknown>
  next_attempt_at?: string | null
  lease_until?: string | null
  fence_token: number
  progress_seq: number
  attempt: number
  last_error?: string | null
}

export interface ExecutionResponse {
  contract_version: string
  run_id: string
  document_id: string
  status: string
  execution_generation: number
  automatic_complete: boolean
  total_fields: number
  counts: Record<string, number>
  jobs: ExecutionJob[]
  next_cursor?: string | null
  legacy?: boolean
}

export interface AssistantCitation {
  citation_id?: string
  chunk_id?: string
  quote?: string
  page_number?: number | null
  block_id?: string
  bbox?: FactEvidence['bbox']
  coordinate_system?: string | null
  coordinate_origin?: string | null
  page_rotation?: number | null
  image_width?: number | null
  image_height?: number | null
  block_ref?: Record<string, unknown>
  table_ref?: Record<string, unknown> | null
  revision_id?: string
  generation_id?: string | null
  risk?: string | null
}

export interface AssistantMessage {
  message_id: string
  question?: string
  mode?: 'answer' | 'all_locations'
  answer?: string
  claims?: Array<Record<string, unknown>>
  citations?: AssistantCitation[]
  risks?: string[]
  revision?: string
  generation?: string
  session_id?: string
  conversation_id?: string
  revision_id?: string
  generation_id?: string
  execution_status?: string
  search_finished?: boolean
  range_stats?: Record<string, unknown>
  remaining_work?: boolean | Record<string, unknown> | null
  next_cursor?: string | null
  status?: string
  error?: string | Record<string, unknown> | null
}

export interface AssistantStatus {
  contract_version?: string
  eligible?: boolean
  can_ask: boolean
  coverage_status?: string
  semantic_status?: string
  answerer_status?: string
  status?: string
  reason?: string | null
  revision_id?: string | null
  generation_id?: string | null
  risks?: string[]
  capabilities?: { answer?: boolean; all_locations?: boolean; compare?: boolean; card?: boolean; feedback?: boolean }
}

export interface PaginatedResponse<T> { total: number; items: T[] }

/** 兼容旧接口的字段规则结构 */
export interface FieldConfig {
  field_key: string
  field_name: string
  field_type: string
  unit?: string
  pattern?: string
  extraction_methods?: string[]
}

// ============ 清单类型与规则 ============

export interface ChecklistType {
  checklist_type: string
  checklist_name: string
  description?: string | null
  is_active: boolean
  sort_order: number
  rule_count: number
  created_at?: string | null
  updated_at?: string | null
}

export interface KeywordSearchConfig {
  keywords?: string[]
  regex?: string | null
  context_window?: number
  search_regions?: string[]
}

export interface TableLookupConfig {
  table_title_keywords?: string[]
  header_keywords?: string[]
  row_position?: string
}

export interface LlmExtractConfig {
  prompt_template?: string | null
  temperature?: number
  max_tokens?: number
  context_scope?: string
}

export interface RegexConfig {
  pattern?: string | null
  context_window?: number
}

export interface RuleStrategy {
  methods?: string[]
  preferred_source?: string
  confidence_threshold?: number
  keyword_search?: KeywordSearchConfig
  table_lookup?: TableLookupConfig
  llm_extract?: LlmExtractConfig
  regex?: RegexConfig
  agent_extract?: AgentExtractConfig
  dimension_bindings?: Record<string, { regex?: string; column_keywords?: string[]; [key: string]: unknown }>
}

export interface AgentExtractConfig {
  background?: string
  instructions?: string
  synonyms?: string[]
  negative_examples?: string[]
  identity_dimensions?: string[]
  evidence_requirements?: string[]
  source_roles?: string[]
  manual_references?: string[]
}

export interface RuleValidation {
  min?: number | null
  max?: number | null
  typical_range?: number[] | null
  regex?: string | null
  blacklist?: string[] | null
}

export interface ExtractionRule {
  rule_id: string
  checklist_type: string
  field_key: string
  field_name_cn: string
  field_description?: string | null
  data_type: string
  unit?: string | null
  required: boolean
  multi_instance: boolean
  merge_strategy: string
  dimension_schema?: Record<string, DimensionDefinition> | null
  value_kind?: FieldInstance['value_kind']
  strategy: RuleStrategy
  validation?: RuleValidation | null
  fallback_value?: string | null
  priority: number
  enabled: boolean
  version: number
  created_at?: string | null
  updated_at?: string | null
}

export interface DimensionDefinition {
  description?: string
  required?: boolean
  repeatable?: boolean
  parent_key?: string | null
}

export interface RuleMeta {
  data_types: { value: string; label: string }[]
  methods: { value: string; label: string }[]
  merge_strategies: { value: string; label: string }[]
  search_regions: { value: string; label: string }[]
}

export interface RuleChangeLog {
  log_id: string
  rule_id?: string | null
  checklist_type?: string | null
  action: string
  before_state?: Record<string, unknown> | null
  after_state?: Record<string, unknown> | null
  operator?: string | null
  created_at?: string | null
}

export interface ImportResult {
  message: string
  created: number
  updated: number
  failed: { field_key?: string; reason: string }[]
}

// ============ 统计 ============

export interface StatsOverview {
  today_count: number
  today_trend_percent: number | null
  yesterday_count: number
  pending_count: number
  completed_count: number
  failed_count: number
  total_runs: number
  document_count: number
  avg_duration_seconds: number | null
  avg_confidence: number | null
  review_count: number
  success_rate: number | null
}

export interface TrendPoint {
  date: string
  total: number
  completed: number
  failed: number
  pending: number
}

export interface TrendResponse {
  days: number
  series: TrendPoint[]
}

export interface ChecklistDistributionItem {
  checklist_type: string
  documents: number
  runs: number
  completed: number
  avg_confidence: number | null
  avg_duration_seconds: number | null
}

export interface ConfidenceBucket {
  label: string
  count: number
  ratio: number
}

export interface FieldQualityItem {
  checklist_type?: string | null
  field_key: string
  field_name: string
  extracted: number
  candidate_found: number
  fact_count: number
  value_group_count: number
  available_for_downstream: number | null
  availability_unknown: number
  risk_count: number
  review_needed: number
  coverage: number | null
  avg_confidence: number | null
  min_confidence: number | null
}

export interface StageDurationItem {
  stage: string
  entries: number
  avg_duration_seconds?: number | null
}

// ============ 服务状态 ============

export interface ServiceStatus {
  name: string
  key: string
  status: string
  latency_ms: number | null
  detail: string
}

export interface ServiceOverview {
  services: ServiceStatus[]
  online_count: number
  total_count: number
  healthy: boolean
}
