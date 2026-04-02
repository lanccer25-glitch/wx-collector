import axios from 'axios'
import { getToken } from '@/utils/auth'
import { Message } from '@arco-design/web-vue'

const http = axios.create({
  baseURL: (import.meta.env.VITE_API_BASE_URL || '') + 'api/v1/',
  timeout: 100000,
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json'
  }
})

const DEFAULT_USERNAME = 'admin'
const DEFAULT_PASSWORD = 'admin@123'

let isRefreshing = false
let failedQueue: Array<{ resolve: (value: any) => void; reject: (reason: any) => void }> = []

function processQueue(error: any, token: string | null = null) {
  failedQueue.forEach(prom => {
    if (error) {
      prom.reject(error)
    } else {
      prom.resolve(token)
    }
  })
  failedQueue = []
}

async function doAutoLogin(): Promise<string | null> {
  try {
    const formData = new URLSearchParams()
    formData.append('username', DEFAULT_USERNAME)
    formData.append('password', DEFAULT_PASSWORD)
    const response = await axios.post(
      (import.meta.env.VITE_API_BASE_URL || '') + 'api/v1/wx/auth/login',
      formData,
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
    )
    const token = response.data?.data?.access_token || response.data?.access_token
    if (token) {
      localStorage.setItem('token', token)
      if (response.data?.data?.expires_in) {
        localStorage.setItem('token_expire', String(Date.now() + response.data.data.expires_in * 1000))
      }
      return token
    }
    return null
  } catch {
    return null
  }
}

http.interceptors.request.use(
  config => {
    const token = getToken()
    if (token) {
      config.headers['Authorization'] = `Bearer ${token}`
    }
    return config
  },
  error => {
    return Promise.reject(error)
  }
)

http.interceptors.response.use(
  response => {
    if (response.data?.code === 0) {
      return response.data?.data || response.data?.detail || response.data || response
    }
    if (response.data?.code == 401) {
      return Promise.reject('未登录或登录已过期，请重新登录。')
    }
    const data = response.data?.detail || response.data
    const errorMsg = data?.message || '请求失败'
    Message.error(errorMsg)
    return Promise.reject(response.data)
  },
  async error => {
    const originalRequest = error.config

    if (error.response?.status === 401 && !originalRequest._retry) {
      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject })
        }).then(token => {
          originalRequest.headers['Authorization'] = `Bearer ${token}`
          return http(originalRequest)
        }).catch(err => Promise.reject(err))
      }

      originalRequest._retry = true
      isRefreshing = true
      localStorage.removeItem('token')

      try {
        const newToken = await doAutoLogin()
        if (newToken) {
          processQueue(null, newToken)
          originalRequest.headers['Authorization'] = `Bearer ${newToken}`
          return http(originalRequest)
        } else {
          processQueue(new Error('自动登录失败'))
          return Promise.reject(error)
        }
      } catch (refreshError) {
        processQueue(refreshError)
        return Promise.reject(refreshError)
      } finally {
        isRefreshing = false
      }
    }

    const errorMsg =
      error?.response?.data?.message ||
      error?.response?.data?.detail?.message ||
      error?.response?.data?.detail ||
      error?.message ||
      '请求错误'
    return Promise.reject(errorMsg)
  }
)

export default http
