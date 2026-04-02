import { createRouter, createWebHistory } from 'vue-router'
import BasicLayout from '../components/Layout/BasicLayout.vue'
import ExportRecords from '../views/ExportRecords.vue'
import Login from '../views/Login.vue'
import ArticleList from '../views/ArticleList.vue'
import ChangePassword from '../views/ChangePassword.vue'
import EditUser from '../views/EditUser.vue'
import AddSubscription from '../views/AddSubscription.vue'
import BatchSubscribe from '../views/BatchSubscribe.vue'
import WeChatMpManagement from '../views/WeChatMpManagement.vue'
import ConfigList from '../views/ConfigList.vue'
import ConfigDetail from '../views/ConfigDetail.vue'
import MessageTaskList from '../views/MessageTaskList.vue'
import MessageTaskForm from '../views/MessageTaskForm.vue'
import NovelReader from '../views/NovelReader.vue'

const routes = [
  {
    path: '/',
    component: BasicLayout,
    children: [
      {
        path: '',
        name: 'Home',
        component: ArticleList,
        meta: { requiresAuth: true }
      },
      {
        path: 'change-password',
        name: 'ChangePassword',
        component: ChangePassword,
        meta: { requiresAuth: true }
      },
      {
        path: 'edit-user',
        name: 'EditUser',
        component: EditUser,
        meta: { requiresAuth: true }
      },
      {
        path: 'add-subscription',
        name: 'AddSubscription',
        component: AddSubscription,
        meta: { requiresAuth: true }
      },
      {
        path: 'batch-subscribe',
        name: 'BatchSubscribe',
        component: BatchSubscribe,
        meta: { requiresAuth: true }
      },
      {
        path: 'wechat/mp',
        name: 'WeChatMpManagement',
        component: WeChatMpManagement,
        meta: { 
          requiresAuth: true,
          permissions: ['wechat:manage'] 
        }
      },
      {
        path: 'configs',
        name: 'ConfigList',
        component: ConfigList,
        meta: { 
          requiresAuth: true,
          permissions: ['config:view'] 
        }
      },
      {
        path: 'export/records',
        name: 'ExportList',
        component: ExportRecords,
        meta: { 
          requiresAuth: true,
          permissions: ['config:view'] 
        }
      },
      {
        path: 'configs/:key',
        name: 'ConfigDetail',
        component: ConfigDetail,
        props: true,
        meta: { 
          requiresAuth: true,
          permissions: ['config:view'] 
        }
      },
      {
        path: 'message-tasks',
        name: 'MessageTaskList',
        component: MessageTaskList,
        meta: { 
          requiresAuth: true,
          permissions: ['message_task:view'] 
        }
      },
      {
        path: 'message-tasks/add',
        name: 'MessageTaskAdd',
        component: MessageTaskForm,
        meta: { 
          requiresAuth: true,
          permissions: ['message_task:edit'] 
        }
      },
      {
        path: 'message-tasks/edit/:id',
        name: 'MessageTaskEdit',
        component: MessageTaskForm,
        props: true,
        meta: { 
          requiresAuth: true,
          permissions: ['message_task:edit'] 
        }
      },
      {
        path: 'sys-info',
        name: 'SysInfo',
        component: () => import('@/views/SysInfo.vue'),
        meta: { 
          requiresAuth: true,
          permissions: ['admin'] 
        }
      },
      {
        path: 'tags',
        name: 'TagList',
        component: () => import('@/views/TagList.vue'),
        meta: { 
          requiresAuth: true,
          permissions: ['tag:view'] 
        }
      },
      {
        path: 'tags/add',
        name: 'TagAdd',
        component: () => import('@/views/TagForm.vue'),
        meta: { 
          requiresAuth: true,
          permissions: ['tag:edit'] 
        }
      },
      {
        path: 'tags/edit/:id',
        name: 'TagEdit',
        component: () => import('@/views/TagForm.vue'),
        props: true,
        meta: { 
          requiresAuth: true,
          permissions: ['tag:edit'] 
        }
      },
      {
        path: 'access-keys',
        name: 'AccessKeyManagement',
        component: () => import('@/views/AccessKeyManagement.vue'),
        meta: { 
          requiresAuth: true,
          permissions: ['admin'] 
        }
      },
      {
        path: 'cascade',
        name: 'CascadeManagement',
        component: () => import('@/views/CascadeManagement.vue'),
        meta: { 
          requiresAuth: true,
          permissions: ['admin'] 
        }
      },
      {
        path: 'env-exception',
        name: 'EnvExceptionStats',
        component: () => import('@/views/EnvExceptionStats.vue'),
        meta: { 
          requiresAuth: true,
          permissions: ['admin'] 
        }
      },
    ]
  },
  {
    path: '/login',
    name: 'Login',
    component: Login
  },
  {
    path: '/reader',
    name: 'NovelReader',
    component: NovelReader,
    meta: { requiresAuth: true }
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

const DEFAULT_USERNAME = 'admin'
const DEFAULT_PASSWORD = 'admin@123'

async function autoLogin(): Promise<boolean> {
  try {
    const formData = new URLSearchParams()
    formData.append('username', DEFAULT_USERNAME)
    formData.append('password', DEFAULT_PASSWORD)
    const { default: axios } = await import('axios')
    const response = await axios.post(
      (import.meta.env.VITE_API_BASE_URL || '') + 'api/v1/wx/auth/login',
      formData,
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
    )
    const token = response.data?.data?.access_token || response.data?.access_token
    if (token) {
      localStorage.setItem('token', token)
      const expiresIn = response.data?.data?.expires_in
      if (expiresIn) {
        localStorage.setItem('token_expire', String(Date.now() + expiresIn * 1000))
      }
      return true
    }
    return false
  } catch {
    return false
  }
}

router.beforeEach(async (to, from, next) => {
  if (!to.meta.requiresAuth) {
    return next()
  }

  const token = localStorage.getItem('token')

  if (!token) {
    const ok = await autoLogin()
    if (ok) {
      return next()
    }
    return next({ path: '/login', query: { redirect: to.fullPath } })
  }

  return next()
})

export default router
