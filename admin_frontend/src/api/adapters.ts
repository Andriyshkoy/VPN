import type {
  AdminIdentity,
  AuditEvent,
  BillingRun,
  DashboardSummary,
  FinanceOverview,
  FinancePoint,
  LedgerEntry,
  ObservabilitySummary,
  Payment,
  ReferralNode,
  ReferralReward,
  Server,
  ServerStatus,
  User,
  VpnConfig,
  VpnOperation,
} from './types'

type Json = Record<string, any>
const obj = (value: unknown): Json => value && typeof value === 'object' ? value as Json : {}
const array = (value: unknown): Json[] => Array.isArray(value) ? value.map(obj) : []
const text = (value: unknown, fallback = '') => value === null || value === undefined ? fallback : String(value)
const number = (value: unknown, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback
const money = (value: unknown) => text(value, '0.00')
const optionalMoney = (record: Json, key: string) => key in record && record[key] !== null ? money(record[key]) : undefined

function cents(value: unknown): bigint {
  const normalized = money(value).trim().replace(',', '.')
  const match = normalized.match(/^(-?)(\d+)(?:\.(\d{0,2}))?$/)
  if (!match) return 0n
  const absolute = BigInt(match[2]) * 100n + BigInt((match[3] ?? '').padEnd(2, '0'))
  return match[1] ? -absolute : absolute
}

function fromCents(value: bigint): string {
  const negative = value < 0n
  const absolute = negative ? -value : value
  return `${negative ? '-' : ''}${absolute / 100n}.${String(absolute % 100n).padStart(2, '0')}`
}

function sumMoney(...values: unknown[]) { return fromCents(values.reduce<bigint>((total, value) => total + cents(value), 0n)) }

export function adaptIdentity(payload: unknown): AdminIdentity | null {
  const root = obj(payload)
  const actor = obj(root.actor ?? root.user ?? root)
  if (!actor.username) return null
  return {
    id: actor.id,
    username: text(actor.username),
    display_name: actor.display_name ? text(actor.display_name) : undefined,
    roles: Array.isArray(actor.roles) ? actor.roles.map(String) : actor.role ? [text(actor.role)] : ['admin'],
    permissions: Array.isArray(actor.permissions) ? actor.permissions.map(String) : [],
    csrf_token: root.csrf_token ? text(root.csrf_token) : undefined,
  }
}

export function adaptUserListItem(value: unknown): User {
  const raw = obj(value)
  const counts = obj(raw.config_counts)
  const referrer = obj(raw.referrer)
  const delivery = text(raw.delivery_status ?? raw.telegram_status, 'unknown')
  const finance = {
    ...(optionalMoney(raw, 'balance') !== undefined ? { balance: optionalMoney(raw, 'balance') } : {}),
    ...(optionalMoney(raw, 'credited_total') !== undefined ? { deposits_total: optionalMoney(raw, 'credited_total') } : optionalMoney(raw, 'deposits_total') !== undefined ? { deposits_total: optionalMoney(raw, 'deposits_total') } : {}),
    ...(optionalMoney(raw, 'service_spend_total') !== undefined ? { service_spend_total: optionalMoney(raw, 'service_spend_total') } : {}),
    ...(optionalMoney(raw, 'referral_rewards_total') !== undefined ? { referral_rewards_total: optionalMoney(raw, 'referral_rewards_total') } : {}),
  }
  const referral = raw.referrer !== undefined || raw.inviter_id !== undefined ? {
    inviter_id: referrer.id ?? raw.inviter_id,
    inviter_username: referrer.username ?? raw.inviter_username,
  } : {}
  return {
    id: raw.id,
    tg_id: raw.tg_id,
    username: raw.username ?? null,
    created_at: raw.created_at,
    last_seen_at: raw.last_seen_at ?? raw.last_payment_at,
    status: delivery,
    telegram_status: delivery,
    configs_count: number(counts.total ?? raw.configs_count),
    active_configs_count: number(counts.active ?? raw.active_configs_count),
    suspended_configs_count: number(counts.suspended ?? raw.suspended_configs_count),
    ...finance,
    ...referral,
  }
}

export function adaptUserDetail(value: unknown): User {
  const raw = obj(value)
  if (!raw.identity) return adaptUserListItem(raw)
  const identity = obj(raw.identity)
  const finance = obj(raw.finance)
  const configs = obj(raw.configs)
  const referral = obj(raw.referral)
  const referrer = obj(referral.referrer)
  const delivery = text(identity.delivery_status, 'unknown')
  const hasFinance = raw.finance !== undefined && raw.finance !== null
  const hasReferral = raw.referral !== undefined && raw.referral !== null
  return {
    id: identity.id,
    tg_id: identity.tg_id,
    username: identity.username ?? null,
    created_at: identity.created_at,
    last_seen_at: finance.last_payment_at,
    status: delivery,
    telegram_status: delivery,
    configs_count: number(configs.total),
    active_configs_count: number(configs.active),
    suspended_configs_count: number(configs.suspended),
    ...(hasFinance ? {
      balance: money(finance.balance),
      deposits_total: money(finance.provider_deposits),
      service_spend_total: sumMoney(finance.service_charges, finance.config_fees),
      latest_ledger_entry_id: finance.latest_ledger_entry_id === null || finance.latest_ledger_entry_id === undefined ? undefined : number(finance.latest_ledger_entry_id),
    } : {}),
    ...(hasReferral ? {
      inviter_id: referrer.id,
      inviter_username: referrer.username,
      referral_rewards_total: money(referral.total_earned ?? finance.referral_rewards),
    } : {}),
  }
}

export function adaptLedger(value: unknown): LedgerEntry {
  const raw = obj(value)
  const details = obj(raw.details)
  const user = obj(raw.user)
  return { id: raw.id, user_id: user.id ?? raw.user_id, user_username: user.username ?? raw.user_username, user_tg_id: user.tg_id ?? raw.user_tg_id, kind: raw.kind, amount: money(raw.amount), balance_after: money(raw.balance_after), description: details.comment ?? details.description, reason: details.reason_code ?? details.reason, created_at: raw.created_at, reference_type: raw.reference_type, reference_id: raw.reference_id }
}

export function adaptPayment(value: unknown): Payment {
  const raw = obj(value)
  const user = obj(raw.user)
  return { id: raw.id, user_id: user.id ?? raw.user_id, user_username: user.username ?? raw.user_username, user_tg_id: user.tg_id ?? raw.user_tg_id, provider: raw.provider, provider_payment_id: raw.provider_payment_id ?? raw.intent_id, amount: money(raw.amount), status: raw.status, created_at: raw.created_at, confirmed_at: raw.credited_at ?? raw.confirmed_at }
}

export function adaptBillingRun(value: unknown): BillingRun {
  const raw = obj(value)
  return { id: raw.id, period_key: raw.period_key, period_start: raw.period_start, period_end: raw.period_end, cost_per_config: money(raw.cost_per_config), status: raw.status, charged_users: number(raw.charged_users), total_amount: money(raw.total_amount), created_at: raw.created_at, completed_at: raw.completed_at }
}

export function adaptConfig(value: unknown): VpnConfig {
  const raw = obj(value)
  const owner = obj(raw.owner)
  const server = obj(raw.server)
  return {
    id: raw.id,
    owner_id: owner.id ?? raw.owner_id,
    owner_username: owner.username ?? raw.owner_username,
    name: raw.name,
    display_name: raw.display_name,
    server_id: server.id ?? raw.server_id,
    server_name: server.name ?? raw.server_name,
    desired_state: raw.desired_state,
    actual_state: raw.actual_state,
    suspended: Boolean(raw.suspended),
    created_at: raw.created_at,
    last_operation_status: raw.operation_status ?? raw.last_operation_status,
    operation_attempts: number(raw.operation_attempts),
    last_error: raw.last_error,
  }
}

export function adaptOperation(value: unknown): VpnOperation {
  const raw = obj(value)
  return { id: raw.operation_id ?? raw.id, type: raw.kind ?? raw.type, status: raw.status, config_id: raw.config_id, server_id: raw.server_id, requested_by: raw.requested_by, created_at: raw.created_at, updated_at: raw.updated_at, error: raw.last_error ?? raw.error, attempts: number(raw.attempts) }
}

export function adaptReferralNode(value: unknown): ReferralNode {
  const raw = obj(value)
  return { user_id: raw.user_id ?? raw.id, username: raw.username, tg_id: raw.tg_id, level: number(raw.level ?? raw.depth), registered_at: raw.registered_at ?? raw.created_at, deposits_total: money(raw.deposits_total ?? raw.provider_deposits), rewards_total: money(raw.rewards_total ?? raw.reward_generated), direct_referrals: number(raw.direct_referrals ?? raw.direct_children), children: Array.isArray(raw.children) ? raw.children.map(adaptReferralNode) : undefined }
}

export function adaptReward(value: unknown): ReferralReward {
  const raw = obj(value)
  const source = obj(raw.source_user)
  const beneficiary = obj(raw.beneficiary)
  return { id: raw.id, beneficiary_id: beneficiary.id ?? raw.beneficiary_id ?? raw.beneficiary_user_id, beneficiary_username: beneficiary.username ?? raw.beneficiary_username, source_user_id: source.id ?? raw.source_user_id, source_username: source.username ?? raw.source_username, level: number(raw.level), deposit_amount: money(raw.source_amount ?? raw.deposit_amount), reward_amount: money(raw.reward_amount), created_at: raw.created_at }
}

export function adaptFinanceOverview(value: unknown): FinanceOverview {
  const raw = obj(value)
  const finance = obj(raw.finance ?? raw)
  const users = obj(raw.users)
  const paying = number(users.paying ?? raw.paying_users)
  const cashIn = money(finance.cash_in ?? raw.deposits)
  const average = paying ? (Number(cashIn) / paying).toFixed(2) : '0.00'
  return {
    deposits: cashIn,
    service_revenue: money(finance.recognized_revenue ?? raw.service_revenue),
    config_fees: money(finance.config_fees ?? raw.config_fees),
    referral_costs: money(finance.referral_rewards ?? raw.referral_costs),
    refunds: money(finance.config_refunds ?? raw.refunds),
    balance_liability: money(finance.wallet_liability ?? raw.balance_liability),
    infrastructure_costs: money(finance.allocated_infrastructure_cost ?? raw.infrastructure_costs),
    estimated_margin: money(finance.estimated_margin ?? raw.estimated_margin),
    paying_users: paying,
    average_payment: money(raw.average_payment ?? average),
  }
}

export function adaptFinancePoint(value: unknown): FinancePoint {
  const raw = obj(value)
  return { date: text(raw.bucket ?? raw.date), deposits: number(raw.cash_in ?? raw.deposits), service_revenue: number(raw.recognized_revenue ?? raw.service_revenue), referral_costs: number(raw.referral_rewards ?? raw.referral_costs), refunds: number(raw.config_refunds ?? raw.refunds) }
}

export function adaptDashboard(value: unknown): DashboardSummary {
  const raw = obj(value)
  const users = obj(raw.users)
  const configs = obj(raw.configs)
  const finance = obj(raw.finance)
  const delivery = obj(users.delivery)
  const operations = obj(raw.operations)
  const servers = array(raw.servers)
  const normalizedServers = servers.map((server) => ({
    id: server.id,
    name: server.name,
    location: server.location,
    health: server.health,
    config_total: number(server.configs_count ?? server.config_total),
    config_active: number(server.active_configs ?? server.config_active),
    config_suspended: number(server.suspended_configs ?? server.config_suspended),
  }))
  const attention = array(operations.attention).map(adaptOperation)
  const blocked = number(delivery.blocked)
  return {
    users_total: number(users.total ?? raw.users_total),
    users_active: number(delivery.active, Math.max(0, number(users.total ?? raw.users_total) - blocked)),
    configs_active: number(configs.active ?? raw.configs_active),
    configs_suspended: number(configs.suspended ?? raw.configs_suspended),
    servers_healthy: normalizedServers.filter((server) => server.health === 'healthy').length,
    servers_total: number(raw.server_total, normalizedServers.length),
    ...(raw.finance !== undefined && raw.finance !== null ? {
      payments_today: money(finance.cash_in ?? raw.payments_today),
      service_revenue_today: money(finance.recognized_revenue ?? raw.service_revenue_today),
      balance_liability: money(finance.wallet_liability ?? raw.balance_liability),
      referral_cost_today: money(finance.referral_rewards ?? raw.referral_cost_today),
    } : {}),
    pending_operations: attention.filter((item) => item.status === 'pending' || item.status === 'running').length,
    alerts_active: number(raw.alerts_active),
    servers: raw.servers === undefined ? undefined : normalizedServers,
    operations_attention: attention,
  }
}

export function adaptAudit(value: unknown): AuditEvent {
  const raw = obj(value)
  const actor = obj(raw.actor)
  const details = obj(raw.details ?? raw.metadata)
  return { id: raw.id, occurred_at: raw.created_at ?? raw.occurred_at, actor: actor.username ?? raw.actor ?? 'system', actor_id: actor.id ?? raw.actor_id, action: raw.action, target_type: raw.target_type, target_id: raw.target_id, result: details.outcome ?? raw.result ?? 'success', reason: details.reason ?? details.comment ?? raw.reason, ip_address: raw.ip_address, metadata: details }
}

export function adaptObservability(value: unknown): ObservabilitySummary {
  const raw = obj(value)
  const features = obj(raw.features)
  const operationCounts = obj(raw.vpn_operations)
  const outbox = obj(raw.notification_outbox)
  const telegram = obj(raw.telegram_inbox)
  const sum = (record: Json, keys: string[]) => keys.reduce((total, key) => total + number(record[key]), 0)
  const canonicalQueues = array(raw.queues)
  const fallbackQueues = [
    { name: 'VPN operations', pending: sum(operationCounts, ['pending', 'running']), failed: number(operationCounts.failed), oldest_age_seconds: undefined },
    { name: 'Notification outbox', pending: sum(outbox, ['pending', 'retrying']), failed: number(outbox.dead ?? outbox.failed), oldest_age_seconds: undefined },
    { name: 'Telegram inbox', pending: sum(telegram, ['pending', 'processing']), failed: number(telegram.dead ?? telegram.failed), oldest_age_seconds: undefined },
  ]
  return {
    status: raw.status,
    generated_at: raw.generated_at,
    dependencies: obj(raw.dependencies),
    features: Object.fromEntries(Object.entries(features).map(([key, enabled]) => [key, Boolean(enabled)])),
    queues: (canonicalQueues.length ? canonicalQueues : fallbackQueues).map((queue) => ({ name: text(queue.name, 'queue'), pending: number(queue.pending), failed: number(queue.failed), oldest_age_seconds: queue.oldest_age_seconds === null || queue.oldest_age_seconds === undefined ? undefined : number(queue.oldest_age_seconds) })),
    alerts: array(raw.alerts).map((alert) => ({ id: alert.id, name: text(alert.name, 'Alert'), severity: alert.severity, state: alert.state, since: alert.since, summary: alert.summary })),
    metrics: { ...obj(raw.metrics), manager_tls_ready: Boolean(obj(raw.manager_tls).ready) },
    links: array(raw.links).map((link) => ({ label: text(link.label, 'Открыть'), url: text(link.url) })).filter((link) => link.url),
  }
}

export function adaptServer(value: unknown): Server { return obj(value) as Server }
export function adaptServerStatus(value: unknown): ServerStatus { return obj(value) as ServerStatus }
