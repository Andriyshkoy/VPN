import { useQuery } from '@tanstack/react-query'
import { Search } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  financeApi,
  type BillingRun,
  type LedgerEntry,
  type Payment,
  type ReferralReward,
} from '../api'
import { DataTable, type Column } from '../components/DataTable'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatMoney, formatNumber, shortId } from '../lib/format'

const PAGE_SIZE = 25
type View = 'ledger' | 'payments' | 'billing' | 'rewards'

function safeOffset(params: URLSearchParams, key: string) {
  const value = Number(params.get(key) ?? 0)
  return Number.isInteger(value) && value >= 0 ? value : 0
}

function userLink(id?: string | number, username?: string | null, tgId?: string | number) {
  if (id === undefined || id === null) return '—'
  const label = username ? `@${username}` : tgId ? `TG ${tgId}` : `#${id}`
  return <Link to={`/users/${id}`}>{label}</Link>
}

export function FinanceActivity({ from, to }: { from: string; to: string }) {
  const [params, setParams] = useSearchParams()
  const requestedView = params.get('finance_view')
  const view: View = ['ledger', 'payments', 'billing', 'rewards'].includes(requestedView ?? '')
    ? requestedView as View
    : 'ledger'
  const [search, setSearch] = useState(params.get('finance_q') ?? '')

  const selectView = (nextView: View) => {
    const next = new URLSearchParams(params)
    if (nextView === 'ledger') next.delete('finance_view'); else next.set('finance_view', nextView)
    setParams(next)
  }
  const update = (values: Record<string, string | undefined>) => {
    const next = new URLSearchParams(params)
    Object.entries(values).forEach(([key, value]) => { if (value) next.set(key, value); else next.delete(key) })
    setParams(next)
  }
  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    update({ finance_q: search.trim() || undefined, ledger_offset: undefined, payment_offset: undefined, ledger_snapshot: undefined })
  }

  return <section className="finance-activity">
    <header className="panel__header finance-activity__header"><div><h2>Финансовые операции</h2><p>Общий неизменяемый ledger, платежи и расчёты за выбранный период</p></div></header>
    <nav className="tabs" aria-label="Финансовые журналы">
      <button type="button" className={view === 'ledger' ? 'tab tab--active' : 'tab'} onClick={() => selectView('ledger')}>Движение балансов</button>
      <button type="button" className={view === 'payments' ? 'tab tab--active' : 'tab'} onClick={() => selectView('payments')}>Платежи</button>
      <button type="button" className={view === 'billing' ? 'tab tab--active' : 'tab'} onClick={() => selectView('billing')}>Биллинг</button>
      <button type="button" className={view === 'rewards' ? 'tab tab--active' : 'tab'} onClick={() => selectView('rewards')}>Реферальные начисления</button>
    </nav>
    {(view === 'ledger' || view === 'payments') && <form className="search-box finance-activity__search" role="search" onSubmit={submitSearch}><Search /><input aria-label="Поиск финансовых операций" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Username, Telegram ID или ID пользователя" /><button type="submit" className="button button--primary button--small">Найти</button></form>}
    {view === 'ledger' && <LedgerJournal from={from} to={to} params={params} update={update} />}
    {view === 'payments' && <PaymentsJournal from={from} to={to} params={params} update={update} />}
    {view === 'billing' && <BillingJournal from={from} to={to} params={params} update={update} />}
    {view === 'rewards' && <RewardsJournal from={from} to={to} params={params} update={update} />}
  </section>
}

function LedgerJournal({ from, to, params, update }: JournalProps) {
  const offset = safeOffset(params, 'ledger_offset')
  const direction = params.get('ledger_direction') ?? ''
  const snapshot = offset ? params.get('ledger_snapshot') ?? undefined : undefined
  const query = useQuery({
    queryKey: ['finance-ledger', from, to, params.get('finance_q'), direction, offset, snapshot],
    queryFn: () => financeApi.ledger({ from, to, q: params.get('finance_q') ?? undefined, direction: direction || undefined, snapshot_id: snapshot, limit: PAGE_SIZE, offset }),
    placeholderData: (previous) => previous,
  })
  const columns: Column<LedgerEntry>[] = [
    { key: 'date', header: 'Дата', render: (row) => formatDateTime(row.created_at) },
    { key: 'user', header: 'Пользователь', render: (row) => userLink(row.user_id, row.user_username, row.user_tg_id) },
    { key: 'kind', header: 'Операция', render: (row) => <span><strong className="mono-soft">{row.kind || 'операция'}</strong><small className="cell-note">{row.description || row.reason || '—'}</small></span> },
    { key: 'amount', header: 'Сумма', align: 'end', render: (row) => <strong className={Number(row.amount) >= 0 ? 'money-positive' : 'money-negative'}>{Number(row.amount) > 0 ? '+' : ''}{formatMoney(row.amount)}</strong> },
    { key: 'balance', header: 'Баланс после', align: 'end', render: (row) => formatMoney(row.balance_after) },
  ]
  return <JournalPanel title="Движение балансов" controls={<label className="inline-select">Операции <select value={direction} onChange={(event) => update({ ledger_direction: event.target.value || undefined, ledger_offset: undefined, ledger_snapshot: undefined })}><option value="">Все</option><option value="credit">Пополнения</option><option value="debit">Списания</option></select></label>} query={query} empty="Движений за период нет" columns={columns} offset={offset} onPage={(nextOffset) => update({ ledger_offset: nextOffset ? String(nextOffset) : undefined, ledger_snapshot: nextOffset ? String(query.data?.snapshot_id ?? snapshot ?? '') : undefined })} />
}

function PaymentsJournal({ from, to, params, update }: JournalProps) {
  const offset = safeOffset(params, 'payment_offset')
  const status = params.get('payment_status') ?? ''
  const query = useQuery({
    queryKey: ['finance-payments', from, to, params.get('finance_q'), status, offset],
    queryFn: () => financeApi.payments({ from, to, q: params.get('finance_q') ?? undefined, status: status || undefined, limit: PAGE_SIZE, offset }),
    placeholderData: (previous) => previous,
  })
  const columns: Column<Payment>[] = [
    { key: 'date', header: 'Дата', render: (row) => formatDateTime(row.created_at) },
    { key: 'user', header: 'Пользователь', render: (row) => userLink(row.user_id, row.user_username, row.user_tg_id) },
    { key: 'payment', header: 'Платёж', render: (row) => <span className="mono-soft" title={row.provider_payment_id}>{row.provider || 'provider'} · {shortId(row.provider_payment_id || row.id)}</span> },
    { key: 'status', header: 'Статус', render: (row) => <StatusBadge value={row.status} /> },
    { key: 'amount', header: 'Сумма', align: 'end', render: (row) => <strong>{formatMoney(row.amount)}</strong> },
  ]
  return <JournalPanel title="Платежи провайдера" controls={<label className="inline-select">Статус <select value={status} onChange={(event) => update({ payment_status: event.target.value || undefined, payment_offset: undefined })}><option value="">Все</option><option value="pending">Ожидают</option><option value="credited">Зачислены</option><option value="expired">Истекли</option><option value="cancelled">Отменены</option><option value="failed">Ошибки</option></select></label>} query={query} empty="Платежей за период нет" columns={columns} offset={offset} onPage={(nextOffset) => update({ payment_offset: nextOffset ? String(nextOffset) : undefined })} />
}

function BillingJournal({ from, to, params, update }: JournalProps) {
  const offset = safeOffset(params, 'billing_offset')
  const status = params.get('billing_status') ?? ''
  const query = useQuery({ queryKey: ['finance-billing-runs', from, to, status, offset], queryFn: () => financeApi.billingRuns({ from, to, status: status || undefined, limit: PAGE_SIZE, offset }), placeholderData: (previous) => previous })
  const columns: Column<BillingRun>[] = [
    { key: 'period', header: 'Период', render: (row) => <span><strong>{formatDateTime(row.period_start)}</strong><small className="cell-note">до {formatDateTime(row.period_end)}</small></span> },
    { key: 'status', header: 'Статус', render: (row) => <StatusBadge value={row.status} /> },
    { key: 'users', header: 'Пользователи', align: 'end', render: (row) => formatNumber(row.charged_users) },
    { key: 'rate', header: 'Тариф за конфиг', align: 'end', render: (row) => formatMoney(row.cost_per_config) },
    { key: 'total', header: 'Списано', align: 'end', render: (row) => <strong>{formatMoney(row.total_amount)}</strong> },
  ]
  return <JournalPanel title="Биллинговые прогоны" controls={<label className="inline-select">Статус <select value={status} onChange={(event) => update({ billing_status: event.target.value || undefined, billing_offset: undefined })}><option value="">Все</option><option value="running">Выполняются</option><option value="completed">Завершены</option><option value="failed">Ошибки</option></select></label>} query={query} empty="Биллинговых прогонов за период нет" columns={columns} offset={offset} onPage={(nextOffset) => update({ billing_offset: nextOffset ? String(nextOffset) : undefined })} />
}

function RewardsJournal({ from, to, params, update }: JournalProps) {
  const offset = safeOffset(params, 'reward_offset')
  const level = params.get('reward_level') ?? ''
  const query = useQuery({ queryKey: ['finance-referral-rewards', from, to, level, offset], queryFn: () => financeApi.rewards({ from, to, level: level || undefined, limit: PAGE_SIZE, offset }), placeholderData: (previous) => previous })
  const columns: Column<ReferralReward>[] = [
    { key: 'date', header: 'Дата', render: (row) => formatDateTime(row.created_at) },
    { key: 'beneficiary', header: 'Получатель', render: (row) => userLink(row.beneficiary_id, row.beneficiary_username) },
    { key: 'source', header: 'Реферал', render: (row) => userLink(row.source_user_id, row.source_username) },
    { key: 'level', header: 'Уровень', align: 'center', render: (row) => `L${row.level ?? '—'}` },
    { key: 'deposit', header: 'Пополнение', align: 'end', render: (row) => formatMoney(row.deposit_amount) },
    { key: 'reward', header: 'Начислено', align: 'end', render: (row) => <strong className="money-positive">+{formatMoney(row.reward_amount)}</strong> },
  ]
  return <JournalPanel title="Реферальные начисления" controls={<label className="inline-select">Уровень <select value={level} onChange={(event) => update({ reward_level: event.target.value || undefined, reward_offset: undefined })}><option value="">Все</option><option value="1">L1</option><option value="2">L2</option></select></label>} query={query} empty="Реферальных начислений за период нет" columns={columns} offset={offset} onPage={(nextOffset) => update({ reward_offset: nextOffset ? String(nextOffset) : undefined })} />
}

interface JournalProps {
  from: string
  to: string
  params: URLSearchParams
  update: (values: Record<string, string | undefined>) => void
}

function JournalPanel<T>({ title, controls, query, empty, columns, offset, onPage }: { title: string; controls: React.ReactNode; query: ReturnType<typeof useQuery<{ items: T[]; total: number; limit: number; offset: number; snapshot_id?: number }>>; empty: string; columns: Column<T>[]; offset: number; onPage: (offset: number) => void }) {
  return <section className="panel"><header className="panel__header"><div><h2>{title}</h2><p>{query.data ? `${formatNumber(query.data.total)} записей` : 'Загрузка…'}</p></div>{controls}</header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><DataTable label={title} columns={columns} rows={query.data!.items} rowKey={(row) => (row as { id: string | number }).id} /><Pagination offset={offset} limit={query.data!.limit} total={query.data!.total} onChange={onPage} /></> : <EmptyState title={empty} />}</section>
}
