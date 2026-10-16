import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Alert, Button } from 'antd'

/** 助手渲染失败时保留结果页和返回入口。 */
export default class AssistantErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('文档 AI 助手渲染失败', error, info.componentStack)
  }

  render() {
    if (this.state.failed) return <Alert type="error" showIcon message="文档 AI 助手加载失败"
      description="请重试打开助手，或点击上方“返回提取核对”继续查看解析结果。"
      action={<Button onClick={() => this.setState({ failed: false })}>重试打开助手</Button>} />
    return this.props.children
  }
}
