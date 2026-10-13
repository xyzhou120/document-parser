import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

interface Props {
  option: echarts.EChartsOption
  height?: number
  /** 图表容器的最小宽度，窄屏时横向滚动 */
  minWidth?: number
}

/**
 * ECharts 轻量封装。
 * 只负责挂载、更新与自适应，不注入任何示例数据——数据一律由调用方传入。
 */
export default function Chart({ option, height = 260, minWidth = 320 }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const instance = useRef<echarts.ECharts>()

  useEffect(() => {
    if (!container.current) return
    instance.current = echarts.init(container.current, undefined, { renderer: 'canvas' })
    const resize = () => instance.current?.resize()
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      instance.current?.dispose()
      instance.current = undefined
    }
  }, [])

  useEffect(() => {
    if (!instance.current) return
    // notMerge 为 true，避免切换数据源时残留上一次的系列
    instance.current.setOption(option, true)
  }, [option])

  return (
    <div style={{ width: '100%', overflowX: 'auto' }}>
      <div ref={container} style={{ width: '100%', minWidth, height }} />
    </div>
  )
}

/** 统一的图表配色与基础样式 */
export const chartTheme = {
  primary: '#087bff',
  success: '#1f9d55',
  warning: '#e9b34b',
  danger: '#e05252',
  muted: '#8fa3bd',
  axisLine: '#dce5ef',
  text: '#5b6f8c',
}

export const baseAxisStyle: echarts.EChartsOption['xAxis'] = {
  type: 'category',
  axisLine: { lineStyle: { color: chartTheme.axisLine } },
  axisLabel: { color: chartTheme.text, fontSize: 11 },
  axisTick: { show: false },
}

export const baseValueAxis: echarts.EChartsOption['yAxis'] = {
  type: 'value',
  splitLine: { lineStyle: { color: '#eef3f9' } },
  axisLabel: { color: chartTheme.text, fontSize: 11 },
}

export const baseTooltip: echarts.EChartsOption['tooltip'] = {
  trigger: 'axis',
  backgroundColor: 'rgba(255,255,255,.96)',
  borderColor: '#dce5ef',
  textStyle: { color: '#20344f', fontSize: 12 },
}
