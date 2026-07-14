import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Search } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { auditApi, type AuditEvent } from '../api'
import { DataTable, type Column } from '../components/DataTable'
import { PageHeader } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, shortId } from '../lib/format'
import { withParam } from '../lib/search'

export function AuditPage() {
  const [params, setParams] = useSearchParams()
  const [queryText, setQueryText] = useState(params.get('q') ?? '')
  const offset = Number(params.get('offset') ?? 0)
  const filters = { q: params.get('q') ?? undefined, action: params.get('action') ?? undefined, result: params.get('result') ?? undefined, target_type: params.get('target_type') ?? undefined, from: params.get('from') ?? undefined, to: params.get('to') ?? undefined, limit: 50, offset }
  const query = useQuery({ queryKey: ['audit', filters], queryFn: () => auditApi.list(filters), placeholderData: (previous) => previous })
  const setFilter = (key: string, value: string) => setParams(withParam(params, key, value, true))
  const submit = (event: FormEvent) => { event.preventDefault(); setFilter('q', queryText.trim()) }
  const columns: Column<AuditEvent>[] = [
    { key: 'time', header: 'Время', render: (row) => formatDateTime(row.occurred_at) },
    { key: 'actor', header: 'Инициатор', render: (row) => <span><strong>{row.actor || 'system'}</strong>{row.ip_address && <small className="cell-note">{row.ip_address}</small>}</span> },
    { key: 'action', header: 'Действие', render: (row) => <span className="mono-soft">{row.action || '—'}</span> },
    { key: 'target', header: 'Объект', render: (row) => row.target_type ? `${row.target_type} · ${shortId(row.target_id)}` : '—' },
    { key: 'reason', header: 'Причина', render: (row) => row.reason || '—' },
    { key: 'result', header: 'Результат', render: (row) => <StatusBadge value={row.result} /> },
    { key: 'details', header: '', render: (row) => row.metadata && Object.keys(row.metadata).length ? <details className="details-popover"><summary><ExternalLink size={15} /> Детали</summary><pre>{JSON.stringify(row.metadata, null, 2)}</pre></details> : null },
  ]
  return <><PageHeader eyebrow="Безопасность" title="Журнал аудита" description="Неизменяемая история административных и системных действий." /><section className="filter-bar"><form className="search-box" role="search" onSubmit={submit}><Search /><input aria-label="Поиск в журнале" value={queryText} onChange={(event) => setQueryText(event.target.value)} placeholder="Инициатор, действие, объект или причина" /><button className="button button--primary button--small" type="submit">Найти</button></form><div className="filter-controls"><label><span className="sr-only">Результат</span><select value={params.get('result') ?? ''} onChange={(event) => setFilter('result', event.target.value)}><option value="">Любой результат</option><option value="success">Успешно</option><option value="failed">Ошибка</option><option value="denied">Отклонено</option></select></label><label><span className="sr-only">Тип объекта</span><select value={params.get('target_type') ?? ''} onChange={(event) => setFilter('target_type', event.target.value)}><option value="">Любой объект</option><option value="user">Пользователь</option><option value="server">Сервер</option><option value="config">Конфигурация</option><option value="payment">Платёж</option></select></label></div></section><section className="panel"><header className="panel__header"><div><h2>События</h2><p>{query.data ? `${query.data!.total} записей` : 'Загрузка…'}</p></div></header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><DataTable label="Журнал аудита" columns={columns} rows={query.data!.items} rowKey={(row) => row.id} /><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setParams(withParam(params, 'offset', nextOffset ? String(nextOffset) : ''))} /></> : <EmptyState title="События не найдены" />}</section></>
}
