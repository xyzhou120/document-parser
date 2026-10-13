import { useEffect, useState } from 'react'
import { Alert, Badge, Button, Descriptions, Spin } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { getServices } from '@/services/stats'
import { apiError } from '@/services/api'
import type { ServiceOverview, ServiceStatus } from '@/types/api'

const statusMeta: Record<string, { text: string; status: 'success' | 'warning' | 'error' | 'default' }> = {
  online: { text: '运行正常', status: 'success' },
  degraded: { text: '降级', status: 'warning' },
  offline: { text: '不可达', status: 'error' },
  unconfigured: { text: '未配置', status: 'default' },
  unknown: { text: '未知', status: 'default' },
}

const icons: Record<string, string> = {
  database: 'PG',
  redis: 'RD',
  minio: 'MI',
  ocr: 'OCR',
  text_llm: 'LLM',
  worker: 'WK',
}

export default function ServiceManagement() {
  const [data, setData] = useState<ServiceOverview>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setData(await getServices())
    } catch (failure) {
      setError(apiError(failure, '读取服务状态失败。'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  return (
    <div className="list-page">
      <div className="page-title-row">
        <div>
          <h1>服务管理</h1>
          <p>实时探测各依赖服务的连通性，不展示写死的示例状态。</p>
        </div>
        <div className="page-actions">
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            重新探测
          </Button>
        </div>
      </div>

      {error && <Alert className="run-alert" type="error" showIcon message={error} />}

      {loading && !data ? (
        <div className="loading-state">
          <Spin size="large" />
        </div>
      ) : (
        <>
          <Alert
            className="run-alert"
            type={data?.healthy ? 'success' : 'warning'}
            showIcon
            message={
              data?.healthy
                ? `全部 ${data.total_count} 个服务运行正常`
                : `${data?.online_count ?? 0}/${data?.total_count ?? 0} 个服务在线，请检查下方状态`
            }
          />

          <div className="service-grid">
            {(data?.services || []).map((service: ServiceStatus) => {
              const meta = statusMeta[service.status] || statusMeta.unknown
              return (
                <article className="service-card" key={service.key}>
                  <div className="service-card-head">
                    <span className="service-icon">{icons[service.key] || service.key.slice(0, 3).toUpperCase()}</span>
                    <div>
                      <h3>{service.name}</h3>
                      <Badge status={meta.status} text={meta.text} />
                    </div>
                  </div>
                  <Descriptions column={1} size="small" className="service-detail">
                    <Descriptions.Item label="响应时间">
                      {service.latency_ms == null ? '—' : `${service.latency_ms} ms`}
                    </Descriptions.Item>
                    <Descriptions.Item label="连接信息">
                      <span className="service-endpoint">{service.detail}</span>
                    </Descriptions.Item>
                  </Descriptions>
                </article>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
