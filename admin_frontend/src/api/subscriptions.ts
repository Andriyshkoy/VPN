import { normalizePage, queryString, request } from './client'
import type { Money, Page } from './types'

export type PlanCode = 'basic' | 'plus'
export interface SubscriptionPlan {
  code: PlanCode
  name: string
  hourly_rate_rub: Money
  currency: string
  monthly_price_rub: Money
  monthly_estimate_rub: Money
  services: string[]
  max_sessions: number | null
}
export interface SubscriptionView {
  id: string | number
  user_id: string | number
  plan_code: PlanCode
  status: string
  hourly_rate_rub: Money
  monthly_price_rub: Money
  monthly_estimate_rub: Money
  max_sessions: number | null
  version: number
  next_charge_at?: string | null
  billing_paused: boolean
  unlimited_balance?: boolean
  services: string[]
  accesses: Array<{ service_id: string; config_id: string | number | null; status: string; remote_access_id?: string | null }>
}
export interface UnlimitedBalanceResult {
  user_id: string | number
  unlimited_balance: boolean
  balance: Money
  balance_display: string
  replayed: boolean
}
export interface SubscriptionMigrationPreview {
  billing_enabled: boolean
  read_only: boolean
  counts: { basic: number; plus: number; suspended: number; unchanged: number; total: number }
  items: Array<{ user_id: string | number; active_configs: number; plan_code: PlanCode | null; status: string; reason: string }>
  balance_changes: number
  ledger_changes: number
  unlimited_balance_changes: number
}
export const subscriptionsApi = {
  plans: () => request<{ items: SubscriptionPlan[] }>('/subscriptions/plans'),
  migrationPreview: () => request<SubscriptionMigrationPreview>('/subscriptions/migration/preview'),
  list: async (params: Record<string, string | number | undefined> = {}): Promise<Page<SubscriptionView>> => normalizePage<SubscriptionView>(await request(`/subscriptions${queryString(params)}`)),
  user: (id: string) => request<{ subscription: SubscriptionView | null }>(`/users/${id}/subscription`),
  changePlan: (id: string, input: { plan_code: PlanCode; reason: string; expected_version?: number }, key: string) => request<SubscriptionView>(`/users/${id}/subscription`, { method: 'PATCH', body: input, idempotencyKey: key }),
  setUnlimitedBalance: (id: string, input: { enabled: boolean; expected_enabled: boolean; reason: string }, key: string) => request<UnlimitedBalanceResult>(`/users/${id}/unlimited-balance`, { method: 'PATCH', body: input, idempotencyKey: key }),
}

export function planLabel(code: string) { return code === 'basic' ? 'Basic' : code === 'plus' ? 'Plus' : code }
export function serviceLabel(service: string) { return service === 'hysteria' ? 'Hysteria 2' : service === 'vless-reality' ? 'VLESS / REALITY' : service }
