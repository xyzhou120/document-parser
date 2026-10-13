import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, Select, Table, Tag, Upload } from 'antd'
import type { UploadFile, UploadProps } from 'antd'
import { CloudUploadOutlined, SendOutlined, UnorderedListOutlined, SlidersOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import dayjs from 'dayjs'
import ParseWorkspace from '@/components/ParseWorkspace'
import { getParseCapabilities, uploadAndParse } from '@/services/parse'
import type { ParseCapabilities, UploadParams, UploadSubmissionResponse } from '@/services/parse'
import { getChecklistTypes } from '@/services/rules'
import { apiError } from '@/services/api'
import type { ChecklistType } from '@/types/api'

interface BatchFile {
  id: string
  file: File
  mine_id: string
  mine_name: string
  checklist_type: string
  reporting_period: string
  status: 'pending' | 'submitting' | 'submitted' | 'failed'
  run_id?: string | null
  document_id?: string
  build_id?: string | null
  extraction_engine?: string
  error?: string
}

export default function DocumentUpload() {
  const navigate = useNavigate()
  const [file, setFile] = useState<File>()
  const [files, setFiles] = useState<BatchFile[]>([])
  const [url, setUrl] = useState<string>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [types, setTypes] = useState<ChecklistType[]>([])
  const [capabilities, setCapabilities] = useState<ParseCapabilities>()
  const [capabilityError, setCapabilityError] = useState('')
  const [capabilityRefresh, setCapabilityRefresh] = useState(0)
  const [form] = Form.useForm()

  // 清单类型来自数据库配置，而不是写死在表单里
  useEffect(() => {
    getChecklistTypes(true)
      .then((list) => {
        setTypes(list)
        if (list.length && !form.getFieldValue('checklist_type')) {
          form.setFieldValue('checklist_type', list[0].checklist_type)
        }
      })
      .catch(() => setTypes([]))
  }, [form])

  useEffect(() => {
    let disposed = false
    setCapabilityError(''); setCapabilities(undefined)
    getParseCapabilities().then((value) => {
      if (disposed) return
      if (!value.extraction_engines.includes(value.default_extraction_engine)) throw new Error('当前默认引擎不在可用引擎列表中。')
      setCapabilities(value)
      form.setFieldValue('extraction_engine', value.default_extraction_engine)
    }).catch((failure) => { if (!disposed) setCapabilityError(apiError(failure, '无法读取提取能力配置，请确认服务状态后再选择引擎。')) })
    return () => { disposed = true }
  }, [form, capabilityRefresh])

  useEffect(() => {
    if (!file) {
      setUrl(undefined)
      return
    }
    const localUrl = URL.createObjectURL(file)
    setUrl(localUrl)
    return () => URL.revokeObjectURL(localUrl)
  }, [file])

  const uploadProps: UploadProps = {
    accept: '.pdf',
    multiple: true,
    disabled: loading,
    fileList: files.map((item) => ({ uid: item.id, name: item.file.name, size: item.file.size, originFileObj: item.file } as UploadFile)),
    beforeUpload: (selected, incoming) => {
      if (files.length + incoming.indexOf(selected) >= 20) {
        setError('每批最多选择 20 份文件，请分批提交。')
        return Upload.LIST_IGNORE
      }
      if (!selected.name.toLowerCase().endsWith('.pdf')) {
        setError('请选择 PDF 文档。')
        return Upload.LIST_IGNORE
      }
      if (selected.size > 100 * 1024 * 1024) {
        setError('文件不能超过 100MB。')
        return Upload.LIST_IGNORE
      }
      const id = selected.uid
      setError('')
      setFiles((current) => {
        if (current.length >= 20) return current
        return [...current, { id, file: selected, mine_id: '', mine_name: '',
          checklist_type: form.getFieldValue('checklist_type') || '',
          reporting_period: form.getFieldValue('reporting_period') || dayjs().format('YYYY-MM'), status: 'pending' }]
      })
      setFile((current) => current || selected)
      return false
    },
    onRemove: (removed) => {
      const item = files.find((entry) => entry.id === removed.uid)
      if (item?.status === 'submitted') return false
      const remaining = files.filter((entry) => entry.id !== removed.uid)
      setFiles(remaining)
      setFile(remaining[0]?.file)
      setError('')
    },
  }

  const updateFile = (id: string, patch: Partial<BatchFile>) => setFiles((current) => current.map((item) => item.id === id ? { ...item, ...patch } : item))
  const applyTemplate = () => {
    const values = form.getFieldsValue()
    setFiles((current) => current.map((item) => item.status === 'submitted' ? item : { ...item,
      mine_id: values.mine_id || '', mine_name: values.mine_name || '',
      checklist_type: values.checklist_type || '', reporting_period: values.reporting_period || '' } ))
  }

  const submit = async (values: {
    mine_id: string
    mine_name: string
    reporting_period: string
    checklist_type: string
    extraction_engine: string
  }) => {
    if (!capabilities || !capabilities.extraction_engines.includes(values.extraction_engine)) { setError('尚未取得当前可用提取引擎，请先重新读取服务能力。'); return }
    if (!file) {
      setError('请先选择一份 PDF 文档。')
      return
    }
    setLoading(true)
    setError('')
    try {
      if (files.length === 1 && files[0].status !== 'submitted') {
        const response = await uploadAndParse({ ...values, extraction_engine: values.extraction_engine as UploadParams['extraction_engine'], file: files[0].file })
        updateFile(files[0].id, { status: 'submitted', run_id: response.parse_run_id, document_id: response.document_id, build_id: response.build_id, extraction_engine: response.extraction_engine || values.extraction_engine })
        handleSubmissionNavigation(response)
      } else {
        const pending = files.filter((item) => item.status !== 'submitted')
        if (pending.some((item) => !item.mine_id.trim() || !item.mine_name.trim() || !item.checklist_type || !/^\d{4}-(0[1-9]|1[0-2])$/.test(item.reporting_period))) {
          setError('请逐份填写煤矿编号、煤矿名称、清单类型和 YYYY-MM 周期。不同煤矿需分别确认。')
          return
        }
        for (const item of pending) {
          updateFile(item.id, { status: 'submitting', error: undefined })
          try {
            const params: UploadParams = { file: item.file, mine_id: item.mine_id.trim(), mine_name: item.mine_name.trim(),
              checklist_type: item.checklist_type, reporting_period: item.reporting_period, extraction_engine: values.extraction_engine as UploadParams['extraction_engine'] }
            const response = await uploadAndParse(params)
            updateFile(item.id, { status: 'submitted', run_id: response.parse_run_id, document_id: response.document_id, build_id: response.build_id, extraction_engine: response.extraction_engine || values.extraction_engine })
          } catch (failure) {
            updateFile(item.id, { status: 'failed', error: apiError(failure, '提交失败') })
          }
        }
      }
    } catch (failure) {
      setError(apiError(failure, '提交失败，请检查服务后重试。'))
    } finally {
      setLoading(false)
    }
  }

  const handleSubmissionNavigation = (response: UploadSubmissionResponse) => {
    if (response.parse_run_id) navigate(`/result/${response.parse_run_id}`)
    else if (response.build_id && response.document_id) navigate(`/documents/${response.document_id}/content-build/${response.build_id}`)
    else setError('上传已受理，但服务没有返回可追踪的运行或内容构建编号，请到解析队列核对。')
  }

  return (
    <div className="parser-workspace">
      <div className="page-title-row">
        <div>
          <h1>文档上传</h1>
          <p>按清单知识包提取并保留原文证据。可选择多份 PDF，逐份确认煤矿及清单归属。</p>
        </div>
        <div className="page-actions">
          <Button icon={<SlidersOutlined />} onClick={() => navigate('/rules')}>
            规则管理
          </Button>
          <Button icon={<UnorderedListOutlined />} onClick={() => navigate('/tasks')}>
            解析队列
          </Button>
        </div>
      </div>

      <section className="upload-surface">
        <Upload {...uploadProps}>
          <Button type="primary" icon={<CloudUploadOutlined />} disabled={loading}>
            选择 PDF（最多 20 份）
          </Button>
        </Upload>
          <Form
          form={form}
          layout="vertical"
          onFinish={submit}
          initialValues={{ reporting_period: dayjs().format('YYYY-MM'), checklist_type: types[0]?.checklist_type }}
          className="upload-form"
        >
          <Form.Item
            label="煤矿编号"
            name="mine_id"
            rules={[{ required: files.length <= 1, whitespace: true, message: '请输入煤矿编号' }]}
          >
            <Input placeholder="如 MINE-001" maxLength={100} disabled={loading} />
          </Form.Item>
          <Form.Item
            label="煤矿名称"
            name="mine_name"
            rules={[{ required: files.length <= 1, whitespace: true, message: '请输入煤矿名称' }]}
          >
            <Input placeholder="请输入所属煤矿" maxLength={200} disabled={loading} />
          </Form.Item>
          <Form.Item
            label="上报周期"
            name="reporting_period"
            rules={[
              { required: true, pattern: /^\d{4}-(0[1-9]|1[0-2])$/, message: '格式为 YYYY-MM' },
            ]}
          >
            <Input placeholder="YYYY-MM" disabled={loading} />
          </Form.Item>
          <Form.Item
            label="清单类型"
            name="checklist_type"
            rules={[{ required: true, message: '请选择清单类型' }]}
            extra={types.length ? undefined : '尚未配置清单类型，请先到规则管理写入示例规则'}
          >
            <Select
              disabled={loading || !types.length}
              options={types.map((item) => ({
                value: item.checklist_type,
                label: `${item.checklist_name}${item.rule_count ? `（${item.rule_count} 条规则）` : ''}`,
              }))}
              placeholder={types.length ? '选择清单类型' : '暂无可用清单'}
            />
          </Form.Item>
          <Form.Item label="提取引擎" name="extraction_engine" rules={[{ required: true }]} extra={capabilityError || (capabilities ? `默认：${capabilities.default_extraction_engine}` : '正在读取当前能力…')}>
            <Select disabled={loading || !capabilities} options={[
              ...(capabilities?.extraction_engines || []).map((engine) => ({ value: engine, label: engine === 'reliable' ? '可靠完整提取（v2）' : engine === 'checklist_agent' ? '清单知识包工具型智能体（旧路径）' : '旧规则引擎' })),
            ]} />
          </Form.Item>
          <Form.Item className="submit-item">
            <Button type="primary" htmlType="submit" loading={loading} disabled={!capabilities} icon={<SendOutlined />}>
              {files.length > 1 ? '提交未完成文件 / 重试失败文件' : '上传并解析'}
            </Button>
          </Form.Item>
        </Form>
        <span className="upload-help">每份 PDF 最大 100MB · 顺序提交 · 上传成功后查看解析队列</span>
      </section>
      {capabilityError && <Alert className="run-alert" type="error" showIcon message={capabilityError} action={<Button onClick={() => setCapabilityRefresh((value) => value + 1)}>重新读取能力</Button>} />}

      {files.length > 1 && <section className="panel" style={{ marginTop: 16, padding: 16 }}>
        <p>每份文档独立归属。下方煤矿信息默认留空；仅点击按钮后才将上方模板应用到待提交文件。</p>
        <Button disabled={loading} onClick={applyTemplate}>将模板应用到全部待提交文件</Button>
        <Table<BatchFile> rowKey="id" dataSource={files} pagination={false} scroll={{ x: 1200 }} size="small"
          columns={[
            { title: '文件 / 预览', key: 'file', width: 220, render: (_, item) => <Button type="link" onClick={() => setFile(item.file)}>{item.file.name}</Button> },
            ...(['mine_id', 'mine_name', 'reporting_period'] as const).map((key) => ({ title: { mine_id: '煤矿编号', mine_name: '煤矿名称', reporting_period: '周期' }[key], key,
              render: (_: unknown, item: BatchFile) => <Input value={item[key]} disabled={loading || item.status === 'submitted'} onChange={(event) => updateFile(item.id, { [key]: event.target.value })} /> })),
            { title: '清单类型', key: 'checklist', width: 210, render: (_, item) => <Select style={{ width: '100%' }} value={item.checklist_type || undefined} disabled={loading || item.status === 'submitted'}
              options={types.map((type) => ({ value: type.checklist_type, label: type.checklist_name }))} onChange={(value) => updateFile(item.id, { checklist_type: value })} /> },
            { title: '提交状态', key: 'status', render: (_, item) => <><Tag color={item.status === 'failed' ? 'red' : item.status === 'submitted' ? 'blue' : 'default'}>
              {{ pending: '待提交', submitting: '提交中', submitted: item.build_id ? '内容准备中' : '已入队', failed: '提交失败' }[item.status]}</Tag>
              {item.run_id && <Button type="link" onClick={() => navigate(`/result/${item.run_id}`)}>查看任务结果</Button>}{item.build_id && item.document_id && <Button type="link" onClick={() => navigate(`/documents/${item.document_id}/content-build/${item.build_id}`)}>进入内容准备</Button>}{item.error && <p>{item.error}</p>}</> },
          ]} />
      </section>}

      {error && <Alert className="run-alert" type="error" showIcon message={error} />}

      <ParseWorkspace fileName={file?.name} fileUrl={url} />
    </div>
  )
}
