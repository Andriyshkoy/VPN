import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, Search, ShieldAlert } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { configsApi, type VpnConfig } from '../api'
import { useAuth } from '../auth/AuthProvider'
import { DataTable, type Column } from '../components/DataTable'
import { Modal } from '../components/Modal'
import { PageHeader } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime } from '../lib/format'
import { withParam } from '../lib/search'

const PAGE_SIZE = 25

export function ConfigsPage() {
  const { user: admin } = useAuth()
  const canManage = admin?.permissions?.includes('configs:write') ?? false
  const canReadUsers = admin?.permissions.includes('users:read') ?? false
  const canReadServers = admin?.permissions.includes('servers:read') ?? false
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('q') ?? '')
  const [selected, setSelected] = useState<VpnConfig | null>(null)
  const offset = Number(params.get('offset') ?? 0)
  const filters = { q: params.get('q') ?? undefined, state: params.get('state') ?? undefined, server_id: params.get('server_id') ?? undefined, limit: PAGE_SIZE, offset }
  const query = useQuery({ queryKey: ['configs', filters], queryFn: () => configsApi.list(filters), placeholderData: (previous) => previous })
  const setFilter = (key: string, value: string) => setParams(withParam(params, key, value, true))
  const submit = (event: FormEvent) => { event.preventDefault(); setFilter('q', search.trim()) }
  const columns: Column<VpnConfig>[] = [
    { key: 'config', header: 'Конфигурация', render: (row) => <span><strong>{row.display_name || row.name || `Конфигурация #${row.id}`}</strong><small className="cell-note">ID {row.id}</small></span> },
    { key: 'owner', header: 'Владелец', render: (row) => row.owner_id ? canReadUsers ? <Link className="primary-link" to={`/users/${row.owner_id}`}>{row.owner_username ? `@${row.owner_username}` : `#${row.owner_id}`}</Link> : row.owner_username ? `@${row.owner_username}` : `#${row.owner_id}` : '—' },
    { key: 'server', header: 'Сервер', render: (row) => row.server_id ? canReadServers ? <Link to={`/servers/${row.server_id}`}>{row.server_name || `#${row.server_id}`}</Link> : row.server_name || `#${row.server_id}` : '—' },
    { key: 'desired', header: 'Desired', render: (row) => <StatusBadge value={row.desired_state ?? (row.suspended ? 'suspended' : 'active')} /> },
    { key: 'actual', header: 'Actual', render: (row) => <StatusBadge value={row.actual_state ?? row.last_operation_status} /> },
    { key: 'created', header: 'Создана', render: (row) => formatDateTime(row.created_at) },
    { key: 'error', header: 'Ошибка', render: (row) => row.last_error ? <span className="error-inline" title={row.last_error}><ShieldAlert size={15} /> {row.last_error}</span> : '—' },
    ...(canManage ? [{ key: 'actions', header: <span className="sr-only">Действия</span>, align: 'end' as const, render: (row: VpnConfig) => <button type="button" className="icon-button" aria-label={`Управление конфигурацией ${row.display_name || row.name || row.id}`} onClick={() => setSelected(row)}><MoreHorizontal /></button> }] : []),
  ]
  return <><PageHeader eyebrow="VPN" title="Конфигурации" description="Желаемое и фактическое состояние всех пользовательских профилей." /><section className="filter-bar"><form className="search-box" role="search" onSubmit={submit}><Search /><input aria-label="Поиск конфигураций" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Название, ID или пользователь" /><button className="button button--primary button--small" type="submit">Найти</button></form><label className="inline-select">Состояние <select value={params.get('state') ?? ''} onChange={(event) => setFilter('state', event.target.value)}><option value="">Все</option><option value="active">Активные</option><option value="suspended">Приостановленные</option><option value="failed">С ошибкой</option><option value="provisioning">Создаются</option><option value="revoked">Отозваны</option></select></label></section><section className="panel"><header className="panel__header"><div><h2>Все конфигурации</h2><p>{query.data ? `${query.data!.total} записей` : 'Загрузка…'}</p></div></header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><DataTable label="Конфигурации" columns={columns} rows={query.data!.items} rowKey={(row) => row.id} /><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setParams(withParam(params, 'offset', nextOffset ? String(nextOffset) : ''))} /></> : <EmptyState title="Конфигурации не найдены" />}</section>{selected && <ConfigActionDialog config={selected} onClose={() => setSelected(null)} />}</>
}

function ConfigActionDialog({ config, onClose }: { config: VpnConfig; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [action, setAction] = useState<'suspend' | 'unsuspend' | 'revoke'>(config.actual_state === 'suspended' ? 'unsuspend' : 'suspend')
  const [reason, setReason] = useState('')
  const mutation = useMutation({ mutationFn: () => configsApi.action(String(config.id), action, reason.trim()), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['configs'] }); onClose() } })
  const submit = (event: FormEvent) => { event.preventDefault(); if (reason.trim().length >= 3) mutation.mutate() }
  return <Modal open title={`Конфигурация ${config.display_name || config.name || `#${config.id}`}`} description="Действие выполнится асинхронно и попадёт в журнал аудита." onClose={onClose}><form className="form-stack" onSubmit={submit}><label className="field"><span>Действие</span><select value={action} onChange={(event) => setAction(event.target.value as typeof action)}>{config.actual_state === 'suspended' ? <option value="unsuspend">Возобновить</option> : <option value="suspend">Приостановить</option>}<option value="revoke">Отозвать навсегда</option></select></label><label className="field"><span>Причина</span><textarea minLength={3} maxLength={500} required rows={3} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Почему требуется это действие" /></label>{mutation.isError && <div className="form-error" role="alert">{mutation.error instanceof Error ? mutation.error.message : 'Не удалось запустить операцию'}</div>}<div className="form-actions"><button type="button" className="button button--secondary" onClick={onClose}>Отмена</button><button type="submit" className={action === 'revoke' ? 'button button--danger' : 'button button--primary'} disabled={reason.trim().length < 3 || mutation.isPending}>{mutation.isPending ? 'Запускаем…' : action === 'revoke' ? 'Отозвать' : action === 'suspend' ? 'Приостановить' : 'Возобновить'}</button></div></form></Modal>
}
