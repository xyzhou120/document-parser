import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Space, Switch, Table, Tag, Upload, message } from 'antd'
import { DownloadOutlined, EditOutlined, ImportOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { ChecklistType } from '@/types/api'
import { apiError } from '@/services/api'
import { createChecklistType, getChecklistTypes, getRules, importRulesFile, patchChecklistType } from '@/services/rules'

interface Draft {
  checklist_type: string
  checklist_name: string
  description?: string
  sort_order: number
  is_active: boolean
}

export default function ChecklistManagement() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<ChecklistType[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<ChecklistType | null>()
  const [form] = Form.useForm<Draft>()
  const [importing, setImporting] = useState<string>()
  const [importReport, setImportReport] = useState<string>()
  const load = async () => {
    setLoading(true); setError('')
    try { setRows(await getChecklistTypes()) }
    catch (failure) { setError(apiError(failure, '读取清单失败')) }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  const open = (row: ChecklistType | null) => {
    setEditing(row)
    form.setFieldsValue(row ? { ...row, description: row.description || '' } : {
      checklist_type: '', checklist_name: '', description: '', sort_order: 0, is_active: true,
    })
  }
  const save = async () => {
    let value: Draft
    try { value = await form.validateFields() } catch { return }
    if (!editing && rows.some((row) => row.checklist_type === value.checklist_type.trim())) {
      form.setFields([{ name: 'checklist_type', errors: ['该清单标识已存在，请编辑现有清单'] }]); return
    }
    setSaving(true)
    try {
      const payload = { checklist_name: value.checklist_name.trim(), description: value.description?.trim() || '', is_active: value.is_active, sort_order: value.sort_order }
      if (editing) await patchChecklistType(editing.checklist_type, payload)
      else await createChecklistType({ ...payload, checklist_type: value.checklist_type.trim() })
      setEditing(undefined); await load(); message.success('清单已保存')
    } catch (failure) { message.error(apiError(failure, '保存清单失败')) }
    finally { setSaving(false) }
  }
  const exportRules = async (row: ChecklistType) => {
    try {
      const rules = await getRules(row.checklist_type)
      const content = { checklist_type: row.checklist_type, checklist_name: row.checklist_name,
        exported_at: new Date().toISOString(), rules: rules.map((rule) => Object.fromEntries(Object.entries(rule).filter(([key]) => !['rule_id', 'version', 'created_at', 'updated_at'].includes(key)))) }
      const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = `规则-${row.checklist_type}.json`; link.click(); URL.revokeObjectURL(url)
    } catch (failure) { message.error(apiError(failure, '导出规则失败')) }
  }
  const importFile = async (row: ChecklistType, file: File) => {
    if (file.size > 5 * 1024 * 1024) { message.error('规则文件不能超过 5MB'); return }
    setImporting(row.checklist_type); setImportReport(undefined)
    try {
      const result = await importRulesFile(row.checklist_type, file)
      setImportReport(`${row.checklist_name}：新增 ${result.created}，更新 ${result.updated}，失败 ${result.failed.length}${result.failed.length ? `；${result.failed.map((item) => `${item.field_key || '未知字段'}：${item.reason}`).join('；')}` : ''}`)
      await load()
    } catch (failure) { setError(apiError(failure, '导入规则失败')) }
    finally { setImporting(undefined) }
  }
  return <div className="list-page">
    <div className="page-title-row"><div><h1>清单管理</h1><p>维护清单名称、说明、排序和启停状态。字段提取范围在规则管理中配置。</p></div>
      <Space><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => open(null)}>新增清单</Button></Space></div>
    <Alert type="info" showIcon message="JSON 导入按字段标识新增或更新当前清单规则，不重置示例数据。正式运行版本请在规则发布中审核与发布。" style={{ marginBottom: 16 }} />
    {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} style={{ marginBottom: 16 }} />}
    {importReport && <Alert type="info" showIcon message="导入结果" description={importReport} style={{ marginBottom: 16 }} />}
    <div className="list-surface"><Table rowKey="checklist_type" dataSource={rows} loading={loading} scroll={{ x: 1100 }} columns={[
      { title: '清单', key: 'name', render: (_, row: ChecklistType) => <><strong>{row.checklist_name}</strong><span className="table-subtitle">{row.checklist_type}</span></> },
      { title: '说明', dataIndex: 'description', render: (value?: string) => value || '—' },
      { title: '排序', dataIndex: 'sort_order', width: 70 },
      { title: '规则数', dataIndex: 'rule_count', width: 80 },
      { title: '状态', dataIndex: 'is_active', width: 90, render: (active: boolean) => <Tag color={active ? 'green' : 'default'}>{active ? '启用' : '停用'}</Tag> },
      { title: '操作', key: 'actions', width: 400, render: (_, row: ChecklistType) => <Space wrap>
        <Button size="small" icon={<EditOutlined />} onClick={() => open(row)}>编辑</Button>
        <Button size="small" onClick={() => navigate('/rules')}>字段规则</Button>
        <Button size="small" icon={<DownloadOutlined />} onClick={() => void exportRules(row)}>导出 JSON</Button>
        <Upload accept=".json,application/json" showUploadList={false} disabled={!!importing} beforeUpload={(file) => { void importFile(row, file); return false }}><Button size="small" loading={importing === row.checklist_type} icon={<ImportOutlined />}>导入 JSON</Button></Upload>
      </Space> },
    ]} pagination={false} locale={{ emptyText: '暂无清单，请新增清单后配置字段规则' }} /></div>
    <Modal title={editing ? '编辑清单' : '新增清单'} open={editing !== undefined} onCancel={() => setEditing(undefined)} onOk={() => void save()} confirmLoading={saving} destroyOnClose>
      <Form form={form} layout="vertical">
        <Form.Item name="checklist_type" label="清单标识" rules={[{ required: true, whitespace: true }, { pattern: /^[a-z][a-z0-9_]{1,99}$/, message: '以小写字母开头，仅含小写字母、数字与下划线，至少2个字符' }]}><Input disabled={!!editing} maxLength={100} /></Form.Item>
        <Form.Item name="checklist_name" label="清单名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={200} /></Form.Item>
        <Form.Item name="description" label="说明"><Input.TextArea rows={3} maxLength={4000} /></Form.Item>
        <Form.Item name="sort_order" label="排序" rules={[{ required: true }]}><InputNumber precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="is_active" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
  </div>
}
