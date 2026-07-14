import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, MapPin, Plus, Search, ServerCog } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { serversApi, type ServerCreateInput } from '../api'
import { useAuth } from '../auth/AuthProvider'
import { Modal } from '../components/Modal'
import { PageHeader } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatMoney, formatNumber } from '../lib/format'
import { withParam } from '../lib/search'

export function ServersPage() {
  const { user: admin } = useAuth()
  const canManage = admin?.permissions.includes('servers:write') ?? false
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('q') ?? '')
  const [createOpen, setCreateOpen] = useState(false)
  const offset = Number(params.get('offset') ?? 0)
  const filters = { q: params.get('q') ?? undefined, status: params.get('status') ?? undefined, location: params.get('location') ?? undefined, limit: 24, offset }
  const query = useQuery({ queryKey: ['servers', filters], queryFn: () => serversApi.list(filters), refetchInterval: 60_000, placeholderData: (previous) => previous })
  const setFilter = (key: string, value: string) => setParams(withParam(params, key, value, true))
  const submit = (event: FormEvent) => { event.preventDefault(); setFilter('q', search.trim()) }
  return <>
    <PageHeader eyebrow="Инфраструктура" title="VPN-серверы" description="Capacity, health, inventory и безопасные операции управления." actions={canManage && <button className="button button--primary" type="button" onClick={() => setCreateOpen(true)}><Plus size={17} /> Добавить сервер</button>} />
    <section className="filter-bar"><form className="search-box" role="search" onSubmit={submit}><Search /><input aria-label="Поиск серверов" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Название, регион, IP или endpoint" /><button className="button button--primary button--small" type="submit">Найти</button></form><label className="inline-select">Состояние <select value={params.get('status') ?? ''} onChange={(event) => setFilter('status', event.target.value)}><option value="">Все серверы</option><option value="active">Активные</option><option value="draining">Drain</option><option value="disabled">Отключённые</option><option value="unhealthy">С ошибкой</option></select></label></section>
    {query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><section className="server-grid">{query.data!.items.map((server) => {
      const active = server.active_configs ?? server.configs_count ?? 0
      const capacity = server.capacity ?? 0
      const load = capacity ? Math.min(100, Math.round((active / capacity) * 100)) : 0
      return <article className="server-card" key={server.id}><header><span className="server-icon"><ServerCog /></span><div><h2>{server.name || `Сервер #${server.id}`}</h2><p><MapPin size={14} />{server.location || server.region || 'Локация не указана'}</p></div><StatusBadge value={server.health ?? server.status ?? server.lifecycle_state} /></header><dl><div><dt>Endpoint</dt><dd className="mono-soft">{server.vpn_endpoint || server.host || server.ip || '—'}</dd></div><div><dt>Конфигурации</dt><dd>{formatNumber(active)}{capacity ? ` / ${formatNumber(capacity)}` : ''}</dd></div><div><dt>Новые конфиги</dt><dd><StatusBadge value={server.accepts_new_configs ?? false} label={server.accepts_new_configs ? 'Принимает' : 'Закрыт'} /></dd></div>{server.monthly_cost !== undefined && <div><dt>Стоимость</dt><dd>{formatMoney(server.monthly_cost)} / мес.</dd></div>}</dl>{capacity > 0 && <div className="capacity"><span><small>Загрузка</small><strong>{load}%</strong></span><div className="progress"><i style={{ width: `${load}%` }} /></div></div>}<footer><span>Проверка {formatDateTime(server.last_seen_at)}</span><Link to={`/servers/${server.id}`}>Управление <ArrowRight size={15} /></Link></footer></article>
    })}</section><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setParams(withParam(params, 'offset', nextOffset ? String(nextOffset) : ''))} /></> : <EmptyState title="Серверы не найдены" />}
    {createOpen && <CreateServerDialog onClose={() => setCreateOpen(false)} />}
  </>
}

function CreateServerDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const [validation, setValidation] = useState('')
  const mutation = useMutation({ mutationFn: serversApi.create, onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['servers'] }); onClose() } })
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setValidation('')
    const form = new FormData(event.currentTarget)
    const maximum = String(form.get('max_configs') ?? '').trim()
    const reserve = Number(form.get('capacity_reserve') || 0)
    if (maximum && reserve >= Number(maximum)) return setValidation('Резерв должен быть меньше общего capacity.')
    const input: ServerCreateInput = {
      name: String(form.get('name')).trim(),
      ip: String(form.get('ip')).trim(),
      port: Number(form.get('port')),
      host: String(form.get('host')).trim(),
      location: String(form.get('location')).trim(),
      api_key: String(form.get('api_key')).trim(),
      monthly_cost: String(form.get('monthly_cost') || '0.00').replace(',', '.'),
      lifecycle_state: 'disabled',
      accepts_new_configs: false,
      max_configs: maximum ? Number(maximum) : undefined,
      capacity_reserve: reserve,
      placement_weight: String(form.get('placement_weight') || '1'),
      provider: String(form.get('provider') ?? '').trim() || undefined,
      public_endpoint: String(form.get('public_endpoint') ?? '').trim() || undefined,
    }
    mutation.mutate(input)
  }
  return <Modal open title="Добавить VPN-сервер" description="Сервер создаётся отключённым: после добавления проверьте Manager и identity, затем активируйте узел. API key отправляется один раз и не возвращается в интерфейс." onClose={onClose}><form className="form-stack" onSubmit={submit}><div className="form-grid"><label className="field"><span>Название</span><input name="name" required maxLength={128} placeholder="ams-2" /></label><label className="field"><span>Локация</span><input name="location" required maxLength={128} placeholder="Amsterdam" /></label><label className="field"><span>Manager IP</span><input name="ip" required maxLength={64} placeholder="10.8.0.3" /></label><label className="field"><span>Manager port</span><input name="port" type="number" min="1" max="65535" defaultValue="16290" required /></label><label className="field"><span>Manager host</span><input name="host" required maxLength={128} placeholder="vpn-node.internal" /></label><label className="field"><span>Публичный VPN endpoint</span><input name="public_endpoint" maxLength={255} placeholder="vpn.example.com" /></label><label className="field"><span>Провайдер</span><input name="provider" maxLength={128} placeholder="Hetzner" /></label><label className="field"><span>Стоимость в месяц, ₽</span><input name="monthly_cost" inputMode="decimal" defaultValue="0.00" pattern="\d+(?:[.,]\d{1,2})?" /></label><label className="field"><span>Capacity</span><input name="max_configs" type="number" min="1" placeholder="500" /></label><label className="field"><span>Резерв capacity</span><input name="capacity_reserve" type="number" min="0" defaultValue="0" /></label><label className="field"><span>Placement weight</span><input name="placement_weight" inputMode="decimal" defaultValue="1" pattern="\d+(?:\.\d{1,3})?" /></label></div><label className="field"><span>Manager API key</span><input name="api_key" type="password" autoComplete="new-password" required maxLength={4096} /></label>{(validation || mutation.isError) && <div className="form-error" role="alert">{validation || (mutation.error instanceof Error ? mutation.error.message : 'Не удалось добавить сервер')}</div>}<div className="form-actions"><button type="button" className="button button--secondary" onClick={onClose}>Отмена</button><button type="submit" className="button button--primary" disabled={mutation.isPending}>{mutation.isPending ? 'Добавляем…' : 'Добавить отключённым'}</button></div></form></Modal>
}
