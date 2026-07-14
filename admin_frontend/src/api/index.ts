import { normalizePage, queryString, request, resetCsrfToken } from './client'
import {
  adaptAudit,
  adaptBillingRun,
  adaptConfig,
  adaptDashboard,
  adaptFinanceOverview,
  adaptFinancePoint,
  adaptIdentity,
  adaptLedger,
  adaptObservability,
  adaptOperation,
  adaptPayment,
  adaptReferralNode,
  adaptReward,
  adaptServer,
  adaptServerStatus,
  adaptUserDetail,
  adaptUserListItem,
  adaptUserTimelineEvent,
} from './adapters'
import type {
  AdminIdentity,
  AuditEvent,
  BalanceAdjustmentInput,
  BillingRun,
  LedgerEntry,
  Page,
  Payment,
  ReferralNode,
  ReferralReward,
  Server,
  ServerCreateInput,
  User,
  UserTimelineEvent,
  VpnConfig,
  VpnOperation,
} from './types'

type ListParams = Record<string, string | number | boolean | null | undefined>

async function page<T>(path: string, params: ListParams): Promise<Page<T>> {
  const payload = await request<unknown>(`${path}${queryString(params)}`)
  return normalizePage<T>(payload, Number(params.limit ?? 25), Number(params.offset ?? 0))
}

async function adaptedPage<T>(path: string, params: ListParams, adapter: (value: unknown) => T): Promise<Page<T>> {
  const result = await page<unknown>(path, params)
  return { ...result, items: result.items.map(adapter) }
}

export const authApi = {
  me: async () => adaptIdentity(await request<unknown>('/auth/me')) as AdminIdentity,
  login: async (username: string, password: string) =>
    adaptIdentity(await request<unknown>('/auth/login', { method: 'POST', body: { username, password } })) as AdminIdentity,
  logout: async () => {
    try {
      await request<void>('/auth/logout', { method: 'POST' })
    } finally {
      resetCsrfToken()
    }
  },
}

export const dashboardApi = {
  summary: async () => adaptDashboard(await request<unknown>('/dashboard')),
}

export const usersApi = {
  list: (params: ListParams) => adaptedPage<User>('/users', normalizeUserFilters(params), adaptUserListItem),
  detail: async (id: string) => adaptUserDetail(await request<unknown>(`/users/${encodeURIComponent(id)}`)),
  ledger: (id: string, params: ListParams = {}) => adaptedPage<LedgerEntry>(`/users/${encodeURIComponent(id)}/ledger`, params, adaptLedger),
  payments: (id: string, params: ListParams = {}) => adaptedPage<Payment>(`/users/${encodeURIComponent(id)}/payments`, params, adaptPayment),
  configs: (id: string, params: ListParams = {}) => adaptedPage<VpnConfig>(`/users/${encodeURIComponent(id)}/configs`, params, adaptConfig),
  operations: (id: string, params: ListParams = {}) => adaptedPage<VpnOperation>(`/users/${encodeURIComponent(id)}/vpn-operations`, params, adaptOperation),
  ancestry: async (id: string) => {
    const payload = await request<unknown>(`/users/${encodeURIComponent(id)}/referrals/ancestry`)
    const items = Array.isArray(payload) ? payload : (payload as { items?: unknown[] })?.items ?? []
    return items.map(adaptReferralNode)
  },
  children: (id: string, params: ListParams = {}) => adaptedPage<ReferralNode>(`/users/${encodeURIComponent(id)}/referrals/children`, params, adaptReferralNode),
  rewards: (id: string, params: ListParams = {}) => adaptedPage<ReferralReward>(`/users/${encodeURIComponent(id)}/referral-rewards`, params, adaptReward),
  timeline: (id: string, params: ListParams = {}) => adaptedPage<UserTimelineEvent>(`/users/${encodeURIComponent(id)}/timeline`, params, adaptUserTimelineEvent),
  adjustBalance: (id: string, input: BalanceAdjustmentInput, key: string) =>
    request<User>(`/users/${encodeURIComponent(id)}/balance-adjustments`, {
      method: 'POST',
      body: input,
      idempotencyKey: key,
    }),
}

export const analyticsApi = {
  overview: async (params: ListParams) => adaptFinanceOverview(await request<unknown>(`/analytics/overview${queryString(normalizePeriod(params))}`)),
  timeseries: async (params: ListParams) => {
    const payload = await request<unknown>(`/analytics/finance/timeseries${queryString({ ...normalizePeriod(params), timezone: ANALYTICS_TIMEZONE })}`)
    const items = Array.isArray(payload) ? payload : (payload as { items?: unknown[] })?.items ?? []
    return items.map(adaptFinancePoint)
  },
}

export const financeApi = {
  ledger: (params: ListParams) => adaptedPage<LedgerEntry>('/finance/ledger', normalizePeriod(params), adaptLedger),
  payments: (params: ListParams) => adaptedPage<Payment>('/finance/payments', normalizePeriod(params), adaptPayment),
  billingRuns: (params: ListParams) => adaptedPage<BillingRun>('/finance/billing-runs', normalizePeriod(params), adaptBillingRun),
  rewards: (params: ListParams) => adaptedPage<ReferralReward>('/finance/referral-rewards', normalizePeriod(params), adaptReward),
}

export const referralsApi = {
  tree: (params: ListParams) => adaptedPage<ReferralNode>('/referrals/tree', params, adaptReferralNode),
}

export const configsApi = {
  list: (params: ListParams) => adaptedPage<VpnConfig>('/configs', params, adaptConfig),
  detail: async (id: string) => adaptConfig(await request<unknown>(`/configs/${encodeURIComponent(id)}`)),
  action: async (id: string, action: 'suspend' | 'unsuspend' | 'revoke', reason: string) => adaptConfig(await request<unknown>(`/configs/${encodeURIComponent(id)}/actions`, { method: 'POST', body: { action, reason }, idempotencyKey: crypto.randomUUID() })),
}

export const serversApi = {
  list: (params: ListParams) => adaptedPage<Server>('/servers', normalizeServerFilters(params), adaptServer),
  detail: async (id: string) => adaptServer(await request<unknown>(`/servers/${encodeURIComponent(id)}`)),
  status: async (id: string) => adaptServerStatus(await request<unknown>(`/servers/${encodeURIComponent(id)}/status`)),
  create: async (input: ServerCreateInput) => adaptServer(await request<unknown>('/servers', { method: 'POST', body: input })),
  update: async (id: string, input: Record<string, unknown>) => adaptServer(await request<unknown>(`/servers/${encodeURIComponent(id)}`, { method: 'PATCH', body: input })),
  action: async (id: string, action: string, input: Record<string, unknown> = {}, idempotencyKey: string = crypto.randomUUID()) =>
    adaptOperation(await request<unknown>(`/servers/${encodeURIComponent(id)}/actions`, {
      method: 'POST',
      body: { action, ...input },
      idempotencyKey,
    })),
}

export const operationsApi = {
  list: (params: ListParams) => adaptedPage<VpnOperation>('/operations', { ...params, kind: params.type, type: undefined }, adaptOperation),
}

export const observabilityApi = {
  summary: async () => adaptObservability(await request<unknown>('/observability/summary')),
}

export const auditApi = {
  list: (params: ListParams) => adaptedPage<AuditEvent>('/audit-events', params, adaptAudit),
}

function normalizeUserFilters(params: ListParams): ListParams {
  const sortValue = String(params.sort ?? 'created_at')
  const descending = sortValue.startsWith('-')
  const configStatus = params.config_status
  return {
    ...params,
    status: undefined,
    config_status: undefined,
    delivery_status: params.status,
    has_configs: configStatus === 'none' ? false : undefined,
    config_state: configStatus && configStatus !== 'none' ? configStatus : undefined,
    sort: sortValue.replace(/^-/, ''),
    order: descending ? 'desc' : 'asc',
  }
}

function normalizeServerFilters(params: ListParams): ListParams {
  const requested = params.status
  return {
    ...params,
    status: undefined,
    lifecycle_state: requested && requested !== 'unhealthy' ? requested : undefined,
    health_state: requested === 'unhealthy' ? 'unhealthy' : undefined,
  }
}

const ANALYTICS_TIMEZONE = 'Asia/Novosibirsk'
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

function normalizePeriod(params: ListParams): ListParams {
  const from = typeof params.from === 'string' && DATE_ONLY.test(params.from) ? zonedDayStart(params.from, ANALYTICS_TIMEZONE) : params.from
  const to = typeof params.to === 'string' && DATE_ONLY.test(params.to) ? zonedDayStart(addCalendarDays(params.to, 1), ANALYTICS_TIMEZONE) : params.to
  return { ...params, from, to }
}

function addCalendarDays(value: string, days: number) {
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1, day + days))
  return date.toISOString().slice(0, 10)
}

/** Convert a calendar-day boundary in the reporting timezone to an exact UTC instant. */
function zonedDayStart(value: string, timeZone: string) {
  const [year, month, day] = value.split('-').map(Number)
  const targetWallClock = Date.UTC(year, month - 1, day)
  let instant = targetWallClock
  for (let pass = 0; pass < 2; pass += 1) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(new Date(instant))
    const part = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((item) => item.type === type)?.value ?? 0)
    const representedWallClock = Date.UTC(part('year'), part('month') - 1, part('day'), part('hour'), part('minute'), part('second'))
    instant += targetWallClock - representedWallClock
  }
  return new Date(instant).toISOString()
}

export * from './client'
export type * from './types'
