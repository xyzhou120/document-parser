import DocumentList from './DocumentList'

/**
 * 批量查询：与文档管理共用同一套真实数据源，
 * 侧重多条件组合筛选与结果导出。
 */
export default function BatchQuery() {
  return (
    <DocumentList
      title="批量查询"
      subtitle="按煤矿、清单类型、上报周期与状态组合筛选解析记录，并导出为 CSV 用于离线核对。"
    />
  )
}
