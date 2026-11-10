import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Search, Send } from 'lucide-react'
import { useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { usersApi } from '../api'
import { campaignsApi, type Campaign, type CampaignInput, type CampaignPreview } from '../api/campaigns'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthProvider'
import { DataTable, type Column } from '../components/DataTable'
import { Modal } from '../components/Modal'
import { PageHeader, StatCard } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, ForbiddenState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatNumber, userLabel } from '../lib/format'

const PAGE_SIZE = 25
const labels: Record<string, string> = { draft: 'Черновик', queued: 'В очереди', running: 'Отправляется', completed: 'Завершена', cancelled: 'Отменена' }
const busy = (campaign?: Campaign) => campaign?.status === 'queued' || campaign?.status === 'running'
const fingerprint = ({ name, text, audience }: CampaignInput) => JSON.stringify({ name, text, audience })

function parseUserIds(value: string) {
  const tokens = value.trim().split(/[\s,;]+/).filter(Boolean)
  const valid = tokens.every((token) => /^\d+$/.test(token) && Number.isSafeInteger(Number(token)) && Number(token) > 0)
  return { valid, ids: valid ? [...new Set(tokens.map(Number))].sort((a, b) => a - b) : [] }
}

function useIntentKey() {
  const keys = useRef(new Map<string, string>())
  const getKey = (intent: string) => {
    if (!keys.current.has(intent)) keys.current.set(intent, crypto.randomUUID())
    return keys.current.get(intent)!
  }
  return { getKey, clearKeys: () => keys.current.clear() }
}

export function CampaignsPage() {
  const { user: admin, loading } = useAuth()
  const permissions = admin?.permissions ?? []
  const canRead = permissions.includes('campaigns:read')
  const canWrite = permissions.includes('campaigns:write')
  const canSend = permissions.includes('campaigns:send')
  const [params] = useSearchParams()
  const [offset, setOffset] = useState(0)
  const [editorKey, setEditorKey] = useState(0)
  const [editing, setEditing] = useState<Campaign | undefined>()
  const [selectedId, setSelectedId] = useState<string | number>()
  const query = useQuery({ queryKey: ['campaigns', offset], queryFn: () => campaignsApi.list({ limit: PAGE_SIZE, offset }), enabled: canRead, refetchInterval: (state) => state.state.data?.items.some(busy) ? 5000 : false })
  const detail = useQuery({ queryKey: ['campaign', selectedId], queryFn: () => campaignsApi.detail(selectedId!), enabled: canRead && selectedId !== undefined, refetchInterval: (state) => busy(state.state.data) ? 3000 : false })
  const columns: Column<Campaign>[] = [
    { key: 'name', header: 'Рассылка', render: (row) => <button className="button button--ghost button--small" type="button" onClick={() => { setSelectedId(row.id); if (row.status === 'draft' && canWrite) { setEditing(row); setEditorKey((value) => value + 1) } }}>{row.name}</button> },
    { key: 'audience', header: 'Аудитория', render: (row) => row.audience.kind === 'all' ? 'Все доступные пользователи' : `Выбранные: ${formatNumber(row.audience.user_ids.length)}` },
    { key: 'status', header: 'Статус', render: (row) => <StatusBadge value={row.status} label={labels[row.status]} /> },
    { key: 'delivered', header: 'Доставлено', align: 'end', render: (row) => `${formatNumber(row.counts.delivered)} / ${formatNumber(row.counts.total)}` },
    { key: 'failed', header: 'Ошибки', align: 'end', render: (row) => formatNumber(row.counts.failed) },
    { key: 'created', header: 'Создана', render: (row) => formatDateTime(row.created_at) },
  ]
  if (loading) return <PageLoading />
  if (!canRead) return <ForbiddenState />
  return <>
    <PageHeader eyebrow="Telegram" title="Рассылки" description="Сообщения всем доступным пользователям или выбранным аккаунтам. Сначала черновик и проверка аудитории, затем отдельный запуск." actions={canWrite && <button className="button button--primary" type="button" onClick={() => { setEditing(undefined); setEditorKey((value) => value + 1); setSelectedId(undefined) }}><Plus size={17} /> Новый черновик</button>} />
    <div className="stack">
      {canWrite && <CampaignEditor key={editorKey} initial={editing} initialUserId={!editing ? params.get('user_id') : null} canSend={canSend} canReadUsers={permissions.includes('users:read')} onSaved={(campaign) => setSelectedId(campaign.id)} />}
      {selectedId !== undefined && (detail.isLoading ? <PageLoading /> : detail.isError ? <ErrorState error={detail.error} retry={() => void detail.refetch()} /> : detail.data && (detail.data.status !== 'draft' || !canWrite) && <CampaignProgress campaign={detail.data} canSend={canSend} />)}
      <section className="panel"><header className="panel__header"><div><h2>История рассылок</h2><p>Черновики, ход доставки и результат</p></div></header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><DataTable label="Рассылки" columns={columns} rows={query.data!.items} rowKey={(row) => row.id} /><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={setOffset} /></> : <EmptyState title="Рассылок пока нет" description="Сохраните первый черновик. Сохранение и проверка аудитории ничего не отправляют." />}</section>
    </div>
  </>
}

function CampaignEditor({ initial, initialUserId, canSend, canReadUsers, onSaved }: { initial?: Campaign; initialUserId: string | null; canSend: boolean; canReadUsers: boolean; onSaved: (campaign: Campaign) => void }) {
  const queryClient = useQueryClient()
  const { getKey } = useIntentKey()
  const [draft, setDraft] = useState(initial)
  const [name, setName] = useState(initial?.name ?? '')
  const [text, setText] = useState(initial?.text ?? '')
  const [kind, setKind] = useState<'all' | 'selected'>(initial?.audience.kind ?? (initialUserId ? 'selected' : 'all'))
  const [selectedIds, setSelectedIds] = useState(initial?.audience.user_ids.join(', ') ?? initialUserId ?? '')
  const [search, setSearch] = useState('')
  const [preview, setPreview] = useState<{ value: CampaignPreview; fingerprint: string }>()
  const [reviewOpen, setReviewOpen] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [countConfirmation, setCountConfirmation] = useState('')
  const [reason, setReason] = useState('')
  const parsedIds = parseUserIds(selectedIds)
  const input: CampaignInput = { name: name.trim(), text, audience: { kind, user_ids: kind === 'selected' ? parsedIds.ids : [] } }
  const inputFingerprint = fingerprint(input)
  const valid = input.name.length > 0 && input.name.length <= 120 && text.trim().length > 0 && text.length <= 4096 && (kind === 'all' || (parsedIds.valid && parsedIds.ids.length > 0))
  const users = useQuery({ queryKey: ['campaign-user-search', search.trim()], queryFn: () => usersApi.list({ q: search.trim(), limit: 10, offset: 0 }), enabled: canReadUsers && kind === 'selected' && search.trim().length >= 2 })
  const invalidate = () => { setPreview(undefined); setReviewOpen(false); setConfirmation(''); setCountConfirmation('') }
  const save = useMutation({
    mutationFn: async () => {
      const saved = draft && fingerprint(draft) === inputFingerprint ? draft : draft ? await campaignsApi.update(draft.id, input, draft.revision, getKey(`update:${draft.id}:${draft.revision}:${inputFingerprint}`)) : await campaignsApi.create(input, getKey(`create:${inputFingerprint}`))
      setDraft(saved)
      onSaved(saved)
      await queryClient.invalidateQueries({ queryKey: ['campaigns'] })
      const checked = await campaignsApi.preview(saved.id)
      return { value: checked, fingerprint: inputFingerprint }
    },
    onSuccess: (checked) => setPreview(checked),
  })
  const launch = useMutation({
    mutationFn: () => campaignsApi.launch(draft!.id, preview!.value, reason.trim(), getKey(`launch:${draft!.id}:${preview!.value.preview_token}:${reason.trim()}`)),
    onSuccess: async (campaign) => { setDraft(campaign); setReviewOpen(false); setPreview(undefined); onSaved(campaign); await queryClient.invalidateQueries({ queryKey: ['campaigns'] }); await queryClient.invalidateQueries({ queryKey: ['campaign', campaign.id] }) },
    onError: (error) => { if (error instanceof ApiError && error.status === 409) invalidate() },
  })
  const deliveryPaused = draft?.delivery_paused === true
  const previewFresh = Boolean(preview && preview.fingerprint === inputFingerprint && (!preview.value.expires_at || Date.parse(preview.value.expires_at) > Date.now()))
  const locked = save.isPending || launch.isPending || (draft !== undefined && draft.status !== 'draft')
  const addUser = (id: string | number) => { invalidate(); setSelectedIds([...new Set([...parsedIds.ids, Number(id)])].join(', ')) }
  if (draft && draft.status !== 'draft') return null
  return <section className="panel"><header className="panel__header"><div><h2>{draft ? `Черновик: ${draft.name}` : 'Новое сообщение'}</h2><p>Обычный текст, до 4096 символов. Проверка аудитории ничего не отправляет.</p></div>{draft && <StatusBadge value={draft.status} label={labels[draft.status]} />}</header><div className="campaign-editor"><CampaignDeliveryNotice paused={deliveryPaused} queued={false} /><div className="content-grid content-grid--two">
    <div className="form-stack">
      <label className="field"><span>Название рассылки</span><input value={name} maxLength={120} disabled={locked} onChange={(event) => { invalidate(); setName(event.target.value) }} /></label>
      <label className="field"><span>Текст сообщения</span><textarea aria-label="Текст сообщения" rows={8} value={text} maxLength={4096} disabled={locked} onChange={(event) => { invalidate(); setText(event.target.value) }} /><small>{text.length} / 4096</small></label>
      <label className="field"><span>Получатели</span><select value={kind} disabled={locked} onChange={(event) => { invalidate(); setKind(event.target.value as typeof kind) }}><option value="all">Все доступные пользователи</option><option value="selected">Выбранные пользователи</option></select></label>
      {kind === 'selected' && <><label className="field"><span>ID пользователей</span><textarea aria-label="ID пользователей" rows={2} value={selectedIds} disabled={locked} placeholder="Например: 42, 57" onChange={(event) => { invalidate(); setSelectedIds(event.target.value) }} /><small>Внутренние ID из админки, через запятую или пробел. Повторы исключаются.</small></label>{!parsedIds.valid && <p className="form-error" role="alert">Укажите положительные целые ID пользователей.</p>}{canReadUsers && <div className="form-stack"><label className="field"><span>Найти получателя</span><span className="search-box"><Search size={16} /><input aria-label="Найти получателя" value={search} disabled={locked} onChange={(event) => setSearch(event.target.value)} placeholder="Username или ID, от двух символов" /></span></label>{users.isFetching && <span>Ищем…</span>}{users.isError && <ErrorState error={users.error} />}{users.data && <div className="button-group">{users.data.items.map((user) => <button key={user.id} className="button button--secondary button--small" type="button" disabled={locked || parsedIds.ids.includes(Number(user.id))} onClick={() => addUser(user.id)}>{userLabel(user)} · #{user.id}</button>)}</div>}</div>}</>}
      {save.isError && <div className="form-error" role="alert">{save.error.message}</div>}
      {launch.isError && !reviewOpen && <div className="form-error" role="alert">{launch.error.message} Проверьте аудиторию заново перед запуском.</div>}
      <div className="form-actions"><button className="button button--secondary" type="button" disabled={!valid || locked} onClick={() => { invalidate(); save.mutate() }}>{save.isPending ? 'Проверяем…' : 'Сохранить и проверить аудиторию'}</button></div>
    </div>
    <div className="stack"><section className="panel"><header className="panel__header"><div><h2>Предпросмотр сообщения</h2><p>Получатель увидит этот текст</p></div></header><div className="campaign-message">{text || 'Здесь появится текст сообщения'}</div></section>
      {preview && previewFresh ? <section className="panel"><header className="panel__header"><div><h2>Проверенная аудитория</h2><p>Проверка действует до {formatDateTime(preview.value.expires_at)}</p></div></header><div className="campaign-review"><dl className="campaign-counts"><div><dt>Получат сообщение</dt><dd>{formatNumber(preview.value.eligible_count)}</dd></div><div><dt>Пропущены: бот заблокирован или недоступен</dt><dd>{formatNumber(preview.value.skipped_count)}</dd></div><div><dt>ID не найдены</dt><dd>{formatNumber(preview.value.missing_count)}</dd></div></dl>{preview.value.sample.length > 0 && <p>Пример получателей: {preview.value.sample.map((item) => item.display).join(', ')}</p>}{canSend ? <button type="button" className="button button--primary" disabled={!preview.value.eligible_count || locked} onClick={() => { setConfirmation(''); setCountConfirmation(''); setReviewOpen(true) }}><Send size={16} /> Проверить и запустить</button> : <p>Для запуска требуется разрешение на отправку рассылок.</p>}</div></section> : <div className="attention-strip"><div><strong>Аудитория ещё не подтверждена</strong><span>После изменения текста или получателей выполните проверку заново.</span></div></div>}
    </div>
  </div></div>
    <Modal open={reviewOpen} title="Запуск рассылки" description={deliveryPaused ? 'После подтверждения будет создана очередь. Доставка приостановлена; очередь будет ждать включения доставки оператором.' : 'После подтверждения сообщения попадут в очередь доставки Telegram. Повторно проверьте текст и число получателей.'} onClose={() => { if (!launch.isPending) setReviewOpen(false) }}><div className="form-stack"><CampaignDeliveryNotice paused={deliveryPaused} queued={false} /><div className="campaign-message">{text}</div><strong>Получателей: {formatNumber(preview?.value.eligible_count ?? 0)}</strong><label className="field"><span>Введите число получателей</span><input inputMode="numeric" value={countConfirmation} disabled={launch.isPending} onChange={(event) => setCountConfirmation(event.target.value)} /></label><label className="field"><span>Для подтверждения введите SEND</span><input autoComplete="off" value={confirmation} disabled={launch.isPending} onChange={(event) => setConfirmation(event.target.value)} /></label><label className="field"><span>Причина запуска</span><textarea value={reason} minLength={3} maxLength={500} disabled={launch.isPending} onChange={(event) => setReason(event.target.value)} /></label>{launch.isError && <div className="form-error" role="alert">{launch.error.message} При устаревшей аудитории закройте окно и проверьте её заново.</div>}<div className="form-actions"><button className="button button--secondary" type="button" disabled={launch.isPending} onClick={() => setReviewOpen(false)}>Вернуться</button><button className="button button--danger" type="button" disabled={launch.isPending || !previewFresh || confirmation !== 'SEND' || countConfirmation !== String(preview?.value.eligible_count) || reason.trim().length < 3} onClick={() => launch.mutate()}>{launch.isPending ? 'Создаём очередь…' : deliveryPaused ? 'СОЗДАТЬ ОЧЕРЕДЬ РАССЫЛКИ' : 'ЗАПУСТИТЬ РАССЫЛКУ'}</button></div></div></Modal>
  </section>
}

function CampaignProgress({ campaign, canSend }: { campaign: Campaign; canSend: boolean }) {
  const queryClient = useQueryClient()
  const { getKey, clearKeys } = useIntentKey()
  const [action, setAction] = useState<'cancel' | 'retry'>()
  const [reason, setReason] = useState('')
  const counts = campaign.counts
  const done = counts.delivered + counts.failed + counts.unknown + counts.skipped + counts.cancelled
  const mutation = useMutation({ mutationFn: () => campaignsApi[action!](campaign.id, reason.trim(), getKey(`${action}:${campaign.id}:${campaign.revision}:${reason.trim()}`)), onSuccess: async () => { clearKeys(); setAction(undefined); setReason(''); await queryClient.invalidateQueries({ queryKey: ['campaigns'] }); await queryClient.invalidateQueries({ queryKey: ['campaign', campaign.id] }) } })
  return <section className="panel"><header className="panel__header"><div><h2>{campaign.name}</h2><p>Запущена {formatDateTime(campaign.launched_at)}</p></div><StatusBadge value={campaign.status} label={labels[campaign.status]} /></header><div className="campaign-review"><CampaignDeliveryNotice paused={campaign.delivery_paused === true} queued={busy(campaign) || counts.pending + counts.queued + counts.sending > 0} /><div className="stats-grid stats-grid--four"><StatCard label="Доставлено" value={formatNumber(counts.delivered)} tone="positive" /><StatCard label="В очереди / отправляется" value={formatNumber(counts.pending + counts.queued + counts.sending)} /><StatCard label="Ошибки доставки" value={formatNumber(counts.failed)} tone={counts.failed ? 'danger' : 'default'} /><StatCard label="Пропущены / отменены" value={`${formatNumber(counts.skipped)} / ${formatNumber(counts.cancelled)}`} /></div><div className="progress" role="progressbar" aria-label="Ход рассылки" aria-valuemin={0} aria-valuemax={counts.total} aria-valuenow={done}><i style={{ width: `${counts.total ? Math.min(100, done / counts.total * 100) : 0}%` }} /></div><p>{formatNumber(done)} из {formatNumber(counts.total)} обработано. Неизвестный результат: {formatNumber(counts.unknown)} — повторно автоматически не отправляется.</p><div className="campaign-message">{campaign.text}</div>{canSend && <div className="form-actions">{busy(campaign) && <button className="button button--danger" type="button" onClick={() => { setReason(''); setAction('cancel') }}>Остановить рассылку</button>}{counts.failed > 0 && !busy(campaign) && campaign.status !== 'draft' && <button className="button button--secondary" type="button" onClick={() => { setReason(''); setAction('retry') }}>Повторить ошибки ({counts.failed})</button>}</div>}</div><Modal open={action !== undefined} title={action === 'retry' ? 'Повторить неудачные доставки' : 'Остановить рассылку'} description={action === 'retry' ? `Повтор только для ошибок: ${counts.failed}. Доставленные сообщения и неизвестные результаты не будут отправлены повторно.` : 'Оставшаяся очередь будет отменена. Уже отправленные сообщения останутся у получателей; сообщения, которые уже отправляются, могут дойти.'} onClose={() => { if (!mutation.isPending) setAction(undefined) }}><div className="form-stack"><label className="field"><span>Причина действия</span><textarea minLength={3} maxLength={500} disabled={mutation.isPending} value={reason} onChange={(event) => setReason(event.target.value)} /></label>{mutation.isError && <div className="form-error" role="alert">{mutation.error.message}</div>}<div className="form-actions"><button className="button button--secondary" type="button" disabled={mutation.isPending} onClick={() => setAction(undefined)}>Вернуться</button><button className={action === 'cancel' ? 'button button--danger' : 'button button--primary'} type="button" disabled={reason.trim().length < 3 || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? 'Выполняем…' : action === 'retry' ? 'Повторить только ошибки' : 'Подтвердить остановку'}</button></div></div></Modal></section>
}

function CampaignDeliveryNotice({ paused, queued }: { paused: boolean; queued: boolean }) {
  if (!paused) return null
  return <div className="product-notice" role="status"><strong>Доставка приостановлена</strong><p>{queued ? 'Очередь создана и будет ждать включения доставки оператором. Сообщения, которые уже отправляются, могут дойти.' : 'После запуска очередь будет ждать включения доставки оператором. Создание очереди не включает доставку.'}</p></div>
}
