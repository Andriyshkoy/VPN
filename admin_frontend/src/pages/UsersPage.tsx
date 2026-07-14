import { useQuery } from '@tanstack/react-query'
import { CircleDollarSign, Search, SlidersHorizontal } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { usersApi, type User } from '../api'
import { useAuth } from '../auth/AuthProvider'
import { BalanceAdjustmentDialog } from '../components/BalanceAdjustmentDialog'
import { DataTable, type Column } from '../components/DataTable'
import { PageHeader } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatMoney, formatNumber, userLabel } from '../lib/format'
import { withParam } from '../lib/search'

const PAGE_SIZE = 25

export function UsersPage() {
  const { user: admin } = useAuth()
  const canReadBalance = admin?.permissions.includes('balance:read') ?? false
  const canReadReferrals = admin?.permissions.includes('referrals:read') ?? false
  const canAdjustBalance = admin?.permissions?.includes('balance:write') ?? false
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('q') ?? '')
  const [selected, setSelected] = useState<User | null>(null)
  const offset = Number(params.get('offset') ?? 0)
  const requestedSort = params.get('sort') ?? '-created_at'
  const filters = {
    q: params.get('q') ?? undefined,
    status: params.get('status') ?? undefined,
    config_status: params.get('config_status') ?? undefined,
    sort: canReadBalance || !/^-?balance$/.test(requestedSort) ? requestedSort : '-created_at',
    limit: PAGE_SIZE,
    offset,
  }
  const query = useQuery({ queryKey: ['users', filters], queryFn: () => usersApi.list(filters), placeholderData: (previous) => previous })
  const setFilter = (key: string, value: string) => {
    setParams(withParam(params, key, value, true))
  }
  const submitSearch = (event: FormEvent) => { event.preventDefault(); setFilter('q', search.trim()) }
  const columns: Column<User>[] = [
    { key: 'user', header: 'Пользователь', render: (user) => <div className="identity-cell"><span className="avatar">{userLabel(user).replace('@', '').slice(0, 1).toUpperCase()}</span><span><Link className="primary-link" to={`/users/${user.id}`}>{userLabel(user)}</Link><small>ID {user.id}{user.tg_id ? ` · TG ${user.tg_id}` : ''}</small></span></div> },
    { key: 'status', header: 'Статус', render: (user) => <StatusBadge value={user.status ?? 'active'} /> },
    ...(canReadBalance ? [{ key: 'balance', header: 'Баланс', align: 'end' as const, render: (user: User) => <strong>{formatMoney(user.balance)}</strong> }] : []),
    { key: 'configs', header: 'Конфигурации', align: 'center', render: (user) => <span>{formatNumber(user.active_configs_count ?? user.configs_count ?? 0)}<small className="cell-note"> активных</small></span> },
    ...(canReadReferrals ? [{ key: 'inviter', header: 'Пригласил', render: (user: User) => user.inviter_id ? <Link to={`/users/${user.inviter_id}`}>{user.inviter_username ? `@${user.inviter_username}` : `#${user.inviter_id}`}</Link> : '—' }] : []),
    { key: 'created', header: 'Регистрация', render: (user) => formatDateTime(user.created_at) },
    { key: 'actions', header: <span className="sr-only">Действия</span>, align: 'end', render: (user) => canAdjustBalance && user.balance !== undefined ? <button className="button button--ghost button--small" type="button" onClick={() => setSelected(user)}><CircleDollarSign size={16} /> Баланс</button> : null },
  ]
  return (
    <>
      <PageHeader eyebrow="Клиенты" title="Пользователи" description="Поиск, финансовые показатели и полная история взаимодействия." />
      <section className="filter-bar">
        <form className="search-box" role="search" onSubmit={submitSearch}><Search aria-hidden="true" /><input aria-label="Поиск пользователей" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Username, Telegram ID, ID или конфигурация" /><button type="submit" className="button button--primary button--small">Найти</button></form>
        <div className="filter-controls"><SlidersHorizontal size={17} aria-hidden="true" /><label><span className="sr-only">Статус пользователя</span><select value={params.get('status') ?? ''} onChange={(event) => setFilter('status', event.target.value)}><option value="">Все пользователи</option><option value="active">Активные</option><option value="blocked">Заблокировали бота</option><option value="deactivated">Деактивированы</option><option value="permanent_failure">Недоступны для доставки</option></select></label><label><span className="sr-only">Статус конфигураций</span><select value={params.get('config_status') ?? ''} onChange={(event) => setFilter('config_status', event.target.value)}><option value="">Любые конфигурации</option><option value="active">Есть активные</option><option value="none">Нет конфигураций</option><option value="suspended">Есть приостановленные</option><option value="failed">Есть с ошибкой</option><option value="pending">Ожидают синхронизации</option></select></label></div>
      </section>
      <section className="panel">
        <header className="panel__header"><div><h2>Все пользователи</h2><p>{query.data ? `${formatNumber(query.data!.total)} записей` : 'Загрузка…'}</p></div><label className="sort-control">Сортировка <select value={filters.sort} onChange={(event) => setFilter('sort', event.target.value)}><option value="-created_at">Сначала новые</option><option value="created_at">Сначала старые</option>{canReadBalance && <><option value="-balance">Баланс: больше</option><option value="balance">Баланс: меньше</option></>}</select></label></header>
        {query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length === 0 ? <EmptyState title="Пользователи не найдены" description="Измените поисковый запрос или фильтры." /> : <><DataTable label="Пользователи" columns={columns} rows={query.data!.items} rowKey={(user) => user.id} /><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setParams(withParam(params, 'offset', nextOffset ? String(nextOffset) : ''))} /></>}
      </section>
      {selected && <BalanceAdjustmentDialog user={selected} open onClose={() => setSelected(null)} />}
    </>
  )
}
