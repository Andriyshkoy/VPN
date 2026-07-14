import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, CircleDollarSign, ExternalLink, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  auditApi,
  usersApi,
  type AuditEvent,
  type LedgerEntry,
  type Payment,
  type ReferralNode,
  type ReferralReward,
  type VpnConfig,
  type VpnOperation,
} from '../api'
import { useAuth } from '../auth/AuthProvider'
import { BalanceAdjustmentDialog } from '../components/BalanceAdjustmentDialog'
import { DataTable, type Column } from '../components/DataTable'
import { PageHeader, StatCard } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, ForbiddenState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatMoney, formatNumber, shortId, userLabel } from '../lib/format'

const DETAIL_PAGE_SIZE = 25

function pageOffset(params: URLSearchParams, key: string) {
  const value = Number(params.get(key) ?? 0)
  return Number.isInteger(value) && value >= 0 ? value : 0
}

function updatePage(params: URLSearchParams, setParams: (next: URLSearchParams) => void, key: string, offset: number, extras: Record<string, string | undefined> = {}) {
  const next = new URLSearchParams(params)
  if (offset) next.set(key, String(offset)); else next.delete(key)
  Object.entries(extras).forEach(([name, value]) => { if (value) next.set(name, value); else next.delete(name) })
  setParams(next)
}

const tabs = [
  { id: 'overview', label: 'Обзор', permission: 'users:read' },
  { id: 'finance', label: 'Финансы', permission: 'balance:read' },
  { id: 'configs', label: 'Конфигурации', permission: 'configs:read' },
  { id: 'referrals', label: 'Рефералы', permission: 'referrals:read' },
  { id: 'audit', label: 'Аудит', permission: 'audit:read' },
]

export function UserDetailPage() {
  const { user: admin } = useAuth()
  const permissions = admin?.permissions ?? []
  const canReadBalance = permissions.includes('balance:read')
  const canReadReferrals = permissions.includes('referrals:read')
  const canReadConfigs = permissions.includes('configs:read')
  const canReadServers = permissions.includes('servers:read')
  const canReadOperations = canReadConfigs || canReadServers
  const canAdjustBalance = permissions.includes('balance:write')
  const { userId = '', tab = 'overview' } = useParams()
  const navigate = useNavigate()
  const [balanceOpen, setBalanceOpen] = useState(false)
  const userQuery = useQuery({ queryKey: ['user', userId], queryFn: () => usersApi.detail(userId), enabled: Boolean(userId) })
  if (!tabs.some((item) => item.id === tab)) return <Navigate to={`/users/${userId}/overview`} replace />
  const tabAllowed = tabs.find((item) => item.id === tab)?.permission
  const canOpenTab = tabAllowed ? admin?.permissions.includes(tabAllowed) ?? false : false
  if (userQuery.isLoading) return <PageLoading />
  if (userQuery.isError) return <ErrorState error={userQuery.error} retry={() => void userQuery.refetch()} />
  const user = userQuery.data!
  return (
    <>
      <button type="button" className="back-link" onClick={() => navigate('/users')}><ArrowLeft size={16} /> К пользователям</button>
      <PageHeader title={userLabel(user)} description={`ID ${user.id}${user.tg_id ? ` · Telegram ${user.tg_id}` : ''}`} actions={<><StatusBadge value={user.status ?? 'active'} />{canAdjustBalance && user.balance !== undefined && <button className="button button--primary" type="button" onClick={() => setBalanceOpen(true)}><CircleDollarSign size={17} /> Изменить баланс</button>}</>} />
      <section className="user-summary">
        <div className="user-summary__identity"><span className="avatar avatar--large">{userLabel(user).replace('@', '').slice(0, 1).toUpperCase()}</span><div><strong>{user.display_name || userLabel(user)}</strong><span>{user.username ? `@${user.username.replace('@', '')}` : 'Username не указан'}</span><small>Зарегистрирован {formatDateTime(user.created_at)}</small></div></div>
        <div className="user-summary__stats">{canReadBalance && <><StatCard label="Баланс" value={formatMoney(user.balance)} /><StatCard label="Пополнения" value={formatMoney(user.deposits_total)} /><StatCard label="Потребление" value={formatMoney(user.service_spend_total)} /></>}<StatCard label="Активные конфиги" value={formatNumber(user.active_configs_count ?? user.configs_count)} /></div>
      </section>
      <nav className="tabs" aria-label="Разделы пользователя">{tabs.filter((item) => admin?.permissions.includes(item.permission)).map((item) => <Link key={item.id} className={tab === item.id ? 'tab tab--active' : 'tab'} to={`/users/${userId}/${item.id}`}>{item.label}</Link>)}</nav>
      {!canOpenTab ? <ForbiddenState /> : <>{tab === 'overview' && <UserOverview userId={userId} user={user} canReadBalance={canReadBalance} canReadReferrals={canReadReferrals} canReadConfigs={canReadConfigs} canReadOperations={canReadOperations} />}{tab === 'finance' && <UserFinance userId={userId} />}{tab === 'configs' && <UserConfigs userId={userId} canReadServers={canReadServers} />}{tab === 'referrals' && <UserReferrals userId={userId} />}{tab === 'audit' && <UserAudit userId={userId} />}</>}
      {canAdjustBalance && user.balance !== undefined && <BalanceAdjustmentDialog user={user} open={balanceOpen} onClose={() => setBalanceOpen(false)} />}
    </>
  )
}

function UserOverview({ userId, user, canReadBalance, canReadReferrals, canReadConfigs, canReadOperations }: { userId: string; user: { inviter_id?: string | number | null; inviter_username?: string | null; telegram_status?: string; suspended_configs_count?: number; referral_rewards_total?: string; last_seen_at?: string | null }; canReadBalance: boolean; canReadReferrals: boolean; canReadConfigs: boolean; canReadOperations: boolean }) {
  const operations = useQuery({ queryKey: ['user-operations', userId, 'preview'], queryFn: () => usersApi.operations(userId, { limit: 5 }), enabled: canReadOperations })
  return <div className={canReadOperations ? 'content-grid content-grid--two' : 'stack'}><section className="panel detail-list"><header className="panel__header"><div><h2>Профиль</h2><p>Текущее состояние аккаунта</p></div></header><dl><div><dt>Статус Telegram</dt><dd><StatusBadge value={user.telegram_status ?? 'unknown'} /></dd></div>{canReadBalance && <div><dt>Последнее пополнение</dt><dd>{formatDateTime(user.last_seen_at)}</dd></div>}{canReadReferrals && <><div><dt>Пригласивший</dt><dd>{user.inviter_id ? <Link to={`/users/${user.inviter_id}`}>{user.inviter_username ? `@${user.inviter_username}` : `Пользователь #${user.inviter_id}`}</Link> : 'Прямой пользователь'}</dd></div><div><dt>Реферальные начисления</dt><dd>{formatMoney(user.referral_rewards_total)}</dd></div></>}<div><dt>Приостановленные конфиги</dt><dd>{formatNumber(user.suspended_configs_count)}</dd></div></dl></section>{canReadOperations && <section className="panel"><header className="panel__header"><div><h2>Последние VPN-операции</h2><p>Provisioning и управление конфигурациями</p></div>{canReadConfigs && <Link to={`/users/${userId}/configs`}>Все операции</Link>}</header>{operations.isLoading ? <PageLoading /> : operations.isError ? <ErrorState error={operations.error} /> : operations.data!.items.length ? <OperationsTable rows={operations.data!.items} /> : <EmptyState title="Операций ещё не было" />}</section>}</div>
}

function UserFinance({ userId }: { userId: string }) {
  const [params, setParams] = useSearchParams()
  const ledgerOffset = pageOffset(params, 'ledger_offset')
  const paymentOffset = pageOffset(params, 'payment_offset')
  const ledgerSnapshot = ledgerOffset ? params.get('ledger_snapshot') ?? undefined : undefined
  const ledgerDirection = params.get('ledger_direction') ?? ''
  const paymentStatus = params.get('payment_status') ?? ''
  const ledger = useQuery({ queryKey: ['user-ledger', userId, ledgerDirection, ledgerOffset, ledgerSnapshot], queryFn: () => usersApi.ledger(userId, { direction: ledgerDirection || undefined, limit: DETAIL_PAGE_SIZE, offset: ledgerOffset, snapshot_id: ledgerSnapshot }), placeholderData: (previous) => previous })
  const payments = useQuery({ queryKey: ['user-payments', userId, paymentStatus, paymentOffset], queryFn: () => usersApi.payments(userId, { status: paymentStatus || undefined, limit: DETAIL_PAGE_SIZE, offset: paymentOffset }), placeholderData: (previous) => previous })
  const setFinanceFilter = (key: string, value: string, offsetKey: string, resetSnapshot = false) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    next.delete(offsetKey)
    if (resetSnapshot) next.delete('ledger_snapshot')
    setParams(next)
  }
  const ledgerColumns: Column<LedgerEntry>[] = [
    { key: 'date', header: 'Дата', render: (row) => formatDateTime(row.created_at) },
    { key: 'type', header: 'Операция', render: (row) => <span className="mono-soft">{row.kind || row.reference_type || 'операция'}</span> },
    { key: 'description', header: 'Описание', render: (row) => row.description || row.reason || '—' },
    { key: 'amount', header: 'Сумма', align: 'end', render: (row) => <strong className={Number(row.amount) >= 0 ? 'money-positive' : 'money-negative'}>{Number(row.amount) > 0 ? '+' : ''}{formatMoney(row.amount)}</strong> },
    { key: 'balance', header: 'Баланс после', align: 'end', render: (row) => formatMoney(row.balance_after) },
  ]
  const paymentColumns: Column<Payment>[] = [
    { key: 'date', header: 'Дата', render: (row) => formatDateTime(row.created_at) },
    { key: 'id', header: 'Платёж', render: (row) => <span className="mono-soft" title={row.provider_payment_id}>{row.provider || 'provider'} · {shortId(row.provider_payment_id || row.id)}</span> },
    { key: 'status', header: 'Статус', render: (row) => <StatusBadge value={row.status} /> },
    { key: 'amount', header: 'Сумма', align: 'end', render: (row) => <strong>{formatMoney(row.amount)}</strong> },
  ]
  return <div className="stack"><section className="panel"><header className="panel__header"><div><h2>Движение баланса</h2><p>Неизменяемый ledger пользователя</p></div><label className="inline-select">Операции <select aria-label="Тип движения баланса" value={ledgerDirection} onChange={(event) => setFinanceFilter('ledger_direction', event.target.value, 'ledger_offset', true)}><option value="">Все</option><option value="credit">Пополнения</option><option value="debit">Списания</option></select></label></header>{ledger.isLoading ? <PageLoading /> : ledger.isError ? <ErrorState error={ledger.error} /> : ledger.data!.items.length ? <><DataTable label="Движение баланса" columns={ledgerColumns} rows={ledger.data!.items} rowKey={(row) => row.id} /><Pagination offset={ledger.data!.offset} limit={ledger.data!.limit} total={ledger.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'ledger_offset', nextOffset, { ledger_snapshot: nextOffset ? String(ledger.data!.snapshot_id ?? ledgerSnapshot ?? '') : undefined })} /></> : <EmptyState title="Движений пока нет" />}</section><section className="panel"><header className="panel__header"><div><h2>Платежи</h2><p>Операции платёжного провайдера</p></div><label className="inline-select">Статус <select aria-label="Статус платежей" value={paymentStatus} onChange={(event) => setFinanceFilter('payment_status', event.target.value, 'payment_offset')}><option value="">Все</option><option value="pending">Ожидают</option><option value="credited">Зачислены</option><option value="expired">Истекли</option><option value="cancelled">Отменены</option><option value="failed">Ошибки</option></select></label></header>{payments.isLoading ? <PageLoading /> : payments.isError ? <ErrorState error={payments.error} /> : payments.data!.items.length ? <><DataTable label="Платежи" columns={paymentColumns} rows={payments.data!.items} rowKey={(row) => row.id} /><Pagination offset={payments.data!.offset} limit={payments.data!.limit} total={payments.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'payment_offset', nextOffset)} /></> : <EmptyState title="Платежей пока нет" />}</section></div>
}

function UserConfigs({ userId, canReadServers }: { userId: string; canReadServers: boolean }) {
  const [params, setParams] = useSearchParams()
  const configOffset = pageOffset(params, 'config_offset')
  const operationOffset = pageOffset(params, 'operation_offset')
  const configs = useQuery({ queryKey: ['user-configs', userId, configOffset], queryFn: () => usersApi.configs(userId, { limit: DETAIL_PAGE_SIZE, offset: configOffset }), placeholderData: (previous) => previous })
  const operations = useQuery({ queryKey: ['user-operations', userId, operationOffset], queryFn: () => usersApi.operations(userId, { limit: DETAIL_PAGE_SIZE, offset: operationOffset }), placeholderData: (previous) => previous })
  const columns: Column<VpnConfig>[] = [
    { key: 'name', header: 'Конфигурация', render: (row) => <span><strong>{row.display_name || row.name || `#${row.id}`}</strong><small className="cell-note">#{row.id}</small></span> },
    { key: 'server', header: 'Сервер', render: (row) => row.server_id ? canReadServers ? <Link to={`/servers/${row.server_id}`}>{row.server_name || `#${row.server_id}`}</Link> : row.server_name || `#${row.server_id}` : '—' },
    { key: 'desired', header: 'Желаемое состояние', render: (row) => <StatusBadge value={row.desired_state ?? (row.suspended ? 'suspended' : 'active')} /> },
    { key: 'actual', header: 'Фактическое состояние', render: (row) => <StatusBadge value={row.actual_state ?? row.last_operation_status} /> },
    { key: 'created', header: 'Создана', render: (row) => formatDateTime(row.created_at) },
    { key: 'error', header: 'Последняя ошибка', render: (row) => row.last_error ? <span className="error-inline" title={row.last_error}><ShieldAlert size={15} /> {row.last_error}</span> : '—' },
  ]
  return <div className="stack"><section className="panel"><header className="panel__header"><div><h2>Конфигурации</h2><p>Desired и actual state</p></div></header>{configs.isLoading ? <PageLoading /> : configs.isError ? <ErrorState error={configs.error} /> : configs.data!.items.length ? <><DataTable label="Конфигурации пользователя" columns={columns} rows={configs.data!.items} rowKey={(row) => row.id} /><Pagination offset={configs.data!.offset} limit={configs.data!.limit} total={configs.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'config_offset', nextOffset)} /></> : <EmptyState title="Конфигураций нет" />}</section><section className="panel"><header className="panel__header"><div><h2>История VPN-операций</h2><p>Асинхронные действия и ошибки</p></div></header>{operations.isLoading ? <PageLoading /> : operations.isError ? <ErrorState error={operations.error} /> : operations.data!.items.length ? <><OperationsTable rows={operations.data!.items} /><Pagination offset={operations.data!.offset} limit={operations.data!.limit} total={operations.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'operation_offset', nextOffset)} /></> : <EmptyState title="Операций ещё не было" />}</section></div>
}

function OperationsTable({ rows }: { rows: VpnOperation[] }) {
  const columns: Column<VpnOperation>[] = [
    { key: 'date', header: 'Создана', render: (row) => formatDateTime(row.created_at) },
    { key: 'type', header: 'Операция', render: (row) => <span className="mono-soft">{row.type || '—'}</span> },
    { key: 'config', header: 'Конфигурация', render: (row) => row.config_id ? `#${row.config_id}` : '—' },
    { key: 'status', header: 'Статус', render: (row) => <StatusBadge value={row.status} /> },
    { key: 'error', header: 'Ошибка', render: (row) => row.error || '—' },
  ]
  return <DataTable label="VPN-операции" columns={columns} rows={rows} rowKey={(row) => row.id} />
}

function UserReferrals({ userId }: { userId: string }) {
  const [params, setParams] = useSearchParams()
  const childrenOffset = pageOffset(params, 'referral_offset')
  const rewardsOffset = pageOffset(params, 'reward_offset')
  const ancestry = useQuery({ queryKey: ['user-ancestry', userId], queryFn: () => usersApi.ancestry(userId) })
  const children = useQuery({ queryKey: ['user-referral-children', userId, childrenOffset], queryFn: () => usersApi.children(userId, { limit: DETAIL_PAGE_SIZE, offset: childrenOffset }), placeholderData: (previous) => previous })
  const rewards = useQuery({ queryKey: ['user-referral-rewards', userId, rewardsOffset], queryFn: () => usersApi.rewards(userId, { limit: DETAIL_PAGE_SIZE, offset: rewardsOffset }), placeholderData: (previous) => previous })
  const rewardColumns: Column<ReferralReward>[] = [
    { key: 'date', header: 'Дата', render: (row) => formatDateTime(row.created_at) },
    { key: 'source', header: 'Источник', render: (row) => row.source_user_id ? <Link to={`/users/${row.source_user_id}`}>{row.source_username ? `@${row.source_username}` : `#${row.source_user_id}`}</Link> : '—' },
    { key: 'level', header: 'Уровень', align: 'center', render: (row) => `L${row.level ?? '—'}` },
    { key: 'deposit', header: 'Пополнение', align: 'end', render: (row) => formatMoney(row.deposit_amount) },
    { key: 'reward', header: 'Начислено', align: 'end', render: (row) => <strong className="money-positive">+{formatMoney(row.reward_amount)}</strong> },
  ]
  return <div className="stack"><section className="panel"><header className="panel__header"><div><h2>Реферальная цепочка</h2><p>Кто привёл пользователя</p></div></header>{ancestry.isLoading ? <PageLoading /> : ancestry.isError ? <ErrorState error={ancestry.error} /> : Array.isArray(ancestry.data) && ancestry.data!.length ? <div className="ancestry-chain">{ancestry.data.map((node, index) => <span key={node.user_id}><Link to={`/users/${node.user_id}`}>{node.username ? `@${node.username}` : `#${node.user_id}`}</Link><small>L{node.level ?? ancestry.data!.length - index}</small></span>)}</div> : <EmptyState title="Пользователь пришёл напрямую" />}</section><section className="panel"><header className="panel__header"><div><h2>Приглашённые пользователи</h2><p>Прямые рефералы</p></div></header>{children.isLoading ? <PageLoading /> : children.isError ? <ErrorState error={children.error} /> : children.data!.items.length ? <><ReferralList nodes={children.data!.items} /><Pagination offset={children.data!.offset} limit={children.data!.limit} total={children.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'referral_offset', nextOffset)} /></> : <EmptyState title="Приглашённых пользователей нет" />}</section><section className="panel"><header className="panel__header"><div><h2>История начислений</h2><p>Вознаграждения по уровням</p></div></header>{rewards.isLoading ? <PageLoading /> : rewards.isError ? <ErrorState error={rewards.error} /> : rewards.data!.items.length ? <><DataTable label="Реферальные начисления" columns={rewardColumns} rows={rewards.data!.items} rowKey={(row) => row.id} /><Pagination offset={rewards.data!.offset} limit={rewards.data!.limit} total={rewards.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'reward_offset', nextOffset)} /></> : <EmptyState title="Начислений пока нет" />}</section></div>
}

function ReferralList({ nodes }: { nodes: ReferralNode[] }) {
  return <div className="referral-list">{nodes.map((node) => <div className="referral-row" key={node.user_id}><span className="avatar">{(node.username || String(node.user_id)).slice(0, 1).toUpperCase()}</span><span><Link className="primary-link" to={`/users/${node.user_id}`}>{node.username ? `@${node.username}` : `Пользователь #${node.user_id}`}</Link><small>Регистрация {formatDateTime(node.registered_at)}</small></span><span><small>Пополнения</small><strong>{formatMoney(node.deposits_total)}</strong></span><span><small>Начисления</small><strong>{formatMoney(node.rewards_total)}</strong></span><span><small>Своя сеть</small><strong>{formatNumber(node.direct_referrals)}</strong></span></div>)}</div>
}

function UserAudit({ userId }: { userId: string }) {
  const [params, setParams] = useSearchParams()
  const offset = pageOffset(params, 'audit_offset')
  const query = useQuery({ queryKey: ['user-audit', userId, offset], queryFn: () => auditApi.list({ target_type: 'user', target_id: userId, limit: DETAIL_PAGE_SIZE, offset }), placeholderData: (previous) => previous })
  const columns: Column<AuditEvent>[] = [
    { key: 'time', header: 'Время', render: (row) => formatDateTime(row.occurred_at) },
    { key: 'actor', header: 'Инициатор', render: (row) => row.actor || 'system' },
    { key: 'action', header: 'Действие', render: (row) => <span className="mono-soft">{row.action || '—'}</span> },
    { key: 'reason', header: 'Причина', render: (row) => row.reason || '—' },
    { key: 'result', header: 'Результат', render: (row) => <StatusBadge value={row.result} /> },
    { key: 'meta', header: '', render: (row) => row.metadata && Object.keys(row.metadata).length ? <details className="details-popover"><summary><ExternalLink size={15} /> Детали</summary><pre>{JSON.stringify(row.metadata, null, 2)}</pre></details> : null },
  ]
  return <section className="panel"><header className="panel__header"><div><h2>История действий</h2><p>Неизменяемый журнал по пользователю</p></div></header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} /> : query.data!.items.length ? <><DataTable label="Аудит пользователя" columns={columns} rows={query.data!.items} rowKey={(row) => row.id} /><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => updatePage(params, setParams, 'audit_offset', nextOffset)} /></> : <EmptyState title="Событий пока нет" />}</section>
}
