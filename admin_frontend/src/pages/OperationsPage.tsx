import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { operationsApi, type VpnOperation } from '../api'
import { DataTable, type Column } from '../components/DataTable'
import { PageHeader } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, shortId } from '../lib/format'
import { withParam } from '../lib/search'

export function OperationsPage() {
  const [params, setParams] = useSearchParams()
  const offset = Number(params.get('offset') ?? 0)
  const filters = { status: params.get('status') ?? undefined, type: params.get('type') ?? undefined, server_id: params.get('server_id') ?? undefined, limit: 50, offset }
  const query = useQuery({ queryKey: ['operations', filters], queryFn: () => operationsApi.list(filters), refetchInterval: 15_000, placeholderData: (previous) => previous })
  const setFilter = (key: string, value: string) => setParams(withParam(params, key, value, true))
  const columns: Column<VpnOperation>[] = [
    { key: 'id', header: 'Операция', render: (row) => <span className="mono-soft" title={String(row.id)}>{shortId(row.id)}</span> },
    { key: 'created', header: 'Создана', render: (row) => formatDateTime(row.created_at) },
    { key: 'type', header: 'Тип', render: (row) => <span className="mono-soft">{row.type || '—'}</span> },
    { key: 'target', header: 'Объект', render: (row) => row.config_id ? `Конфигурация #${row.config_id}` : row.server_id ? `Сервер #${row.server_id}` : '—' },
    { key: 'status', header: 'Статус', render: (row) => <StatusBadge value={row.status} /> },
    { key: 'updated', header: 'Обновлена', render: (row) => formatDateTime(row.updated_at) },
    { key: 'error', header: 'Ошибка', render: (row) => row.error || '—' },
  ]
  return <><PageHeader eyebrow="Очередь" title="VPN-операции" description="Provisioning, suspend, revoke, inventory и действия с серверами." /><section className="filter-bar filter-bar--simple"><div className="filter-controls"><label>Статус <select value={params.get('status') ?? ''} onChange={(event) => setFilter('status', event.target.value)}><option value="">Все</option><option value="pending">В очереди</option><option value="running">Выполняются</option><option value="succeeded">Завершены</option><option value="failed">Ошибки</option></select></label><label>Тип <select value={params.get('type') ?? ''} onChange={(event) => setFilter('type', event.target.value)}><option value="">Все операции</option><option value="provision">Provision</option><option value="suspend">Suspend</option><option value="unsuspend">Unsuspend</option><option value="revoke">Revoke</option><option value="refresh_inventory">Inventory</option><option value="audit_drift">Drift audit</option></select></label></div></section><section className="panel"><header className="panel__header"><div><h2>Журнал операций</h2><p>Автообновление каждые 15 секунд</p></div></header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><DataTable label="VPN-операции" columns={columns} rows={query.data!.items} rowKey={(row) => row.id} /><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setParams(withParam(params, 'offset', nextOffset ? String(nextOffset) : ''))} /></> : <EmptyState title="Операций нет" description="Очередь и история операций пусты." />}</section></>
}
