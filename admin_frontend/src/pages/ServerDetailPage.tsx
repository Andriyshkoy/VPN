import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Archive,
  ArrowLeft,
  Ban,
  CheckCircle2,
  DatabaseZap,
  Edit3,
  GitCompareArrows,
  Power,
  RefreshCw,
  Save,
} from 'lucide-react'
import { useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { operationsApi, serversApi, type Server, type VpnOperation } from '../api'
import { useAuth } from '../auth/AuthProvider'
import { Modal } from '../components/Modal'
import { PageHeader, StatCard } from '../components/PageHeader'
import { ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatBytes, formatDateTime, formatMoney, formatNumber, percent } from '../lib/format'

interface PendingAction {
  action: string
  title: string
  description: string
  confirm: string
  body?: Record<string, unknown>
  danger?: boolean
}

interface ActionIntent {
  input: PendingAction
  reason: string
  key: string
}

const TERMINAL_SUCCESS = new Set(['succeeded', 'completed', 'success'])
const TERMINAL_FAILURE = new Set(['failed', 'exhausted', 'cancelled'])

export function ServerDetailPage() {
  const { user: admin } = useAuth()
  const canManage = admin?.permissions.includes('servers:write') ?? false
  const { serverId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [actionReason, setActionReason] = useState('')
  const [capacity, setCapacity] = useState('')
  const [editOpen, setEditOpen] = useState(false)
  const actionIntent = useRef<{ fingerprint: string; key: string } | null>(null)

  const server = useQuery({
    queryKey: ['server', serverId],
    queryFn: () => serversApi.detail(serverId),
    enabled: Boolean(serverId),
  })
  const status = useQuery({
    queryKey: ['server-status', serverId],
    queryFn: () => serversApi.status(serverId),
    enabled: Boolean(serverId),
    refetchInterval: 30_000,
  })
  const action = useMutation({
    mutationFn: async ({ input, reason, key }: ActionIntent) => {
      const versioned = ['set_accepting', 'drain', 'update_capacity', 'disable', 'activate', 'retire'].includes(input.action)
      const operation = await serversApi.action(serverId, input.action, {
        reason,
        ...(versioned && server.data?.version ? { expected_version: server.data.version } : {}),
        ...input.body,
      }, key)
      const result = await waitForOperation(operation, serverId)
      if (TERMINAL_FAILURE.has(result.status ?? '')) throw new Error(result.error || 'Операция завершилась с ошибкой')
      return result
    },
    onSuccess: async () => {
      actionIntent.current = null
      setPending(null)
      setConfirmation('')
      setActionReason('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['server', serverId] }),
        queryClient.invalidateQueries({ queryKey: ['server-status', serverId] }),
        queryClient.invalidateQueries({ queryKey: ['servers'] }),
        queryClient.invalidateQueries({ queryKey: ['operations'] }),
      ])
    },
  })

  if (server.isLoading) return <PageLoading />
  if (server.isError) return <ErrorState error={server.error} retry={() => void server.refetch()} />

  const node = server.data!
  const nodeStatus = status.data
  const lifecycle = node.lifecycle_state ?? node.status ?? 'unknown'
  const configCount = node.configs_count ?? node.active_configs ?? 0
  const snapshotAge = ageLabel(nodeStatus?.last_checked_at)
  const capacityUsage = nodeStatus?.capacity
    ? ((nodeStatus.active_configs ?? 0) / nodeStatus.capacity) * 100
    : undefined

  const openAction = (value: PendingAction) => {
    actionIntent.current = null
    setConfirmation('')
    setActionReason(value.description)
    setPending(value)
  }
  const submitAction = () => {
    if (!pending || actionReason.trim().length < 3) return
    const fingerprint = JSON.stringify({
      serverId,
      action: pending.action,
      body: pending.body,
      reason: actionReason.trim(),
      version: node.version,
    })
    if (!actionIntent.current || actionIntent.current.fingerprint !== fingerprint) {
      actionIntent.current = { fingerprint, key: crypto.randomUUID() }
    }
    action.mutate({ input: pending, reason: actionReason.trim(), key: actionIntent.current.key })
  }
  const updateCapacity = (event: FormEvent) => {
    event.preventDefault()
    const value = Number(capacity)
    if (!Number.isInteger(value) || value < 1) return
    openAction({
      action: 'update_capacity',
      title: 'Изменить capacity',
      description: `Установить лимит ${value} конфигураций.`,
      confirm: 'ИЗМЕНИТЬ',
      body: { capacity: value },
    })
  }

  return (
    <>
      <button type="button" className="back-link" onClick={() => navigate('/servers')}><ArrowLeft size={16} /> К серверам</button>
      <PageHeader
        title={node.name || `Сервер #${node.id}`}
        description={`${node.location || node.region || 'Локация не указана'} · ${node.vpn_endpoint || node.host || node.ip || 'endpoint не указан'}`}
        actions={<><StatusBadge value={nodeStatus?.status ?? node.health ?? node.status} />{canManage && <button type="button" className="button button--secondary" onClick={() => setEditOpen(true)}><Edit3 size={16} /> Редактировать</button>}</>}
      />
      <section className="stats-grid stats-grid--four">
        <StatCard label="Конфигурации" value={formatNumber(nodeStatus?.active_configs ?? node.active_configs ?? node.configs_count)} hint={node.capacity ? `из ${formatNumber(node.capacity)}` : 'Лимит не задан'} />
        <StatCard label="Онлайн-сессии" value={formatNumber(nodeStatus?.online_sessions)} hint="В последнем snapshot" />
        <StatCard label="Manager" value={<StatusBadge value={nodeStatus?.manager_ready === undefined ? 'unknown' : nodeStatus.manager_ready} label={nodeStatus?.manager_ready === undefined ? 'Нет данных' : nodeStatus.manager_ready ? 'Готов' : 'Не готов'} />} hint={snapshotAge} />
        {node.monthly_cost !== undefined && <StatCard label="Стоимость" value={formatMoney(node.monthly_cost)} hint="В месяц" />}
      </section>
      {nodeStatus?.instance_mismatch && <div className="form-error" role="alert">Manager instance не совпадает с зарегистрированным узлом. Не активируйте сервер до проверки identity.</div>}

      <div className={canManage ? 'content-grid content-grid--server' : 'stack'}>
        <div className="stack">
          <section className="panel">
            <header className="panel__header">
              <div><h2>Последний снимок Manager</h2><p>{nodeStatus?.last_checked_at ? `${formatDateTime(nodeStatus.last_checked_at)} · ${snapshotAge}` : 'Успешных проверок ещё не было'}</p></div>
              <div className="panel__actions"><StatusBadge value={nodeStatus?.status} /><button type="button" className="button button--secondary button--small" disabled={status.isFetching} onClick={() => void status.refetch()}><RefreshCw className={status.isFetching ? 'spin' : ''} size={16} /> Перечитать snapshot</button></div>
            </header>
            {status.isLoading ? <PageLoading /> : status.isError ? <ErrorState error={status.error} retry={() => void status.refetch()} /> : <div className="server-telemetry">
              <Gauge label="Capacity" value={capacityUsage} />
              <dl className="telemetry-list">
                <Metric label="Онлайн-сессии" value={formatNumber(nodeStatus?.online_sessions)} />
                <Metric label="Получено" value={formatBytes(nodeStatus?.bytes_received)} />
                <Metric label="Отправлено" value={formatBytes(nodeStatus?.bytes_sent)} />
                <Metric label="Сертификат до" value={formatDateTime(nodeStatus?.certificate_expires_at)} />
                <Metric label="Manager version" value={nodeStatus?.manager_version || '—'} />
                <Metric label="Inventory revision" value={nodeStatus?.inventory_revision || node.inventory_revision || '—'} />
              </dl>
            </div>}
          </section>
          <section className="panel">
            <header className="panel__header"><div><h2>Inventory и drift</h2><p>Revision {nodeStatus?.inventory_revision || node.inventory_revision || '—'}</p></div></header>
            {nodeStatus?.drift?.length ? <div className="drift-list">{nodeStatus.drift.map((item, index) => <div key={`${item.code}-${index}`}><StatusBadge value={item.severity} /><span><strong>{item.code || 'drift'}</strong><small>{item.message}</small></span></div>)}</div> : <div className="success-state"><CheckCircle2 /><span><strong>Расхождений не обнаружено</strong><small>По результату последнего сохранённого drift-аудита.</small></span></div>}
          </section>
        </div>

        {canManage && <aside className="stack">
          <section className="panel action-panel">
            <header className="panel__header"><div><h2>Действия</h2><p>Операции требуют причины и журналируются</p></div></header>
            <ActionButton icon={<Activity />} title="Проверить Manager" subtitle="Запросить новый health/status snapshot" onClick={() => openAction({ action: 'health_check', title: 'Проверить сервер', description: 'Запустить проверку Manager и OpenVPN без изменения lifecycle.', confirm: 'ПРОВЕРИТЬ' })} />
            <ActionButton icon={<DatabaseZap />} title="Обновить inventory" subtitle="Запросить актуальный inventory у Manager" onClick={() => openAction({ action: 'refresh_inventory', title: 'Обновить inventory', description: 'Запросить актуальный inventory у Manager.', confirm: 'ОБНОВИТЬ' })} />
            <ActionButton icon={<GitCompareArrows />} title="Проверить drift" subtitle="Сравнить desired и actual state" onClick={() => openAction({ action: 'audit_drift', title: 'Проверить drift', description: 'Сравнить inventory с desired state без автоматического исправления.', confirm: 'ПРОВЕРИТЬ' })} />
            {lifecycle === 'active' && <ActionButton icon={node.accepts_new_configs ? <Ban /> : <CheckCircle2 />} title={node.accepts_new_configs ? 'Закрыть для новых конфигов' : 'Разрешить новые конфиги'} subtitle="Существующие клиенты не затрагиваются" onClick={() => openAction({ action: 'set_accepting', title: node.accepts_new_configs ? 'Закрыть размещение' : 'Открыть размещение', description: node.accepts_new_configs ? 'Запретить размещение новых конфигураций на узле.' : 'Разрешить размещение новых конфигураций на проверенном узле.', confirm: node.accepts_new_configs ? 'ЗАКРЫТЬ' : 'ОТКРЫТЬ', body: { accepts_new_configs: !node.accepts_new_configs } })} />}
            {lifecycle === 'active' && <ActionButton danger icon={<Ban />} title="Перевести в drain" subtitle="Запретить новые размещения" onClick={() => openAction({ action: 'drain', title: 'Перевести сервер в drain', description: 'Запретить новые размещения; существующие подключения продолжат работать.', confirm: 'DRAIN', danger: true })} />}
            {['active', 'draining'].includes(lifecycle) && <ActionButton danger icon={<Power />} title="Отключить сервер" subtitle="Остановить использование узла хабом" onClick={() => openAction({ action: 'disable', title: 'Отключить сервер', description: 'Перевести сервер в disabled и запретить новые размещения.', confirm: 'ОТКЛЮЧИТЬ', danger: true })} />}
            {['draining', 'disabled'].includes(lifecycle) && <ActionButton icon={<Power />} title="Активировать сервер" subtitle="Требует свежий healthy snapshot и совпавший identity" onClick={() => openAction({ action: 'activate', title: 'Активировать сервер', description: 'Активировать проверенный сервер и разрешить новые размещения.', confirm: 'АКТИВИРОВАТЬ' })} />}
            {lifecycle !== 'retired' && configCount === 0 && <ActionButton danger icon={<Archive />} title="Вывести сервер навсегда" subtitle="Retire нельзя отменить" onClick={() => openAction({ action: 'retire', title: 'Навсегда вывести сервер', description: 'Перевести пустой сервер в retired. Повторная активация будет невозможна.', confirm: `ВЫВЕСТИ ${node.name || node.id}`, danger: true })} />}
          </section>
          <section className="panel capacity-form">
            <header className="panel__header"><div><h2>Capacity</h2><p>Лимит размещения</p></div></header>
            <form onSubmit={updateCapacity}><label className="field"><span>Максимум конфигураций</span><input type="number" min="1" step="1" value={capacity} onChange={(event) => setCapacity(event.target.value)} placeholder={String(node.capacity ?? '')} /></label><button type="submit" className="button button--secondary button--wide" disabled={!capacity}><Save size={16} /> Изменить лимит</button></form>
          </section>
        </aside>}
      </div>

      <Modal open={Boolean(pending)} title={pending?.title ?? ''} description={pending?.description} onClose={() => { if (!action.isPending) setPending(null) }}>
        <div className="form-stack">
          <label className="field"><span>Причина действия</span><textarea rows={3} minLength={3} maxLength={500} required value={actionReason} onChange={(event) => setActionReason(event.target.value)} /></label>
          <label className="field"><span>Введите <strong>{pending?.confirm}</strong> для подтверждения</span><input autoFocus value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
          {action.isError && <div className="form-error" role="alert">{action.error instanceof Error ? action.error.message : 'Операция не запущена'}</div>}
          <div className="form-actions"><button className="button button--secondary" type="button" onClick={() => setPending(null)}>Отмена</button><button className={`button ${pending?.danger ? 'button--danger' : 'button--primary'}`} type="button" disabled={confirmation !== pending?.confirm || actionReason.trim().length < 3 || action.isPending} onClick={submitAction}>{action.isPending ? 'Выполняем…' : 'Подтвердить'}</button></div>
        </div>
      </Modal>
      {editOpen && <ServerSettingsDialog server={node} onClose={() => setEditOpen(false)} />}
    </>
  )
}

async function waitForOperation(initial: VpnOperation, serverId: string) {
  if (TERMINAL_SUCCESS.has(initial.status ?? '') || TERMINAL_FAILURE.has(initial.status ?? '')) return initial
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 750))
    const page = await operationsApi.list({ server_id: serverId, source: 'server', limit: 100 })
    const current = page.items.find((item) => String(item.id) === String(initial.id))
    if (current && (TERMINAL_SUCCESS.has(current.status ?? '') || TERMINAL_FAILURE.has(current.status ?? ''))) return current
  }
  throw new Error('Операция принята, но не завершилась вовремя. Проверьте журнал операций.')
}

function ServerSettingsDialog({ server, onClose }: { server: Server; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [validation, setValidation] = useState('')
  const mutation = useMutation({
    mutationFn: (input: Record<string, unknown>) => serversApi.update(String(server.id), input),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['server', String(server.id)] }),
        queryClient.invalidateQueries({ queryKey: ['servers'] }),
      ])
      onClose()
    },
  })
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setValidation('')
    if (!server.version) return setValidation('Версия сервера не загружена. Обновите страницу и повторите попытку.')
    const form = new FormData(event.currentTarget)
    const maximum = String(form.get('max_configs') ?? '').trim()
    const reserve = Number(form.get('capacity_reserve') || 0)
    const apiKey = String(form.get('api_key') ?? '').trim()
    if (maximum && reserve >= Number(maximum)) return setValidation('Резерв должен быть меньше общего capacity.')
    mutation.mutate({
      expected_version: server.version,
      name: String(form.get('name')).trim(),
      ip: String(form.get('ip')).trim(),
      port: Number(form.get('port')),
      host: String(form.get('host')).trim(),
      location: String(form.get('location')).trim(),
      provider: String(form.get('provider') ?? '').trim() || null,
      public_endpoint: String(form.get('public_endpoint') ?? '').trim() || null,
      monthly_cost: String(form.get('monthly_cost') || '0.00').replace(',', '.'),
      ...(maximum ? { max_configs: Number(maximum) } : { clear_max_configs: true }),
      capacity_reserve: reserve,
      placement_weight: String(form.get('placement_weight') || '1'),
      ...(apiKey ? { api_key: apiKey } : {}),
    })
  }
  return <Modal open title="Настройки сервера" description={`Версия ${server.version ?? '—'} · endpoint Manager можно менять только после вывода всех конфигураций.`} onClose={onClose}><form className="form-stack" onSubmit={submit}><div className="form-grid"><label className="field"><span>Название</span><input name="name" required maxLength={128} defaultValue={server.name} /></label><label className="field"><span>Локация</span><input name="location" required maxLength={128} defaultValue={server.location} /></label><label className="field"><span>Manager IP</span><input name="ip" required maxLength={64} defaultValue={server.ip} /></label><label className="field"><span>Manager port</span><input name="port" type="number" min="1" max="65535" required defaultValue={server.port ?? 16290} /></label><label className="field"><span>Manager host</span><input name="host" required maxLength={128} defaultValue={server.host} /></label><label className="field"><span>Новый Manager API key</span><input name="api_key" type="password" autoComplete="new-password" maxLength={4096} placeholder="Оставьте пустым без ротации" /></label><label className="field"><span>Провайдер</span><input name="provider" maxLength={128} defaultValue={server.provider ?? ''} /></label><label className="field"><span>Публичный endpoint</span><input name="public_endpoint" maxLength={255} defaultValue={server.public_endpoint ?? server.vpn_endpoint ?? ''} /></label><label className="field"><span>Стоимость в месяц, ₽</span><input name="monthly_cost" inputMode="decimal" required pattern="\d+(?:[.,]\d{1,2})?" defaultValue={server.monthly_cost ?? '0.00'} /></label><label className="field"><span>Capacity</span><input name="max_configs" type="number" min="1" defaultValue={server.capacity ?? ''} /></label><label className="field"><span>Резерв capacity</span><input name="capacity_reserve" type="number" min="0" defaultValue={server.capacity_reserve ?? 0} /></label><label className="field"><span>Placement weight</span><input name="placement_weight" inputMode="decimal" pattern="\d+(?:\.\d{1,3})?" defaultValue={server.placement_weight ?? '1'} /></label></div>{(validation || mutation.isError) && <div className="form-error" role="alert">{validation || (mutation.error instanceof Error ? mutation.error.message : 'Не удалось сохранить сервер')}</div>}<div className="form-actions"><button type="button" className="button button--secondary" onClick={onClose}>Отмена</button><button type="submit" className="button button--primary" disabled={mutation.isPending}>{mutation.isPending ? 'Сохраняем…' : 'Сохранить'}</button></div></form></Modal>
}

function Gauge({ label, value }: { label: string; value?: number }) {
  const normalized = Math.max(0, Math.min(100, value ?? 0))
  const tone = value === undefined ? 'neutral' : normalized >= 90 ? 'danger' : normalized >= 75 ? 'warning' : 'success'
  return <div className="metric-gauge"><div className={`metric-gauge__ring metric-gauge__ring--${tone}`} style={{ '--progress': `${normalized * 3.6}deg` } as CSSProperties}><span>{value === undefined ? '—' : percent(normalized)}</span></div><strong>{label}</strong></div>
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>
}

function ActionButton({ icon, title, subtitle, onClick, danger }: { icon: ReactNode; title: string; subtitle: string; onClick: () => void; danger?: boolean }) {
  return <button type="button" className={`action-row ${danger ? 'action-row--danger' : ''}`} onClick={onClick}><span>{icon}</span><span><strong>{title}</strong><small>{subtitle}</small></span></button>
}

function ageLabel(value?: string | null) {
  if (!value) return 'Нет snapshot'
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return 'Время неизвестно'
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  if (seconds < 60) return `${seconds} сек. назад`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} мин. назад`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} ч. назад`
  return `${Math.floor(hours / 24)} дн. назад`
}
