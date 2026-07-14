import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, CircleDollarSign, FileKey2, ServerCog, Users } from 'lucide-react'
import { Link } from 'react-router-dom'
import { dashboardApi } from '../api'
import { useAuth } from '../auth/AuthProvider'
import { DataTable, type Column } from '../components/DataTable'
import { PageHeader, StatCard } from '../components/PageHeader'
import { ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatMoney, formatNumber } from '../lib/format'
import type { DashboardSummary, VpnOperation } from '../api'

export function DashboardPage() {
  const { user } = useAuth()
  const permissions = user?.permissions ?? []
  const canReadUsers = permissions.includes('users:read')
  const canReadFinance = permissions.includes('finance:read')
  const canReadConfigs = permissions.includes('configs:read')
  const canReadServers = permissions.includes('servers:read')
  const canReadOperations = canReadConfigs || canReadServers
  const canReadMetrics = permissions.includes('metrics:read')
  const query = useQuery({ queryKey: ['dashboard'], queryFn: dashboardApi.summary, refetchInterval: 60_000 })
  if (query.isLoading) return <PageLoading />
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />
  const data = query.data!
  const serverHealth = data.servers_total ? Math.round((data.servers_healthy / data.servers_total) * 100) : 0
  const unknownServers = data.servers?.filter((server) => !server.health || server.health === 'unknown').length ?? 0
  type DashboardServer = NonNullable<DashboardSummary['servers']>[number]
  const serverColumns: Column<DashboardServer>[] = [
    { key: 'server', header: 'Сервер', render: (server) => <Link className="primary-link" to={`/servers/${server.id}`}>{server.name || `#${server.id}`}</Link> },
    { key: 'location', header: 'Локация', render: (server) => server.location || '—' },
    { key: 'configs', header: 'Конфигурации', align: 'end', render: (server) => `${formatNumber(server.config_active)} / ${formatNumber(server.config_total)}` },
  ]
  const operationColumns: Column<VpnOperation>[] = [
    { key: 'action', header: 'Операция', render: (operation) => <span className="mono-soft">{operation.type || '—'}</span> },
    { key: 'target', header: 'Объект', render: (operation) => operation.config_id ? `Конфиг #${operation.config_id}` : operation.server_id ? `Сервер #${operation.server_id}` : '—' },
    { key: 'result', header: 'Статус', render: (operation) => <StatusBadge value={operation.status} /> },
    { key: 'time', header: 'Обновлена', render: (operation) => formatDateTime(operation.updated_at) },
  ]
  return (
    <>
      <PageHeader eyebrow="Сегодня" title="Обзор системы" description="Главные показатели VPN-сервиса и состояние инфраструктуры." />
      <section className="stats-grid stats-grid--four">
        <StatCard label="Пользователи" value={formatNumber(data.users_total)} hint={`${formatNumber(data.users_active)} активных`} />
        <StatCard label="Активные конфигурации" value={formatNumber(data.configs_active)} hint={data.configs_suspended ? `${data.configs_suspended} приостановлено` : 'Все работают'} />
        {canReadFinance && <StatCard label="Пополнения за 30 дней" value={formatMoney(data.payments_today)} hint={`Выручка ${formatMoney(data.service_revenue_today)}`} tone="positive" />}
        {canReadServers && <StatCard label="Состояние серверов" value={`${data.servers_healthy}/${data.servers_total}`} hint={unknownServers ? `${unknownServers} без свежего статуса` : `${serverHealth}% исправны`} tone={serverHealth < 100 ? 'warning' : 'positive'} />}
      </section>
      {(data.alerts_active > 0 || data.pending_operations > 0) && <section className="attention-strip"><AlertTriangle /><div><strong>Требуется внимание</strong><span>{data.alerts_active} активных алертов · {data.pending_operations} операций в очереди</span></div>{canReadMetrics ? <Link to="/monitoring">Открыть мониторинг <ArrowRight size={16} /></Link> : canReadOperations ? <Link to="/operations">Открыть очередь <ArrowRight size={16} /></Link> : null}</section>}
      <section className="quick-grid">
        {canReadUsers && <Link className="quick-card" to="/users"><Users /><span><strong>Пользователи</strong><small>Поиск и User 360</small></span><ArrowRight /></Link>}
        {canReadFinance && <Link className="quick-card" to="/finance"><CircleDollarSign /><span><strong>Финансы</strong><small>Доходы и движение денег</small></span><ArrowRight /></Link>}
        {canReadConfigs && <Link className="quick-card" to="/configs"><FileKey2 /><span><strong>Конфигурации</strong><small>Состояния и ошибки</small></span><ArrowRight /></Link>}
        {canReadServers && <Link className="quick-card" to="/servers"><ServerCog /><span><strong>Серверы</strong><small>Capacity и управление</small></span><ArrowRight /></Link>}
      </section>
      <div className="content-grid content-grid--two">
        {canReadServers && <section className="panel"><header className="panel__header"><div><h2>VPN-серверы</h2><p>Распределение конфигураций</p></div><Link to="/servers">Все серверы</Link></header>{data.servers?.length ? <DataTable label="VPN-серверы" columns={serverColumns} rows={data.servers} rowKey={(server) => server.id} /> : <div className="panel__empty">Серверы пока не добавлены</div>}</section>}
        {canReadOperations && <section className="panel"><header className="panel__header"><div><h2>Операции, требующие внимания</h2><p>Pending, running и failed</p></div><Link to="/operations">Вся очередь</Link></header>{data.operations_attention?.length ? <DataTable label="VPN-операции" columns={operationColumns} rows={data.operations_attention} rowKey={(operation) => operation.id} /> : <div className="panel__empty">Проблемных операций нет</div>}</section>}
      </div>
    </>
  )
}
