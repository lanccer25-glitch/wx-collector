<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { Message } from '@arco-design/web-vue'
import { batchSubscribe, addSubscription } from '@/api/subscription'

const CHUNK_SIZE = 20
const STORAGE_KEY = 'batchSubscribe_processed_v1'

const loadProcessedSet = (): Set<string> => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return new Set()
    return new Set(JSON.parse(raw) as string[])
  } catch { return new Set() }
}

const saveProcessedSet = (set: Set<string>) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...set]))
  } catch {}
}

const router = useRouter()
const companyText = ref('')
const allChunks = ref<string[][]>([])
const currentChunkIdx = ref(0)
const currentResults = ref<any[]>([])
const searching = ref(false)
const subscribing = ref(false)
const retrying = ref(false)
const allDone = ref(false)
const totalStats = ref({ success: 0, skip: 0, batches: 0 })
const filteredCount = ref(0)
const processedArchiveCount = ref(loadProcessedSet().size)

const isStarted = computed(() => allChunks.value.length > 0)
const batchTotal = computed(() => allChunks.value.length)
const batchCurrent = computed(() => currentChunkIdx.value + 1)
const isLastChunk = computed(() => currentChunkIdx.value >= allChunks.value.length - 1)

const selectedCount = computed(() =>
  currentResults.value.reduce((acc, r) => acc + (r.selected_indices?.length || 0), 0)
)
const rateLimitedCount = computed(() =>
  currentResults.value.filter(r => r.status === 'rate_limited').length
)

const getCompanies = () =>
  companyText.value.split('\n').map((s: string) => s.trim()).filter(Boolean)

const toggleCandidate = (rowIdx: number, ci: number) => {
  const row = currentResults.value[rowIdx]
  if (!row.selected_indices) row.selected_indices = []
  const idx = row.selected_indices.indexOf(ci)
  if (idx >= 0) row.selected_indices.splice(idx, 1)
  else row.selected_indices.push(ci)
}

const searchChunk = async (chunkIdx: number) => {
  searching.value = true
  currentResults.value = []
  try {
    const res = await batchSubscribe(allChunks.value[chunkIdx], false)
    currentResults.value = (res?.results || []).map((r: any) => ({
      ...r,
      subscribed: false,
      message: r.message || '',
      selected_indices: r.selected_index !== null && r.selected_index !== undefined ? [r.selected_index] : [],
    }))
  } catch (e: any) {
    Message.error(e?.message || '搜索失败，请检查微信登录状态')
  } finally {
    searching.value = false
  }
}

const handleStart = async () => {
  const allInput = getCompanies()
  if (!allInput.length) {
    Message.warning('请先输入公司名称')
    return
  }
  const processed = loadProcessedSet()
  const companies = allInput.filter(c => !processed.has(c))
  filteredCount.value = allInput.length - companies.length
  if (!companies.length) {
    Message.warning(`输入的 ${allInput.length} 家公司均已在历史存档中，无需重复搜索`)
    return
  }
  if (filteredCount.value > 0) {
    Message.info(`已自动过滤 ${filteredCount.value} 家历史已处理公司，剩余 ${companies.length} 家待搜索`)
  }
  const chunks: string[][] = []
  for (let i = 0; i < companies.length; i += CHUNK_SIZE) {
    chunks.push(companies.slice(i, i + CHUNK_SIZE))
  }
  allChunks.value = chunks
  currentChunkIdx.value = 0
  allDone.value = false
  totalStats.value = { success: 0, skip: 0, batches: 0 }
  await searchChunk(0)
}

const subscribeCurrentSelected = async () => {
  const toSubscribe: Array<{row: any, candidate: any}> = []
  for (const row of currentResults.value) {
    for (const ci of (row.selected_indices || [])) {
      if (row.candidates?.[ci]) toSubscribe.push({ row, candidate: row.candidates[ci] })
    }
  }
  if (!toSubscribe.length) return { success: 0, skip: 0 }
  subscribing.value = true
  let successCount = 0
  let skipCount = 0
  for (const { row, candidate: c } of toSubscribe) {
    try {
      await addSubscription({
        mp_name: c.nickname,
        mp_id: c.fakeid,
        avatar: c.avatar,
        mp_intro: c.intro || '',
      })
      row.subscribed = true
      row.message = '订阅成功'
      successCount++
    } catch (e: any) {
      const msg = (e as any)?.response?.data?.detail?.message || (e as any)?.message || ''
      if (msg.includes('已存在') || msg.includes('已订阅') || (e as any)?.response?.status === 409) {
        row.message = '已订阅（跳过）'
        skipCount++
      } else {
        row.message = msg || '订阅失败'
      }
    }
  }
  subscribing.value = false
  const processed = loadProcessedSet()
  for (const { row } of toSubscribe) {
    if (row.subscribed || row.message === '已订阅（跳过）') {
      processed.add(row.company)
    }
  }
  saveProcessedSet(processed)
  processedArchiveCount.value = processed.size
  return { success: successCount, skip: skipCount }
}

const handleSubscribeAndNext = async () => {
  const { success, skip } = await subscribeCurrentSelected()
  totalStats.value.success += success
  totalStats.value.skip += skip
  totalStats.value.batches++
  if (success + skip > 0) {
    Message.success(`第 ${batchCurrent.value} 批：新增 ${success} 个，已存在跳过 ${skip} 个`)
  }
  await goNext()
}

const handleSkipAndNext = async () => {
  totalStats.value.batches++
  Message.info(`第 ${batchCurrent.value} 批已跳过`)
  await goNext()
}

const goNext = async () => {
  if (isLastChunk.value) {
    allDone.value = true
    currentResults.value = []
    Message.success(`全部 ${batchTotal.value} 批处理完毕！共订阅 ${totalStats.value.success} 个，跳过 ${totalStats.value.skip} 个`)
  } else {
    currentChunkIdx.value++
    await searchChunk(currentChunkIdx.value)
  }
}

const handleRetryRateLimited = async () => {
  const rateLimitedCompanies = currentResults.value
    .filter(r => r.status === 'rate_limited')
    .map(r => r.company)
  if (!rateLimitedCompanies.length) return
  retrying.value = true
  try {
    const res = await batchSubscribe(rateLimitedCompanies, false)
    const newRows = (res?.results || []).map((r: any) => ({
      ...r,
      subscribed: false,
      message: r.message || '',
      selected_indices: r.selected_index !== null && r.selected_index !== undefined ? [r.selected_index] : [],
    }))
    for (const newRow of newRows) {
      const idx = currentResults.value.findIndex(r => r.company === newRow.company)
      if (idx >= 0) currentResults.value[idx] = newRow
    }
    const stillLimited = currentResults.value.filter(r => r.status === 'rate_limited').length
    const found = newRows.filter((r: any) => r.status !== 'not_found' && r.status !== 'error' && r.status !== 'rate_limited').length
    if (stillLimited > 0) {
      Message.warning(`仍有 ${stillLimited} 家受频率限制，可再次重试`)
    } else {
      Message.success(`重试完成，找到 ${found} 家候选账号`)
    }
  } catch (e: any) {
    Message.error(e?.message || '重试失败')
  } finally {
    retrying.value = false
  }
}

const clearArchive = () => {
  localStorage.removeItem(STORAGE_KEY)
  processedArchiveCount.value = 0
  Message.success('历史存档已清除')
}

const reset = () => {
  companyText.value = ''
  allChunks.value = []
  currentChunkIdx.value = 0
  currentResults.value = []
  allDone.value = false
  filteredCount.value = 0
  totalStats.value = { success: 0, skip: 0, batches: 0 }
}

const goBack = () => router.go(-1)
</script>

<template>
  <div class="batch-subscribe">
    <a-page-header
      title="批量订阅"
      subtitle="输入公司名称，系统按每批20家逐批搜索，您逐批确认订阅"
      :show-back="true"
      @back="goBack"
    />

    <!-- 输入阶段 -->
    <a-card v-if="!isStarted">
      <a-space direction="vertical" size="large" fill>
        <!-- 历史存档提示 -->
        <a-alert v-if="processedArchiveCount > 0" type="info" show-icon>
          本机浏览器已存档 <strong>{{ processedArchiveCount }}</strong> 家已处理公司，开始搜索时将自动过滤，只处理未曾成功过的公司。
          <a-link @click.stop="clearArchive" style="margin-left:8px;">清除存档</a-link>
        </a-alert>
        <div>
          <div style="margin-bottom: 8px; font-weight: 500;">公司名称列表（每行一个）</div>
          <a-textarea
            v-model="companyText"
            placeholder="每行输入一个公司名称，例如：
北京赛德阳光医院管理集团
上海不齐而遇医疗科技
先临三维科技股份有限公司"
            :auto-size="{ minRows: 8, maxRows: 20 }"
            style="font-size: 13px;"
          />
        </div>
        <a-space wrap>
          <a-button type="primary" :loading="searching" @click="handleStart">
            <template #icon><icon-search /></template>
            开始逐批搜索
          </a-button>
          <a-button @click="reset">重置</a-button>
        </a-space>
      </a-space>
    </a-card>

    <!-- 完成状态 -->
    <a-card v-else-if="allDone">
      <a-result status="success" :title="`全部 ${batchTotal} 批处理完毕`">
        <template #subtitle>
          共新增订阅 <strong>{{ totalStats.success }}</strong> 个，已存在跳过 <strong>{{ totalStats.skip }}</strong> 个
        </template>
        <template #extra>
          <a-space>
            <a-button type="primary" @click="reset">重新开始</a-button>
            <a-button @click="goBack">返回</a-button>
          </a-space>
        </template>
      </a-result>
    </a-card>

    <!-- 逐批处理阶段 -->
    <template v-else>
      <!-- 批次进度条 -->
      <a-card style="margin-bottom: 16px;">
        <a-space direction="vertical" fill size="small">
          <div style="display:flex; align-items:center; justify-content:space-between;">
            <span style="font-weight:600; font-size:15px;">
              第 {{ batchCurrent }} 批 / 共 {{ batchTotal }} 批
              <a-tag color="blue" style="margin-left:8px;">每批 {{ allChunks[currentChunkIdx]?.length }} 家</a-tag>
            </span>
            <a-space>
              <a-tag color="green">已选 {{ selectedCount }} 个</a-tag>
            </a-space>
          </div>
          <a-progress
            :percent="Math.round((batchCurrent - 1) / batchTotal * 100)"
            status="normal"
          />
        </a-space>
      </a-card>

      <!-- 搜索中 -->
      <div v-if="searching" style="text-align:center; padding: 60px 0;">
        <a-spin size="large" />
        <div style="margin-top:12px; color:#666;">正在搜索第 {{ batchCurrent }} 批...</div>
      </div>

      <!-- 本批结果 -->
      <div v-else>
        <div class="result-list">
          <div
            v-for="(row, rowIdx) in currentResults"
            :key="rowIdx"
            class="result-row"
            :class="{
              'row-error': row.status === 'error',
              'row-not-found': row.status === 'not_found',
              'row-rate-limited': row.status === 'rate_limited'
            }"
          >
            <!-- 公司名 -->
            <div class="company-name">
              <span>{{ row.company }}</span>
              <a-tag v-if="row.subscribed" color="green" size="small">已订阅</a-tag>
              <a-tag v-else-if="row.message === '已订阅（跳过）'" color="orange" size="small">已存在</a-tag>
            </div>

            <!-- 无结果 -->
            <div v-if="row.status === 'not_found'" class="no-result">
              <icon-close-circle style="color:#999;" /> 未找到相关公众号
            </div>
            <div v-else-if="row.status === 'rate_limited'" class="no-result" style="color:#fa8c16;">
              <icon-clock-circle style="color:#fa8c16;" /> 微信频率限制，稍后再试
            </div>
            <div v-else-if="row.status === 'error'" class="no-result">
              <icon-exclamation-circle style="color:#f53;" /> {{ row.message }}
            </div>

            <!-- 候选列表（多选） -->
            <div v-else class="candidates">
              <div
                v-for="(c, ci) in row.candidates"
                :key="ci"
                class="candidate-item"
                :class="{
                  'candidate-selected': row.selected_indices?.includes(ci),
                  'candidate-high': c.confidence === 'high',
                  'candidate-medium': c.confidence === 'medium',
                  'candidate-low': c.confidence === 'low',
                }"
                @click="toggleCandidate(rowIdx, ci)"
              >
                <a-checkbox
                  :model-value="row.selected_indices?.includes(ci)"
                  @click.stop="toggleCandidate(rowIdx, ci)"
                  style="margin-right:8px;"
                />
                <a-avatar :size="32" :image-url="c.avatar" style="flex-shrink:0;" />
                <div class="candidate-info">
                  <div class="candidate-name">
                    {{ c.nickname }}
                    <a-tag v-if="c.verify_status >= 1" color="arcoblue" size="mini">认证</a-tag>
                    <a-tag v-if="c.confidence === 'high'" color="green" size="mini">精确</a-tag>
                    <a-tag v-else-if="c.confidence === 'medium'" color="orange" size="mini">相似</a-tag>
                  </div>
                  <div v-if="c.intro" class="candidate-intro">{{ c.intro }}</div>
                </div>
              </div>
              <!-- 取消选择 -->
              <div v-if="row.selected_indices?.length" class="deselect-link">
                <a-link size="small" @click="currentResults[rowIdx].selected_indices = []">不订阅此公司</a-link>
              </div>
            </div>
          </div>
        </div>

        <!-- 操作按钮 -->
        <div style="margin-top: 20px; display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
          <a-button
            v-if="rateLimitedCount > 0"
            :loading="retrying"
            status="warning"
            @click="handleRetryRateLimited"
          >
            <template #icon><icon-refresh /></template>
            重试频率限制（{{ rateLimitedCount }} 家）
          </a-button>
          <a-button
            type="primary"
            :loading="subscribing"
            @click="handleSubscribeAndNext"
          >
            <template #icon><icon-plus /></template>
            {{ selectedCount > 0 ? `订阅已选(${selectedCount})并继续` : (isLastChunk ? '完成' : '跳过并继续下一批') }}
          </a-button>
          <a-button
            v-if="selectedCount > 0"
            :loading="subscribing"
            @click="handleSkipAndNext"
          >
            {{ isLastChunk ? '不订阅并完成' : '不订阅跳过此批' }}
          </a-button>
          <a-button status="danger" @click="reset">放弃并重置</a-button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.batch-subscribe {
  padding: 20px;
  max-width: 960px;
  margin: 0 auto;
}
.result-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.result-row {
  border: 1px solid #e5e6e8;
  border-radius: 8px;
  padding: 12px 16px;
  background: #fff;
}
.row-not-found {
  background: #fafafa;
  border-color: #f0f0f0;
}
.row-error {
  border-color: #ffa9a9;
  background: #fff8f8;
}
.company-name {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.no-result {
  color: #999;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.candidates {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.candidate-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid #e5e6e8;
  cursor: pointer;
  transition: all 0.15s;
}
.candidate-item:hover {
  border-color: #165dff;
  background: #f2f6ff;
}
.candidate-selected {
  border-color: #165dff !important;
  background: #e8f0ff !important;
}
.candidate-high {
  border-left: 3px solid #00b42a;
}
.candidate-medium {
  border-left: 3px solid #ff7d00;
}
.candidate-low {
  border-left: 3px solid #c9cdd4;
}
.candidate-info {
  flex: 1;
  min-width: 0;
}
.candidate-name {
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 4px;
}
.candidate-intro {
  font-size: 12px;
  color: #999;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.deselect-link {
  text-align: right;
  font-size: 12px;
  margin-top: 2px;
}
</style>
