import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Empty, Progress, Spin, Table } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import type { ColumnsType } from 'antd/es/table'
import Chart, { baseTooltip, baseValueAxis, chartTheme } from '@/components/Chart'
import {
  getChecklistDistribution,
  getConfidenceDistribution,
  getFieldQuality,
  getOverview,
  getStageDuration,
  getTrend,
} from '@/services/stats'
import { apiError } from '@/services/api'
import type {
  ChecklistDistributionItem,
  ConfidenceBucket,
  FieldQualityItem,
  StageDurationItem,
  StatsOverview,
  TrendPoint,
} from '@/types/api'

const stageLabels: Record<string, string> = {
  pdf_parse: 'PDF 解析',
  ocr: 'OCR 识别',
  quality: '质量评估',
  extraction: '字段提取',
  pipeline: '整体链路',
}

export default function Analytics() {
  const [overview, setOverview] = useState<StatsOverview>()
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [distribution, setDistribution] = useState<ChecklistDistributionItem[]>([])
  const [confidence, setConfidence] = useState<ConfidenceBucket[]>([])
  const [fields, setFields] = useState<FieldQualityItem[]>([])
  const [stages, setStages] = useState<StageDurationItem[]>([])
  const [avgTotal, setAvgTotal] = useState<number | null>(null)
  const [days, setDays] = useState(30)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [panelErrors, setPanelErrors] = useState<Record<string, string>>({})
  const requestSequence = useRef(0)

  const load = async () => {
    const request = ++requestSequence.current
    setLoading(true)
    setError('')
    const failures: Record<string, string> = {}
    const capture = <T,>(label: string, promise: Promise<T>) => promise.catch((failure) => {
      failures[label] = apiError(failure, '接口不可用')
      return undefined
    })
    const [summary, trendData, dist, conf, fieldData, stageData] = await Promise.all([
      capture('概览', getOverview()),
      capture('趋势', getTrend(days)),
      capture('清单分布', getChecklistDistribution()),
      capture('方法匹配分分布', getConfidenceDistribution()),
      capture('字段质量', getFieldQuality()),
      capture('链路耗时', getStageDuration()),
    ])
    if (request !== requestSequence.current) return
    setPanelErrors(failures)
    if (summary) setOverview(summary)
    if (trendData) setTrend(trendData.series)
    if (dist) setDistribution(dist.items)
    if (conf) setConfidence(conf.items)
    if (fieldData) setFields(fieldData.items)
    if (stageData) {
      setStages(stageData.stages)
      setAvgTotal(stageData.avg_total_seconds)
    }
    if (Object.keys(failures).length) setError(`部分分析数据加载失败：${Object.keys(failures).join('、')}。其他面板仍可查看，失败面板若有历史数据则保留展示。`)
    setLoading(false)
  }

  useEffect(() => {
    void load()
    return () => { requestSequence.current += 1 }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days])

  const trendOption = useMemo(
    () => ({
      tooltip: baseTooltip,
      legend: {
        data: ['解析总量', '完成', '失败'],
        right: 0,
        top: 0,
        textStyle: { color: chartTheme.text, fontSize: 11 },
      },
      grid: { left: 8, right: 8, top: 36, bottom: 4, containLabel: true },
      xAxis: {
        type: 'category' as const,
        data: trend.map((item) => dayjs(item.date).format('MM-DD')),
        axisLine: { lineStyle: { color: chartTheme.axisLine } },
        axisLabel: { color: chartTheme.text, fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: baseValueAxis,
      series: [
        {
          name: '解析总量',
          type: 'bar' as const,
          barMaxWidth: 22,
          data: trend.map((item) => item.total),
          itemStyle: { color: '#cfe2fb', borderRadius: [3, 3, 0, 0] },
        },
        {
          name: '完成',
          type: 'line' as const,
          smooth: true,
          data: trend.map((item) => item.completed),
          itemStyle: { color: chartTheme.primary },
        },
        {
          name: '失败',
          type: 'line' as const,
          smooth: true,
          data: trend.map((item) => item.failed),
          itemStyle: { color: chartTheme.danger },
        },
      ],
    }),
    [trend],
  )

  const confidenceOption = useMemo(
    () => ({
      tooltip: { ...baseTooltip, trigger: 'item' as const },
      series: [
        {
          type: 'pie' as const,
          radius: ['46%', '72%'],
          data: confidence.map((item, index) => ({
            name: item.label,
            value: item.count,
            itemStyle: {
              color: [chartTheme.success, chartTheme.primary, chartTheme.warning, chartTheme.danger][
                index
              ],
            },
          })),
          label: {
            formatter: '{b}\n{c} 次',
            color: chartTheme.text,
            fontSize: 11,
          },
        },
      ],
    }),
    [confidence],
  )

  const fieldColumns: ColumnsType<FieldQualityItem> = [
    {
      title: '字段',
      dataIndex: 'field_name',
      render: (value: string, row) => (
        <div>
          <strong>{value}</strong>
          <small className="table-subtitle">{row.checklist_type || '未知清单'} · {row.field_key}</small>
        </div>
      ),
    },
    { title: '已发现', dataIndex: 'candidate_found', width: 90 },
    {
      title: '可供下游使用', dataIndex: 'available_for_downstream', width: 125,
      render: (value: number | null, row) => <span title="有结构化事实及来源可供下游判断，不代表业务已采纳或保证正确">
        {value == null ? '未评估' : value}
        {row.availability_unknown > 0 && <small className="table-subtitle">另有 {row.availability_unknown} 次未评估</small>}
      </span>,
    },
    { title: '候选事实数', dataIndex: 'fact_count', width: 100 },
    { title: '值组数', dataIndex: 'value_group_count', width: 85 },
    { title: '风险提示', dataIndex: 'risk_count', width: 90 },
    {
      title: '发现覆盖率',
      dataIndex: 'coverage',
      width: 160,
      render: (value: number | null) =>
        value == null ? '—' : <Progress percent={value} size="small" strokeColor={chartTheme.primary} />,
    },
    {
      title: '平均方法匹配分',
      dataIndex: 'avg_confidence',
      width: 120,
      render: (value: number | null) => (value == null ? '—' : value.toFixed(3)),
    },
    {
      title: '最低方法匹配分',
      dataIndex: 'min_confidence',
      width: 120,
      render: (value: number | null) =>
        value == null ? (
          '—'
        ) : (
          <span>{value.toFixed(3)}</span>
        ),
    },
  ]

  const totalDocuments = distribution.reduce((sum, item) => sum + item.documents, 0)
  const hasAnyData = (overview?.total_runs ?? 0) > 0 || trend.some((item) => item.total > 0) || distribution.length > 0 || confidence.some((item) => item.count > 0) || fields.length > 0 || stages.length > 0
  const maxStageDuration = Math.max(1, ...stages.map((s) => s.avg_duration_seconds ?? 0))

  return (
    <div className="list-page">
      <div className="page-title-row">
        <div>
          <h1>数据分析</h1>
          <p>解析效率、字段质量与链路耗时，全部基于真实解析记录统计。</p>
        </div>
        <div className="page-actions">
          <div className="range-switch">
            {[7, 30, 90].map((value) => (
              <button key={value} className={days === value ? 'active' : ''} onClick={() => setDays(value)}>
                近 {value} 天
              </button>
            ))}
          </div>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        </div>
      </div>

      {error && <Alert className="run-alert" type="error" showIcon message={error} />}

      {loading && !overview ? (
        <div className="loading-state">
          <Spin size="large" />
        </div>
      ) : !hasAnyData ? (
        <div className="panel empty-panel">
          <Empty description={error ? '部分统计接口不可用，请刷新重试' : '还没有解析记录，完成几次解析后这里会出现真实统计'} />
        </div>
      ) : (
        <>
          {overview && <div className="stat-grid">
            <article className="stat-card">
              <span className="stat-label">累计解析</span>
              <strong className="stat-value">
                {overview?.total_runs ?? 0}
                <em>次</em>
              </strong>
              <span className="stat-meta">覆盖 {overview?.document_count ?? 0} 份文档</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">解析成功率</span>
              <strong className="stat-value">
                {overview?.success_rate ?? '—'}
                {overview?.success_rate != null && <em>%</em>}
              </strong>
              <span className="stat-meta">
                完成 {overview?.completed_count ?? 0} · 失败 {overview?.failed_count ?? 0}
              </span>
            </article>
            <article className="stat-card">
              <span className="stat-label">平均方法匹配分</span>
              <strong className="stat-value">
                {overview?.avg_confidence == null ? '—' : overview.avg_confidence.toFixed(3)}
              </strong>
              <span className="stat-meta">待复核任务 {overview?.review_count ?? 0}</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">平均耗时</span>
              <strong className="stat-value">
                {overview?.avg_duration_seconds ?? '—'}
                {overview?.avg_duration_seconds != null && <em>秒</em>}
              </strong>
              <span className="stat-meta">单份文档端到端</span>
            </article>
          </div>}

          <section className="panel">
            <div className="panel-heading">
              <h2 className="panel-title">解析量趋势</h2>
              <span className="muted">近 {days} 天</span>
            </div>
            <Chart option={trendOption} height={280} minWidth={560} />
            {panelErrors['趋势'] && <Alert type="warning" showIcon message={panelErrors['趋势']} />}
          </section>

          <div className="analytics-grid section-gap">
            <section className="panel">
              <div className="panel-heading">
                <h2 className="panel-title">清单类型分布</h2>
                <span className="muted">共 {totalDocuments} 份文档</span>
              </div>
              <div className="panel-body">
                {panelErrors['清单分布'] && <Alert type="warning" showIcon message={panelErrors['清单分布']} />}
                {distribution.length ? (
                  distribution.map((item) => {
                    const ratio = totalDocuments ? (item.documents / totalDocuments) * 100 : 0
                    return (
                      <div className="distribution-row" key={item.checklist_type}>
                        <span className="distribution-label" title={item.checklist_type}>
                          {item.checklist_type}
                        </span>
                        <div className="progress-track">
                          <div className="progress-fill" style={{ width: `${ratio}%` }} />
                        </div>
                        <span className="distribution-value">
                          {item.documents}
                          {item.avg_confidence != null && (
                            <small>匹配分 {item.avg_confidence.toFixed(3)}</small>
                          )}
                        </span>
                      </div>
                    )
                  })
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />
                )}
              </div>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <h2 className="panel-title">方法匹配分分布（未经正确率校准）</h2>
                <span className="muted">{confidence.reduce((sum, i) => sum + i.count, 0)} 次解析</span>
              </div>
              {confidence.some((item) => item.count > 0) ? (
                <Chart option={confidenceOption} height={240} minWidth={260} />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无方法匹配分数据" />
              )}
              {panelErrors['方法匹配分分布'] && <Alert type="warning" showIcon message={panelErrors['方法匹配分分布']} />}
            </section>
          </div>

          <div className="analytics-grid section-gap">
            <section className="panel">
              <div className="panel-heading">
                <h2 className="panel-title">字段发现与下游使用</h2>
                <span className="muted">按已完成运行统计；可供使用表示支持下游判断</span>
              </div>
              <Table
                rowKey={(row) => `${row.checklist_type || ''}:${row.field_key}`}
                size="small"
                columns={fieldColumns}
                dataSource={fields}
                pagination={false}
                scroll={{ x: 1150, y: 300 }}
                locale={{ emptyText: '暂无字段数据' }}
              />
              {panelErrors['字段质量'] && <Alert type="warning" showIcon message={panelErrors['字段质量']} />}
            </section>

            <section className="panel">
              <div className="panel-heading">
                <h2 className="panel-title">链路耗时</h2>
                {avgTotal != null && <span className="muted">平均 {avgTotal} 秒</span>}
              </div>
              <div className="panel-body">
                {panelErrors['链路耗时'] && <Alert type="warning" showIcon message={panelErrors['链路耗时']} />}
                {stages.length ? (
                  stages.map((stage) => (
                    <div className="distribution-row" key={stage.stage}>
                      <span className="distribution-label">
                        {stageLabels[stage.stage] || stage.stage}
                      </span>
                      <div className="progress-track">
                        <div
                          className="progress-fill secondary"
                          style={{ width: stage.avg_duration_seconds == null ? undefined : `${(stage.avg_duration_seconds / maxStageDuration) * 100}%`, visibility: stage.avg_duration_seconds == null ? 'hidden' : 'visible' }}
                        />
                      </div>
                      <span className="distribution-value">
                        {stage.avg_duration_seconds == null ? `${stage.entries} 条日志` : `${stage.avg_duration_seconds} 秒`}
                      </span>
                    </div>
                  ))
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无链路日志" />
                )}
                <p className="panel-note">
                  {stages.some((stage) => stage.avg_duration_seconds != null)
                    ? '按解析阶段展示平均耗时。'
                    : '当前仅有阶段日志条数，写入阶段计时后将显示平均耗时。'}
                </p>
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  )
}
