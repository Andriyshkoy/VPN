export type Money = string

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
  snapshot_id?: number
}

export interface AdminIdentity {
  id?: string | number
  username: string
  display_name?: string
  roles: string[]
  permissions: string[]
  csrf_token?: string
}

export interface DashboardSummary {
  users_total: number
  users_active: number
  configs_active: number
  configs_suspended: number
  servers_healthy: number
  servers_total: number
  payments_today?: Money
  service_revenue_today?: Money
  balance_liability?: Money
  referral_cost_today?: Money
  pending_operations: number
  alerts_active: number
  recent_users?: User[]
  recent_events?: AuditEvent[]
  servers?: Array<{ id: string | number; name?: string; location?: string; health?: string; config_total?: number; config_active?: number; config_suspended?: number }>
  operations_attention?: VpnOperation[]
}

export interface User {
  id: string | number
  tg_id?: string | number
  username?: string | null
  display_name?: string | null
  balance?: Money
  created_at?: string
  last_seen_at?: string | null
  status?: string
  telegram_status?: string
  configs_count?: number
  active_configs_count?: number
  suspended_configs_count?: number
  inviter_id?: string | number | null
  inviter_username?: string | null
  deposits_total?: Money
  service_spend_total?: Money
  referral_rewards_total?: Money
  latest_ledger_entry_id?: number
}

export interface LedgerEntry {
  id: string | number
  user_id?: string | number
  user_username?: string | null
  user_tg_id?: string | number
  kind?: string
  amount: Money
  balance_after?: Money
  description?: string | null
  reason?: string | null
  created_at?: string
  reference_type?: string | null
  reference_id?: string | number | null
}

export interface Payment {
  id: string | number
  user_id?: string | number
  user_username?: string | null
  user_tg_id?: string | number
  provider?: string
  provider_payment_id?: string
  amount: Money
  status?: string
  created_at?: string
  confirmed_at?: string | null
}

export interface VpnConfig {
  id: string | number
  owner_id?: string | number
  owner_username?: string | null
  name?: string
  display_name?: string | null
  server_id?: string | number
  server_name?: string | null
  desired_state?: string
  actual_state?: string
  suspended?: boolean
  created_at?: string
  last_operation_status?: string | null
  operation_attempts?: number
  last_error?: string | null
}

export interface VpnOperation {
  id: string | number
  type?: string
  status?: string
  config_id?: string | number | null
  server_id?: string | number | null
  requested_by?: string | null
  created_at?: string
  updated_at?: string
  error?: string | null
  attempts?: number
}

export interface ReferralNode {
  user_id: string | number
  username?: string | null
  tg_id?: string | number
  level?: number
  registered_at?: string
  deposits_total?: Money
  rewards_total?: Money
  direct_referrals?: number
  children?: ReferralNode[]
}

export interface ReferralReward {
  id: string | number
  beneficiary_id?: string | number
  beneficiary_username?: string | null
  source_user_id?: string | number
  source_username?: string | null
  level?: number
  deposit_amount?: Money
  reward_amount: Money
  created_at?: string
}

export interface BillingRun {
  id: string | number
  period_key?: string
  period_start?: string
  period_end?: string
  cost_per_config?: Money
  status?: string
  charged_users?: number
  total_amount: Money
  created_at?: string
  completed_at?: string | null
}

export interface FinanceOverview {
  deposits: Money
  service_revenue: Money
  config_fees: Money
  referral_costs: Money
  refunds: Money
  balance_liability: Money
  infrastructure_costs: Money
  estimated_margin: Money
  paying_users: number
  average_payment: Money
}

export interface FinancePoint {
  date: string
  deposits: number
  service_revenue: number
  referral_costs: number
  refunds: number
}

export interface Server {
  id: string | number
  name?: string
  location?: string
  region?: string
  provider?: string | null
  host?: string
  port?: number
  ip?: string
  vpn_endpoint?: string
  status?: string
  health?: string
  lifecycle_state?: string
  accepts_new_configs?: boolean
  active_configs?: number
  configs_count?: number
  capacity?: number
  capacity_reserve?: number
  placement_weight?: string
  public_endpoint?: string | null
  api_key_configured?: boolean
  monthly_cost?: Money
  last_seen_at?: string | null
  inventory_revision?: string | null
  drift_count?: number
  version?: number
}

export interface ServerStatus {
  status?: string
  reachable?: boolean
  manager_ready?: boolean
  openvpn_ready?: boolean
  online_sessions?: number
  bytes_received?: number
  bytes_sent?: number
  active_configs?: number
  configs_count?: number
  capacity?: number
  certificate_expires_at?: string | null
  last_checked_at?: string | null
  inventory_revision?: string | null
  manager_version?: string | null
  manager_instance_id?: string | null
  instance_mismatch?: boolean
  error_code?: string | null
  drift?: Array<{ code?: string; severity?: string; message?: string }>
}

export interface ServerCreateInput {
  name: string
  ip: string
  port: number
  host: string
  location: string
  api_key: string
  monthly_cost: Money
  lifecycle_state: 'disabled'
  accepts_new_configs: boolean
  max_configs?: number
  capacity_reserve: number
  placement_weight: string
  provider?: string
  public_endpoint?: string
}

export interface ObservabilitySummary {
  status?: string
  generated_at?: string
  dependencies?: Record<string, boolean | string>
  features?: Record<string, boolean>
  queues?: Array<{ name: string; pending: number; failed: number; oldest_age_seconds?: number }>
  alerts?: Array<{ id?: string; name: string; severity?: string; state?: string; since?: string; summary?: string }>
  metrics?: Record<string, number | string | boolean>
  links?: Array<{ label: string; url: string }>
}

export interface AuditEvent {
  id: string | number
  occurred_at?: string
  actor?: string
  actor_id?: string | number | null
  action?: string
  target_type?: string
  target_id?: string | number | null
  result?: string
  reason?: string | null
  ip_address?: string | null
  metadata?: Record<string, unknown>
}

export interface BalanceAdjustmentInput {
  direction: 'credit' | 'debit'
  amount: Money
  reason_code: string
  comment: string
  expected_balance?: Money
  expected_ledger_entry_id?: number
}
