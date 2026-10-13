import axios from 'axios'
const api = axios.create({ baseURL: import.meta.env.VITE_API_BASE_URL || '/api', timeout: 60000 })
api.interceptors.response.use((response) => response.data)
export function apiError(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return '请检查表单必填项与格式。'
    if (!error.response) return '无法连接解析 API，请检查本地服务是否启动。'
  }
  return fallback
}
export default api
