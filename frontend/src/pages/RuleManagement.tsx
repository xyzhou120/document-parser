import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  ImportOutlined,
  PlusOutlined,
  ReloadOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import {
  createChecklistType,
  createRule,
  deleteRule,
  getChangeLog,
  getChecklistTypes,
  getRuleMeta,
  getRules,
  importRulesFile,
  seedRules,
  toggleRule,
  updateRule,
} from '@/services/rules'
import { apiError } from '@/services/api'
import type { ChecklistType, DimensionDefinition, ExtractionRule, RuleChangeLog, RuleMeta, RuleStrategy, RuleValidation } from '@/types/api'

const methodLabels: Record<string, string> = {
  keyword_search: '关键词检索',
  table_lookup: '表格查找',
  llm_extract: '模型提取',
  regex: '正则匹配',
  keyword: '关键词检索',
  table: '表格查找',
  llm: '模型提取',
  agent_extract: '清单知识包智能体',
}

const typeLabels: Record<string, string> = {
  string: '字符串',
  integer: '整数',
  float: '浮点数',
  date: '日期',
  array: '明细',
  json: 'JSON',
}

interface DraftState {
  original_strategy?: RuleStrategy
  original_validation?: RuleValidation | null
  rule_id?: string
  checklist_type: string
  field_key: string
  field_name_cn: string
  target_type: 'fact' | 'statement' | 'table'
  target_group_key: string
  target_group_name: string
  output_schema_json: string
  source_policy_json: string
  evidence_policy_json: string
  field_description?: string | null
  data_type: string
  unit?: string | null
  required: boolean
  multi_instance: boolean
  merge_strategy: string
  dimension_schema: Record<string, DimensionDefinition>
  value_kind: string
  dimension_bindings_json: string
  agent_background: string
  agent_instructions: string
  agent_synonyms: string[]
  agent_negative_examples: string[]
  agent_identity_dimensions: string[]
  agent_evidence_requirements: string[]
  agent_source_roles: string[]
  agent_manual_references: string[]
  methods: string[]
  keywords: string[]
  regex?: string | null
  regex_pattern: string
  regex_context_window: number
  context_window: number
  search_regions: string[]
  table_title_keywords: string[]
  header_keywords: string[]
  row_position: string
  prompt_template?: string | null
  context_scope: string
  temperature: number
  min?: number | null
  max?: number | null
  typical_low?: number | null
  typical_high?: number | null
  validation_regex?: string | null
  fallback_value?: string | null
  priority: number
  enabled: boolean
}

const emptyDraft = (checklistType: string): DraftState => ({
  checklist_type: checklistType,
  field_key: '',
  field_name_cn: '',
  target_type: 'fact',
  target_group_key: '',
  target_group_name: '',
  output_schema_json: '{}',
  source_policy_json: '{}',
  evidence_policy_json: '{}',
  field_description: '',
  data_type: 'string',
  unit: '',
  required: false,
  multi_instance: true,
  merge_strategy: 'value_consistent',
  dimension_schema: {},
  value_kind: 'unknown',
  dimension_bindings_json: '{}',
  agent_background: '',
  agent_instructions: '',
  agent_synonyms: [],
  agent_negative_examples: [],
  agent_identity_dimensions: [],
  agent_evidence_requirements: ['value', 'unit', 'object', 'scope'],
  agent_source_roles: [],
  agent_manual_references: [],
  methods: ['agent_extract'],
  keywords: [],
  regex: '',
  regex_pattern: '',
  regex_context_window: 50,
  context_window: 50,
  search_regions: ['all'],
  table_title_keywords: [],
  header_keywords: [],
  row_position: 'first_data_row',
  prompt_template: '',
  context_scope: 'keyword_window',
  temperature: 0.1,
  min: null,
  max: null,
  typical_low: null,
  typical_high: null,
  validation_regex: '',
  fallback_value: '',
  priority: 50,
  enabled: true,
})

function toDraft(rule: ExtractionRule): DraftState {
  const strategy = rule.strategy || {}
  const keyword = strategy.keyword_search || {}
  const table = strategy.table_lookup || {}
  const llm = strategy.llm_extract || {}
  const validation = rule.validation || {}
  const typical = validation.typical_range || []
  return {
    original_strategy: strategy,
    original_validation: rule.validation,
    rule_id: rule.rule_id,
    checklist_type: rule.checklist_type,
    field_key: rule.field_key,
    field_name_cn: rule.field_name_cn,
    target_type: rule.target_type || 'fact',
    target_group_key: rule.target_group_key || '',
    target_group_name: rule.target_group_name || '',
    output_schema_json: JSON.stringify(rule.output_schema || {}, null, 2),
    source_policy_json: JSON.stringify(rule.source_policy || {}, null, 2),
    evidence_policy_json: JSON.stringify(rule.evidence_policy || {}, null, 2),
    field_description: rule.field_description,
    data_type: rule.data_type,
    unit: rule.unit,
    required: rule.required,
    multi_instance: rule.multi_instance,
    merge_strategy: rule.merge_strategy,
    dimension_schema: rule.dimension_schema || {},
    value_kind: rule.value_kind || 'unknown',
    dimension_bindings_json: JSON.stringify(strategy.dimension_bindings || {}, null, 2),
    agent_background: strategy.agent_extract?.background || '',
    agent_instructions: strategy.agent_extract?.instructions || '',
    agent_synonyms: strategy.agent_extract?.synonyms || [],
    agent_negative_examples: strategy.agent_extract?.negative_examples || [],
    agent_identity_dimensions: strategy.agent_extract?.identity_dimensions || [],
    agent_evidence_requirements: strategy.agent_extract?.evidence_requirements || [],
    agent_source_roles: strategy.agent_extract?.source_roles || [],
    agent_manual_references: strategy.agent_extract?.manual_references || [],
    methods: strategy.methods || [],
    keywords: keyword.keywords || [],
    regex: keyword.regex || '',
    regex_pattern: strategy.regex?.pattern || '',
    regex_context_window: strategy.regex?.context_window ?? 50,
    context_window: keyword.context_window ?? 50,
    search_regions: keyword.search_regions || ['all'],
    table_title_keywords: table.table_title_keywords || [],
    header_keywords: table.header_keywords || [],
    row_position: table.row_position || 'first_data_row',
    prompt_template: llm.prompt_template || '',
    context_scope: llm.context_scope || 'keyword_window',
    temperature: llm.temperature ?? 0.1,
    min: validation.min ?? null,
    max: validation.max ?? null,
    typical_low: typical[0] ?? null,
    typical_high: typical[1] ?? null,
    validation_regex: validation.regex || '',
    fallback_value: rule.fallback_value || '',
    priority: rule.priority,
    enabled: rule.enabled,
  }
}

function fromDraft(draft: DraftState, dimensionBindings: RuleStrategy['dimension_bindings']): {
  payload: Record<string, unknown>
  strategy: RuleStrategy
} {
  const strategy: RuleStrategy = {
    ...draft.original_strategy,
    methods: draft.methods,
    dimension_bindings: dimensionBindings,
  }

  if (draft.methods.includes('agent_extract')) {
    strategy.agent_extract = {
      ...strategy.agent_extract,
      background: draft.agent_background,
      instructions: draft.agent_instructions,
      synonyms: draft.agent_synonyms,
      negative_examples: draft.agent_negative_examples,
      identity_dimensions: draft.agent_identity_dimensions,
      evidence_requirements: draft.agent_evidence_requirements,
      source_roles: draft.agent_source_roles,
      manual_references: draft.agent_manual_references,
    }
  }

  if (draft.methods.includes('keyword_search')) {
    strategy.keyword_search = {
      ...strategy.keyword_search,
      keywords: draft.keywords,
      regex: draft.regex || null,
      context_window: draft.context_window,
      search_regions: draft.search_regions,
    }
  }
  if (draft.methods.includes('table_lookup')) {
    strategy.table_lookup = {
      ...strategy.table_lookup,
      table_title_keywords: draft.table_title_keywords,
      header_keywords: draft.header_keywords,
      row_position: draft.row_position,
    }
  }
  if (draft.methods.includes('llm_extract')) {
    strategy.llm_extract = {
      ...strategy.llm_extract,
      prompt_template: draft.prompt_template || null,
      context_scope: draft.context_scope,
      temperature: draft.temperature,
    }
  }
  if (draft.methods.includes('regex')) {
    strategy.regex = {
      ...strategy.regex,
      pattern: draft.regex_pattern || null,
      context_window: draft.regex_context_window,
    }
  }

  const isNumeric = draft.data_type === 'float' || draft.data_type === 'integer'
  const validation: RuleValidation = { ...draft.original_validation }
  delete validation.regex
  if (isNumeric) {
    delete validation.min
    delete validation.max
    delete validation.typical_range
    if (draft.min != null) validation.min = draft.min
    if (draft.max != null) validation.max = draft.max
    if (draft.typical_low != null && draft.typical_high != null) {
      validation.typical_range = [draft.typical_low, draft.typical_high]
    }
  }
  if (draft.validation_regex) validation.regex = draft.validation_regex

  const payload = {
    checklist_type: draft.checklist_type,
    field_key: draft.field_key,
    field_name_cn: draft.field_name_cn,
    target_type: draft.target_type,
    target_group_key: draft.target_group_key.trim() || null,
    target_group_name: draft.target_group_name.trim() || null,
    output_schema: JSON.parse(draft.output_schema_json || '{}'),
    source_policy: JSON.parse(draft.source_policy_json || '{}'),
    evidence_policy: JSON.parse(draft.evidence_policy_json || '{}'),
    field_description: draft.field_description?.trim() || null,
    data_type: draft.data_type,
    unit: draft.unit?.trim() || null,
    required: draft.required,
    multi_instance: draft.multi_instance,
    merge_strategy: draft.merge_strategy,
    dimension_schema: draft.dimension_schema,
    value_kind: draft.value_kind,
    strategy,
    validation: Object.keys(validation).length ? validation : null,
    fallback_value: draft.fallback_value?.trim() || null,
    priority: draft.priority,
    enabled: draft.enabled,
  }

  return { payload, strategy }
}

type OutputSchema = { polarity?: string; columns?: { key: string; header?: string; unit?: string; required?: boolean }[]; row_identity?: string[] }

function outputSchema(draft: DraftState): OutputSchema {
  try {
    const value = JSON.parse(draft.output_schema_json || '{}')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch {
    return {}
  }
}

function tableColumnsText(draft: DraftState): string {
  return (outputSchema(draft).columns || []).filter((column: { key?: string; header?: string; unit?: string }) => column?.key && column?.header)
    .map((column: { key?: string; header?: string; unit?: string }) => `${column.key}=${column.header}${column.unit ? `:${column.unit}` : ''}`)
    .join('; ')
}

export default function RuleManagement() {
  const [types, setTypes] = useState<ChecklistType[]>([])
  const [selected, setSelected] = useState<string>()
  const [rules, setRules] = useState<ExtractionRule[]>([])
  const [meta, setMeta] = useState<RuleMeta>()
  const [logs, setLogs] = useState<RuleChangeLog[]>([])
  const [loading, setLoading] = useState(true)
  const [ruleLoading, setRuleLoading] = useState(false)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<DraftState>()
  const [saving, setSaving] = useState(false)
  const [newTypeOpen, setNewTypeOpen] = useState(false)
  const [typeForm] = Form.useForm()
  const [historyOpen, setHistoryOpen] = useState(false)
  const ruleRequest = useRef(0)
  const selectedType = useRef(selected)
  selectedType.current = selected

  const loadTypes = async (keepSelection = true) => {
    setLoading(true)
    setError('')
    try {
      const [typeList, ruleMeta] = await Promise.all([getChecklistTypes(), getRuleMeta()])
      setTypes(typeList)
      setMeta(ruleMeta)
      setSelected((current) => {
        if (keepSelection && current && typeList.some((t) => t.checklist_type === current)) {
          return current
        }
        return typeList[0]?.checklist_type
      })
    } catch (failure) {
      setError(apiError(failure, '读取清单类型失败，请确认后端服务已启动。'))
    } finally {
      setLoading(false)
    }
  }

  const loadRules = async (checklistType?: string) => {
    const request = ++ruleRequest.current
    setRules([])
    if (!checklistType) {
      setRuleLoading(false)
      return
    }
    setRuleLoading(true)
    try {
      const loaded = await getRules(checklistType)
      if (request === ruleRequest.current) { setRules(loaded); setError('') }
    } catch (failure) {
      if (request === ruleRequest.current) setError(apiError(failure, '读取规则失败。'))
    } finally {
      if (request === ruleRequest.current) setRuleLoading(false)
    }
  }

  useEffect(() => {
    void loadTypes(false)
  }, [])

  useEffect(() => {
    void loadRules(selected)
    return () => { ruleRequest.current += 1 }
  }, [selected])

  const refreshAll = async () => {
    await loadTypes()
    if (selectedType.current) {
      await loadRules(selectedType.current)
      const latest = await getChecklistTypes()
      setTypes(latest)
    }
  }

  const currentType = useMemo(
    () => types.find((t) => t.checklist_type === selected),
    [types, selected],
  )

  const handleSave = async () => {
    if (!draft) return
    if (!draft.field_key.trim() || !draft.field_name_cn.trim()) {
      message.warning('请填写字段标识与字段名称')
      return
    }
    if (!draft.methods.length) {
      message.warning('请至少选择一种提取方法')
      return
    }
    if (draft.methods.includes('regex') && !draft.regex_pattern.trim()) {
      message.warning('请填写正则匹配表达式')
      return
    }
    if (draft.agent_identity_dimensions.some((key) => !(key in draft.dimension_schema))) {
      message.warning('同值归并身份维度必须先添加到关联维度中')
      return
    }

    let dimensionBindings: RuleStrategy['dimension_bindings']
    try {
      const parsed: unknown = JSON.parse(draft.dimension_bindings_json || '{}')
      if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid bindings')
      if (Object.keys(parsed).some((key) => !(key in draft.dimension_schema))) {
        message.warning('维度绑定标识必须先添加到关联维度中')
        return
      }
      dimensionBindings = parsed as RuleStrategy['dimension_bindings']
    } catch {
      message.warning('维度绑定需要有效的 JSON 对象')
      return
    }

    for (const [label, value] of [
      ['输出结构', draft.output_schema_json],
      ['来源准入策略', draft.source_policy_json],
      ['证据要求', draft.evidence_policy_json],
    ] as const) {
      try {
        const parsed = JSON.parse(value || '{}')
        if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid')
      } catch {
        message.warning(`${label}需要有效的 JSON 对象`)
        return
      }
    }

    setSaving(true)
    try {
      const { payload } = fromDraft(draft, dimensionBindings)
      if (draft.rule_id) {
        await updateRule(draft.rule_id, payload)
        message.success('规则已更新')
      } else {
        await createRule(payload as never)
        message.success('规则已创建')
      }
      setDraft(undefined)
      await refreshAll()
    } catch (failure) {
      message.error(apiError(failure, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleToggle = async (rule: ExtractionRule, enabled: boolean) => {
    try {
      const updated = await toggleRule(rule.rule_id, enabled)
      setRules((list) => list.map((item) => (item.rule_id === rule.rule_id ? updated : item)))
      message.success(`已${enabled ? '启用' : '停用'}「${rule.field_name_cn}」`)
    } catch (failure) {
      message.error(apiError(failure, '操作失败'))
    }
  }

  const handleDelete = async (rule: ExtractionRule) => {
    try {
      await deleteRule(rule.rule_id)
      message.success(`已删除「${rule.field_name_cn}」`)
      await refreshAll()
    } catch (failure) {
      message.error(apiError(failure, '删除失败'))
    }
  }

  const handleSeed = async () => {
    setSaving(true)
    try {
      const result = await seedRules(false)
      message.success(
        `示例规则写入完成：新增 ${result.rules_created ?? 0} 条，跳过 ${result.rules_skipped ?? 0} 条`,
      )
      await refreshAll()
    } catch (failure) {
      message.error(apiError(failure, '写入示例规则失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleImport = async (file: File) => {
    if (!selected) {
      message.warning('请先选择清单类型')
      return false
    }
    try {
      const result = await importRulesFile(selected, file)
      message.success(`导入完成：新增 ${result.created} 条，更新 ${result.updated} 条`)
      if (result.failed?.length) {
        message.warning(`${result.failed.length} 条未导入`)
      }
      await refreshAll()
    } catch (failure) {
      message.error(apiError(failure, '导入失败'))
    }
    return false
  }

  const openHistory = async () => {
    setHistoryOpen(true)
    try {
      setLogs(await getChangeLog(selected))
    } catch (failure) {
      message.error(apiError(failure, '读取变更历史失败'))
    }
  }

  const createType = async () => {
    try {
      const values = await typeForm.validateFields()
      await createChecklistType({
        checklist_type: values.checklist_type,
        checklist_name: values.checklist_name,
        description: values.description,
        sort_order: values.sort_order ?? 100,
      })
      message.success('清单类型已创建')
      setNewTypeOpen(false)
      typeForm.resetFields()
      await loadTypes(false)
      setSelected(values.checklist_type)
    } catch (failure) {
      if ((failure as { errorFields?: unknown }).errorFields) return
      message.error(apiError(failure, '创建清单类型失败'))
    }
  }

  const renderCard = (rule: ExtractionRule) => {
    const strategy = rule.strategy || {}
    const methods = strategy.methods || []
    const keywords = strategy.keyword_search?.keywords || []
    const validation = rule.validation || {}
    const numeric = rule.data_type === 'float' || rule.data_type === 'integer'

    return (
      <article className={`rule-card ${rule.enabled ? '' : 'disabled'}`} key={rule.rule_id}>
        <div className="rule-card-head">
          <div className="rule-card-title">
            <h3>{rule.field_name_cn}</h3>
            <code>{rule.field_key}</code>
          </div>
          <div className="rule-card-badges">
            {rule.required && <Tag color="red">必填</Tag>}
            <Tag color={rule.target_type === 'table' ? 'cyan' : rule.target_type === 'statement' ? 'gold' : 'blue'}>
              {rule.target_type === 'table' ? '表格明细' : rule.target_type === 'statement' ? '原文内容' : '普通事实'}
            </Tag>
            {rule.target_group_name || rule.target_group_key ? <Tag>{rule.target_group_name || rule.target_group_key}</Tag> : null}
            <Tag>{typeLabels[rule.data_type] || rule.data_type}</Tag>
            {rule.unit && <Tag color="blue">{rule.unit}</Tag>}
            {rule.multi_instance && <Tag color="purple">多实例</Tag>}
            <Tooltip title={rule.enabled ? '停用规则' : '启用规则'}>
              <Switch
                size="small"
                checked={rule.enabled}
                onChange={(checked) => void handleToggle(rule, checked)}
              />
            </Tooltip>
          </div>
        </div>

        <div className="rule-card-body">
          <div className="rule-row">
            <span className="rule-row-label">提取方法</span>
            <span className="rule-row-value">
              {methods.length ? (
                methods.map((m) => (
                  <Tag key={m} color="geekblue">
                    {methodLabels[m] || m}
                  </Tag>
                ))
              ) : (
                <em>未配置</em>
              )}
            </span>
          </div>

          {keywords.length > 0 && (
            <div className="rule-row">
              <span className="rule-row-label">关键词</span>
              <span className="rule-row-value">
                {keywords.slice(0, 6).map((k) => (
                  <span className="soft-tag" key={k}>
                    {k}
                  </span>
                ))}
                {keywords.length > 6 && <em>等 {keywords.length} 个</em>}
              </span>
            </div>
          )}

          {strategy.table_lookup?.header_keywords?.length ? (
            <div className="rule-row">
              <span className="rule-row-label">表格特征</span>
              <span className="rule-row-value">
                {strategy.table_lookup.header_keywords.map((k) => (
                  <span className="soft-tag" key={k}>
                    {k}
                  </span>
                ))}
              </span>
            </div>
          ) : null}

          <div className="rule-row">
            <span className="rule-row-label">校验与兜底</span>
            <span className="rule-row-value">
              {numeric && (validation.min != null || validation.max != null) && (
                <span className="soft-tag">
                  区间 {validation.min ?? '不限'} – {validation.max ?? '不限'}
                </span>
              )}
              {validation.regex && <span className="soft-tag">正则校验</span>}
              <span className="soft-tag">旧合并配置（仅兼容）：{rule.merge_strategy}</span>
              {rule.fallback_value && <span className="soft-tag">兜底 {rule.fallback_value}</span>}
              {!numeric && !validation.regex && !rule.fallback_value && !rule.multi_instance && (
                <em>无</em>
              )}
            </span>
          </div>
          <div className="rule-row">
            <span className="rule-row-label">语义维度</span>
            <span className="rule-row-value">
              {Object.entries(rule.dimension_schema || {}).map(([key, definition]) => (
                <span className="soft-tag" key={key}>{key}{definition.required ? '（必填）' : ''}{definition.repeatable ? '（可重复）' : ''}{definition.parent_key ? ` ← ${definition.parent_key}` : ''}</span>
              ))}
              {!Object.keys(rule.dimension_schema || {}).length && <em>未指定维度</em>}
              <span className="soft-tag">值类型 {rule.value_kind || 'unknown'}</span>
            </span>
          </div>
        </div>

        <div className="rule-card-foot">
          <span className="rule-priority">优先级 {rule.priority} · v{rule.version}</span>
          <div className="rule-card-actions">
            <Button size="small" icon={<EditOutlined />} onClick={() => setDraft(toDraft(rule))}>
              编辑
            </Button>
            <Popconfirm
              title="确认删除该规则？"
              description="删除后该字段将不再参与提取。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => void handleDelete(rule)}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>
                删除
              </Button>
            </Popconfirm>
          </div>
        </div>
      </article>
    )
  }

  return (
    <div className="list-page rule-page">
      <div className="page-title-row">
        <div>
          <h1>规则管理</h1>
          <p>这里维护清单的提取目标。保存或启停后，后续新任务使用当前启用规则；已启动任务继续使用自己的规则快照。</p>
        </div>
        <div className="page-actions">
          <Button icon={<ReloadOutlined />} loading={loading || ruleLoading} onClick={() => void refreshAll()}>
            刷新
          </Button>
          <Button icon={<ExperimentOutlined />} loading={saving} onClick={() => void handleSeed()}>
            写入示例规则
          </Button>
          <Button icon={<HistoryIcon onClick={openHistory} />} onClick={openHistory}>
            变更历史
          </Button>
          <Upload
            accept=".csv,.json"
            showUploadList={false}
            beforeUpload={(file) => {
              void handleImport(file)
              return false
            }}
          >
            <Button icon={<ImportOutlined />} disabled={!selected}>
              从 CSV/JSON 导入
            </Button>
          </Upload>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!selected}
            onClick={() => setDraft(emptyDraft(selected || ''))}
          >
            新建规则
          </Button>
        </div>
      </div>

      {error && <Alert className="run-alert" type="error" showIcon message={error} />}

      {loading && !types.length ? (
        <div className="loading-state">
          <Spin size="large" />
        </div>
      ) : !types.length ? (
        <div className="panel empty-panel">
          <Empty description="还没有配置清单类型">
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setNewTypeOpen(true)}>
              新建清单类型
            </Button>
            <Button icon={<ExperimentOutlined />} onClick={() => void handleSeed()}>
              写入内置示例（3 类清单 / 12 条规则）
            </Button>
          </Empty>
        </div>
      ) : (
        <div className="rules-layout">
          <aside className="panel rule-types">
            <div className="panel-heading">
              <h2 className="panel-title">清单类型</h2>
              <Button type="text" size="small" icon={<PlusOutlined />} onClick={() => setNewTypeOpen(true)} />
            </div>
            {types.map((type) => (
              <button
                key={type.checklist_type}
                className={`rule-type ${selected === type.checklist_type ? 'active' : ''}`}
                onClick={() => setSelected(type.checklist_type)}
              >
                <span>{type.checklist_name}</span>
                <small>{type.rule_count}</small>
              </button>
            ))}
          </aside>

          <section className="panel rules-panel">
            <div className="panel-heading">
              <h2 className="panel-title">{currentType?.checklist_name || '规则'}</h2>
              <span className="muted">{rules.length} 条规则</span>
            </div>

            {currentType?.description && <p className="rule-type-desc">{currentType.description}</p>}

            {ruleLoading ? (
              <div className="loading-state">
                <Spin />
              </div>
            ) : rules.length ? (
              <div className="rule-list">{rules.map(renderCard)}</div>
            ) : (
              <div className="empty-panel">
                <Empty description="该清单还没有规则，先新建一条或从 CSV 导入">
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setDraft(emptyDraft(selected || ''))}
                  >
                    新建规则
                  </Button>
                </Empty>
              </div>
            )}
          </section>
        </div>
      )}

      {/* 规则编辑弹窗 */}
      <Modal
        open={!!draft}
        title={draft?.rule_id ? '编辑提取规则' : '新建提取规则'}
        width={860}
        onCancel={() => setDraft(undefined)}
        onOk={() => void handleSave()}
        confirmLoading={saving}
        okText="保存规则"
        cancelText="取消"
        destroyOnClose
      >
        {draft && (
          <div className="rule-editor">
            <section>
              <h4>基础信息</h4>
              <div className="editor-grid">
                <label>
                  <span>字段标识 *</span>
                  <Input
                    value={draft.field_key}
                    placeholder="如 residual_gas_threshold"
                    onChange={(e) => setDraft({ ...draft, field_key: e.target.value })}
                  />
                </label>
                <label>
                  <span>字段名称 *</span>
                  <Input
                    value={draft.field_name_cn}
                    placeholder="如 残余瓦斯含量临界值"
                    onChange={(e) => setDraft({ ...draft, field_name_cn: e.target.value })}
                  />
                </label>
                <label>
                  <span>目标类型</span>
                  <Select
                    value={draft.target_type}
                    onChange={(value) => {
                      const schema = value === 'statement' ? { polarity: 'any' } : value === 'table' ? { row_identity: [], columns: [] } : {}
                      setDraft({ ...draft, target_type: value, output_schema_json: JSON.stringify(schema, null, 2) })
                    }}
                    options={meta?.target_types || [
                      { value: 'fact', label: '普通事实' },
                      { value: 'statement', label: '原文内容' },
                      { value: 'table', label: '表格明细' },
                    ]}
                  />
                </label>
                <label>
                  <span>目标组标识</span>
                  <Input value={draft.target_group_key} placeholder="如 punching_design"
                    onChange={(e) => setDraft({ ...draft, target_group_key: e.target.value })} />
                </label>
                <label>
                  <span>目标组名称</span>
                  <Input value={draft.target_group_name} placeholder="如 冲孔措施设计"
                    onChange={(e) => setDraft({ ...draft, target_group_name: e.target.value })} />
                </label>
                <label>
                  <span>数据类型</span>
                  <Select
                    value={draft.data_type}
                    onChange={(value) => setDraft({ ...draft, data_type: value })}
                    options={meta?.data_types.map((item) => ({ value: item.value, label: item.label }))}
                  />
                </label>
                <label>
                  <span>单位</span>
                  <Input
                    value={draft.unit || ''}
                    placeholder="如 m³/t"
                    onChange={(e) => setDraft({ ...draft, unit: e.target.value })}
                  />
                </label>
                <label>
                  <span>优先级（0-100）</span>
                  <InputNumber
                    min={0}
                    max={100}
                    value={draft.priority}
                    onChange={(value) => setDraft({ ...draft, priority: value ?? 50 })}
                    style={{ width: '100%' }}
                  />
                </label>
                <label>
                  <span>值类型（按原文语义分类）</span>
                  <Select value={draft.value_kind} onChange={(value) => setDraft({ ...draft, value_kind: value })}
                    options={[
                      { value: 'unknown', label: '未指定' }, { value: 'original', label: '原始' },
                      { value: 'design', label: '设计' }, { value: 'actual', label: '实际' },
                      { value: 'calculated', label: '计算' }, { value: 'measured', label: '实测' },
                      { value: 'threshold', label: '阈值' }, { value: 'reported', label: '报告记录' },
                      { value: 'presence', label: '存在性原文' }, { value: 'process', label: '过程描述' },
                    ]} />
                </label>
                <label>
                  <span>缺失配置提示（不作为提取值）</span>
                  <Input
                    value={draft.fallback_value || ''}
                    placeholder="缺失时仅提示配置，不填入报告事实"
                    onChange={(e) => setDraft({ ...draft, fallback_value: e.target.value })}
                  />
                </label>
              </div>
              <label className="editor-full">
                <span>字段说明</span>
                <Input.TextArea
                  rows={2}
                  value={draft.field_description || ''}
                  placeholder="说明这个字段的业务含义与常见表述，供人工核对参考"
                  onChange={(e) => setDraft({ ...draft, field_description: e.target.value })}
                />
              </label>
              {draft.target_type === 'statement' && (
                <label className="editor-full">
                  <span>原文陈述类型</span>
                  <Select value={outputSchema(draft).polarity || 'any'} style={{ width: '100%' }}
                    options={[{ value: 'any', label: '肯定、否定和不确定都保留' },
                      { value: 'affirmed', label: '只提取肯定陈述' }, { value: 'negated', label: '只提取否定陈述' },
                      { value: 'uncertain', label: '只提取不确定陈述' }]}
                    onChange={(value) => setDraft({ ...draft, output_schema_json: JSON.stringify({ polarity: value }, null, 2) })} />
                  <small className="editor-hint">平台只返回原文及其语气，业务服务再据此判断。</small>
                </label>
              )}
              {draft.target_type === 'table' && (
                <>
                  <label className="editor-full">
                    <span>表格行身份列 *</span>
                    <Select mode="multiple" value={(outputSchema(draft).row_identity || []).filter((key: string) => (outputSchema(draft).columns || []).some((column: { key?: string }) => column.key === key))} style={{ width: '100%' }}
                      placeholder="先填写表格列，再选择其中能唯一标识一行的列"
                      options={(outputSchema(draft).columns || []).filter((column: { key?: string }) => column.key).map((column: { key: string; header?: string }) => ({ value: column.key, label: column.header ? `${column.key}（${column.header}）` : column.key }))}
                      onChange={(value) => setDraft({ ...draft, output_schema_json: JSON.stringify({ ...outputSchema(draft), row_identity: value }, null, 2) })} />
                  </label>
                  <label className="editor-full">
                    <span>表格列（标识=表头[:单位]，分号分隔） *</span>
                    <Input key={`${draft.field_key}-columns`} defaultValue={tableColumnsText(draft)} placeholder="point=测点; pressure=瓦斯压力:MPa; content=瓦斯含量:m³/t"
                      onBlur={(event) => {
                        const previous = outputSchema(draft).columns || []
                        const columns = event.target.value.split(';').map((part) => part.trim()).filter(Boolean).map((part) => {
                          const [left, ...unitParts] = part.split(':')
                          const [key, header] = left.split('=').map((item) => item.trim())
                          const kept: { unit?: string } = previous.find((column: { key?: string; unit?: string }) => column.key === key) || {}
                          return { ...kept, key, header, ...(unitParts.length ? { unit: unitParts.join(':').trim() } : { unit: kept.unit }) }
                        }).filter((column) => column.key && column.header)
                        const identity = (outputSchema(draft).row_identity || []).filter((key: string) => columns.some((column) => column.key === key))
                        setDraft({ ...draft, output_schema_json: JSON.stringify({ ...outputSchema(draft), columns, row_identity: identity }, null, 2) })
                      }} />
                    <small className="editor-hint">列缺失、行身份不清或必填单元格为空时，结果会标记为待复核。</small>
                  </label>
                </>
              )}
              <label className="editor-full">
                <span>输出结构 JSON（高级）</span>
                <Input.TextArea rows={3} value={draft.output_schema_json}
                  placeholder={draft.target_type === 'table' ? '{"row_identity":["measurement_point"],"columns":[]}' : '{}'}
                  onChange={(e) => setDraft({ ...draft, output_schema_json: e.target.value })} />
              </label>
              <label className="editor-full">
                <span>来源准入/排除 JSON</span>
                <Input.TextArea rows={3} value={draft.source_policy_json}
                  placeholder='{"allowed_roles":[],"excluded_roles":[],"exclude_terms":[],"required_headers":[]}'
                  onChange={(e) => setDraft({ ...draft, source_policy_json: e.target.value })} />
                <small className="editor-hint">提取时只认 allowed_roles、excluded_roles、exclude_terms、required_headers。写错键名不会生效。</small>
              </label>
              <label className="editor-full">
                <span>证据要求 JSON（尚未参与提取）</span>
                <Input.TextArea rows={3} value={draft.evidence_policy_json}
                  placeholder='{}'
                  onChange={(e) => setDraft({ ...draft, evidence_policy_json: e.target.value })} />
                <small className="editor-hint">目前只随规则保存并计入规则 hash，提取时还不会按这里拦截。</small>
              </label>
              <label className="editor-full">
                <span>关联维度（可自定义，回车添加）</span>
                <Select mode="tags" value={Object.keys(draft.dimension_schema)}
                  placeholder="按检查项选择，如 workface、evaluation_unit、measurement_point；可留空"
                  onChange={(values: string[]) => setDraft({ ...draft, dimension_schema: Object.fromEntries(
                    values.map((key) => [key.trim(), draft.dimension_schema[key] || { required: false }]).filter(([key]) => !!key),
                  ) })} />
              </label>
              <label className="editor-full">
                <span>必须绑定的维度</span>
                <Select mode="multiple" value={Object.entries(draft.dimension_schema).filter(([, definition]) => definition.required).map(([key]) => key)}
                  options={Object.keys(draft.dimension_schema).map((key) => ({ value: key, label: key }))}
                  onChange={(keys: string[]) => setDraft({ ...draft, dimension_schema: Object.fromEntries(
                    Object.keys(draft.dimension_schema).map((key) => [key, { ...draft.dimension_schema[key], required: keys.includes(key) }]),
                  ) })} />
              </label>
              <p className="editor-hint">维度按检查项配置，不默认绑定工作面。未发现维度证据时会保留实例并标记绑定未知；不得凭配置补造维度值。</p>
              {!!Object.keys(draft.dimension_schema).length && <div className="editor-full">
                <span>维度结构</span>
                {Object.entries(draft.dimension_schema).map(([key, definition]) => (
                  <div className="editor-switches" key={key}>
                    <code>{key}</code>
                    <Input size="small" placeholder="维度含义、取值范围及排除项" value={definition.description || ''}
                      style={{ minWidth: 260, flex: 1 }} onChange={(event) => setDraft({
                        ...draft,
                        dimension_schema: { ...draft.dimension_schema, [key]: { ...definition, description: event.target.value } },
                      })} />
                    <Checkbox checked={!!definition.repeatable} onChange={(event) => setDraft({
                      ...draft,
                      dimension_schema: { ...draft.dimension_schema, [key]: { ...definition, repeatable: event.target.checked } },
                    })}>允许重复</Checkbox>
                    <Select size="small" allowClear placeholder="父维度（可选）" value={definition.parent_key || undefined}
                      options={Object.keys(draft.dimension_schema).filter((candidate) => candidate !== key).map((candidate) => ({ value: candidate, label: candidate }))}
                      onChange={(value) => setDraft({
                        ...draft,
                        dimension_schema: { ...draft.dimension_schema, [key]: { ...definition, parent_key: value || null } },
                      })} />
                  </div>
                ))}
              </div>}
              <label className="editor-full">
                <span>维度提取绑定（高级 JSON）</span>
                <Input.TextArea rows={5} value={draft.dimension_bindings_json}
                  onChange={(event) => setDraft({ ...draft, dimension_bindings_json: event.target.value })} />
              </label>
              <p className="editor-hint">键名与关联维度一致；每项可配置 regex（从原文上下文提取）或 column_keywords（从表格列提取），例如 {JSON.stringify({ workface: { column_keywords: ['工作面'] } })}。仅添加维度标识不会凭空产生绑定信息。</p>
              <div className="editor-switches">
                <Checkbox
                  checked={draft.required}
                  onChange={(e) => setDraft({ ...draft, required: e.target.checked })}
                >
                  必填字段
                </Checkbox>
                <Checkbox
                  checked={draft.multi_instance}
                  onChange={(e) => setDraft({ ...draft, multi_instance: e.target.checked })}
                >
                  多实例配置（旧字段兼容）
                </Checkbox>
                <Checkbox
                  checked={draft.enabled}
                  onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
                >
                  启用规则
                </Checkbox>
                {draft.multi_instance && (
                  <label className="inline-select">
                    <span>旧合并策略（仅兼容）</span>
                    <Select
                      disabled
                      size="small"
                      value={draft.merge_strategy}
                      style={{ width: 150 }}
                      onChange={(value) => setDraft({ ...draft, merge_strategy: value })}
                      options={meta?.merge_strategies.map((item) => ({
                        value: item.value,
                        label: item.label,
                      }))}
                    />
                  </label>
                )}
              </div>
              <p className="editor-hint">相同业务身份和归一值可合并展示，并保留全部证据。不同值不自动选主值，旧合并策略不参与裁决。</p>
            </section>

            <section>
              <h4>提取策略（执行所选路径并保留全部出现位置）</h4>
              <div className="editor-switches">
                {meta?.methods
                  .filter((m) => ['agent_extract', 'table_lookup', 'keyword_search', 'llm_extract', 'regex'].includes(m.value))
                  .map((method) => (
                    <Checkbox
                      key={method.value}
                      checked={draft.methods.includes(method.value)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          methods: e.target.checked
                            ? [...draft.methods, method.value]
                            : draft.methods.filter((m) => m !== method.value),
                        })
                      }
                    >
                      {method.label}
                    </Checkbox>
                  ))}
              </div>

              {draft.methods.includes('agent_extract') && <div className="editor-sub">
                <h5>清单知识包</h5>
                <label className="editor-full"><span>提取背景</span><Input.TextArea rows={3} value={draft.agent_background}
                  placeholder="说明检查项背景、字段含义及常见来源；平台仅提取信息。"
                  onChange={(event) => setDraft({ ...draft, agent_background: event.target.value })} /></label>
                <label className="editor-full"><span>提取任务说明</span><Input.TextArea rows={4} value={draft.agent_instructions}
                  placeholder="明确要查找的内容、设计/实际等区别、需要查看的上下文；看不清时返回不可确认。"
                  onChange={(event) => setDraft({ ...draft, agent_instructions: event.target.value })} /></label>
                <label className="editor-full"><span>同义词与检索线索</span><Select mode="tags" value={draft.agent_synonyms}
                  onChange={(value) => setDraft({ ...draft, agent_synonyms: value })} /></label>
                <label className="editor-full"><span>容易混淆的反例</span><Select mode="tags" value={draft.agent_negative_examples}
                  placeholder="如：工程量 6030m 不是钻孔数；阈值不是实测值"
                  onChange={(value) => setDraft({ ...draft, agent_negative_examples: value })} /></label>
                <label className="editor-full"><span>同值归并身份维度</span><Select mode="multiple" value={draft.agent_identity_dimensions}
                  options={Object.keys(draft.dimension_schema).map((key) => ({ value: key, label: key }))}
                  onChange={(value) => setDraft({ ...draft, agent_identity_dimensions: value })} /></label>
                <label className="editor-full"><span>必须提供的证据</span><Select mode="multiple" value={draft.agent_evidence_requirements}
                  options={[['value', '字段值'], ['unit', '单位'], ['column_header', '列头'], ['row_header', '行头'], ['scope', '范围'], ['object', '所属对象'], ['value_kind', '值类型'], ['source_role', '来源角色']].map(([value, label]) => ({ value, label }))}
                  onChange={(value) => setDraft({ ...draft, agent_evidence_requirements: value })} /></label>
                <label className="editor-full"><span>允许的来源角色</span><Select mode="tags" value={draft.agent_source_roles}
                  placeholder="如 report_body、report_sheet、design、ledger"
                  onChange={(value) => setDraft({ ...draft, agent_source_roles: value })} /></label>
                <label className="editor-full"><span>规范/手册引用</span><Select mode="tags" value={draft.agent_manual_references}
                  placeholder="记录页码或制度条款，不作为报告事实"
                  onChange={(value) => setDraft({ ...draft, agent_manual_references: value })} /></label>
                <p className="editor-hint">知识包与字段、维度一同版本化。相同业务身份和归一值可归并，但保留多处证据；自动结果均未核验。</p>
              </div>}

              {draft.methods.includes('table_lookup') && (
                <div className="editor-sub">
                  <h5>表格查找</h5>
                  <label className="editor-full">
                    <span>表头关键词（逗号分隔）</span>
                    <Select
                      mode="tags"
                      value={draft.header_keywords}
                      placeholder="如 残余瓦斯, 临界值"
                      onChange={(value) => setDraft({ ...draft, header_keywords: value })}
                    />
                  </label>
                  <div className="editor-grid">
                    <label>
                      <span>表格标题关键词（逗号分隔）</span>
                      <Select
                        mode="tags"
                        value={draft.table_title_keywords}
                        placeholder="如 临界值, 考察结果"
                        onChange={(value) => setDraft({ ...draft, table_title_keywords: value })}
                      />
                    </label>
                    <label>
                      <span>取行位置</span>
                      <Select
                        value={draft.row_position}
                        onChange={(value) => setDraft({ ...draft, row_position: value })}
                        options={[
                          { value: 'first_data_row', label: '首行数据' },
                          { value: 'all_rows', label: '全部数据行' },
                        ]}
                      />
                    </label>
                  </div>
                </div>
              )}

              {draft.methods.includes('keyword_search') && (
                <div className="editor-sub">
                  <h5>关键词检索</h5>
                  <label className="editor-full">
                    <span>关键词（回车添加）</span>
                    <Select
                      mode="tags"
                      value={draft.keywords}
                      placeholder="如 残余瓦斯含量临界值"
                      onChange={(value) => setDraft({ ...draft, keywords: value })}
                    />
                  </label>
                  <label className="editor-full">
                    <span>正则表达式（可选，优先于关键词）</span>
                    <Input
                      value={draft.regex || ''}
                      placeholder={'如 残余瓦斯含量临界值[为是]?\\\\s*(\\\\d+\\\\.?\\\\d*)\\\\s*m³/t'}
                      onChange={(e) => setDraft({ ...draft, regex: e.target.value })}
                    />
                  </label>
                  <div className="editor-grid">
                    <label>
                      <span>上下文窗口（字符）</span>
                      <InputNumber
                        min={10}
                        max={500}
                        value={draft.context_window}
                        onChange={(value) => setDraft({ ...draft, context_window: value ?? 50 })}
                        style={{ width: '100%' }}
                      />
                    </label>
                    <label>
                      <span>检索范围</span>
                      <Select
                        mode="multiple"
                        value={draft.search_regions}
                        onChange={(value) => setDraft({ ...draft, search_regions: value })}
                        options={meta?.search_regions.map((item) => ({
                          value: item.value,
                          label: item.label,
                        }))}
                      />
                    </label>
                  </div>
                </div>
              )}

              {draft.methods.includes('regex') && (
                <div className="editor-sub">
                  <h5>正则匹配</h5>
                  <label className="editor-full">
                    <span>正则表达式 *</span>
                    <Input
                      value={draft.regex_pattern}
                      placeholder="如 钻孔数量[为是]?\\s*(\\d+)"
                      onChange={(e) => setDraft({ ...draft, regex_pattern: e.target.value })}
                    />
                  </label>
                  <label className="editor-full">
                    <span>上下文窗口（字符）</span>
                    <InputNumber
                      min={10}
                      max={500}
                      value={draft.regex_context_window}
                      onChange={(value) => setDraft({ ...draft, regex_context_window: value ?? 50 })}
                      style={{ width: '100%' }}
                    />
                  </label>
                </div>
              )}

              {draft.methods.includes('llm_extract') && (
                <div className="editor-sub">
                  <h5>模型提取</h5>
                  <label className="editor-full">
                    <span>提示词（{'{context}'} 会被替换为文档或关键词上下文）</span>
                    <Input.TextArea
                      rows={5}
                      value={draft.prompt_template || ''}
                      placeholder="留空则使用通用提示词。建议写明背景知识、输出 JSON 格式与未找到时的处理。"
                      onChange={(e) => setDraft({ ...draft, prompt_template: e.target.value })}
                    />
                  </label>
                  <div className="editor-grid">
                    <label>
                      <span>上下文范围</span>
                      <Select
                        value={draft.context_scope}
                        onChange={(value) => setDraft({ ...draft, context_scope: value })}
                        options={[
                          { value: 'keyword_window', label: '关键词窗口（快）' },
                          { value: 'full_document', label: '全文（慢，更全）' },
                        ]}
                      />
                    </label>
                    <label>
                      <span>temperature</span>
                      <InputNumber
                        min={0}
                        max={1}
                        step={0.05}
                        value={draft.temperature}
                        onChange={(value) => setDraft({ ...draft, temperature: value ?? 0.1 })}
                        style={{ width: '100%' }}
                      />
                    </label>
                  </div>
                </div>
              )}
            </section>

            <section>
              <h4>验证规则</h4>
              {(draft.data_type === 'float' || draft.data_type === 'integer') && (
                <div className="editor-grid editor-grid-four">
                  <label>
                    <span>最小值</span>
                    <InputNumber
                      value={draft.min}
                      onChange={(value) => setDraft({ ...draft, min: value })}
                      style={{ width: '100%' }}
                    />
                  </label>
                  <label>
                    <span>最大值</span>
                    <InputNumber
                      value={draft.max}
                      onChange={(value) => setDraft({ ...draft, max: value })}
                      style={{ width: '100%' }}
                    />
                  </label>
                  <label>
                    <span>典型区间 下限</span>
                    <InputNumber
                      value={draft.typical_low}
                      onChange={(value) => setDraft({ ...draft, typical_low: value })}
                      style={{ width: '100%' }}
                    />
                  </label>
                  <label>
                    <span>典型区间 上限</span>
                    <InputNumber
                      value={draft.typical_high}
                      onChange={(value) => setDraft({ ...draft, typical_high: value })}
                      style={{ width: '100%' }}
                    />
                  </label>
                </div>
              )}
              <label className="editor-full">
                <span>正则校验（可选）</span>
                <Input
                  value={draft.validation_regex || ''}
                  placeholder="如 ^[一-龥]{2,4}$"
                  onChange={(e) => setDraft({ ...draft, validation_regex: e.target.value })}
                />
              </label>
              <p className="editor-hint">
                校验配置仅产生提取质量提示；方法匹配分不代表取值正确率，平台不作抽采达标判定。
              </p>
            </section>
          </div>
        )}
      </Modal>

      {/* 新建清单类型 */}
      <Modal
        open={newTypeOpen}
        title="新建清单类型"
        onCancel={() => setNewTypeOpen(false)}
        onOk={() => void createType()}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={typeForm} layout="vertical" initialValues={{ sort_order: 100 }}>
          <Form.Item
            label="清单标识"
            name="checklist_type"
            rules={[
              { required: true, message: '请输入清单标识' },
              { pattern: /^[a-z][a-z0-9_]{1,99}$/, message: '小写字母开头，仅含小写字母、数字与下划线' },
            ]}
          >
            <Input placeholder="如 regional_prediction" />
          </Form.Item>
          <Form.Item label="清单名称" name="checklist_name" rules={[{ required: true, message: '请输入清单名称' }]}>
            <Input placeholder="如 区域预测临界值考察报告" />
          </Form.Item>
          <Form.Item label="说明" name="description">
            <Input.TextArea rows={2} placeholder="该清单的用途与关注字段" />
          </Form.Item>
          <Form.Item label="排序" name="sort_order">
            <InputNumber min={0} max={9999} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 变更历史 */}
      <Modal
        open={historyOpen}
        title={`规则变更历史${currentType ? ` · ${currentType.checklist_name}` : ''}`}
        width={760}
        footer={null}
        onCancel={() => setHistoryOpen(false)}
      >
        {logs.length ? (
          <div className="log-list">
            {logs.map((log) => (
              <div className="log-item" key={log.log_id}>
                <div className="log-head">
                  <Tag color="blue">{log.action}</Tag>
                  <span>{log.after_state?.field_name_cn as string || log.checklist_type}</span>
                  <time>{log.created_at ? dayjs(`${log.created_at}Z`).format('YYYY-MM-DD HH:mm:ss') : ''}</time>
                </div>
                <div className="log-body">
                  版本 {(log.before_state?.version as number) ?? '—'} → {(log.after_state?.version as number) ?? '—'}
                  {log.after_state?.enabled != null && ` · ${log.after_state.enabled ? '启用' : '停用'}`}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无变更记录" />
        )}
      </Modal>
    </div>
  )
}

/** 变更历史按钮内的图标占位，避免额外引入图标包 */
function HistoryIcon({ onClick }: { onClick: () => void }) {
  return <span onClick={onClick} style={{ display: 'none' }} />
}
