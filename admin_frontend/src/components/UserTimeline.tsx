import { useQuery } from '@tanstack/react-query'
import {
  Bot,
  CalendarDays,
  CircleDollarSign,
  Gift,
  History,
  Network,
  RotateCcw,
  Search,
  ShieldCheck,
  UserRound,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { usersApi, type TimelineActor, type TimelineCategory, type UserTimelineEvent } from '../api'
import { formatDateTime } from '../lib/format'
import { Pagination } from './Pagination'
import { EmptyState, ErrorState, PageLoading } from './States'
import { StatusBadge } from './StatusBadge'

const PAGE_SIZE = 25
const PARAMS = {
  category: 'history_category',
  action: 'history_action',
  result: 'history_result',
  from: 'history_from',
  to: 'history_to',
  offset: 'history_offset',
} as const

const categoryOptions: Array<{ value: '' | TimelineCategory; label: string; icon: LucideIcon }> = [
  { value: '', label: 'Все события', icon: History },
  { value: 'bot', label: 'Бот', icon: Bot },
  { value: 'finance', label: 'Финансы', icon: CircleDollarSign },
  { value: 'vpn', label: 'VPN', icon: Network },
  { value: 'referral', label: 'Рефералы', icon: Gift },
  { value: 'admin', label: 'Администраторы', icon: ShieldCheck },
  { value: 'account', label: 'Аккаунт', icon: UserRound },
]

const categoryLabel: Record<TimelineCategory, string> = {
  bot: 'Telegram-бот',
  finance: 'Финансы',
  vpn: 'VPN',
  referral: 'Рефералы',
  admin: 'Администратор',
  account: 'Аккаунт',
}

const actionLabels: Record<string, string> = {
  'navigation.start': 'Запущен бот',
  'navigation.menu': 'Открыто главное меню',
  'navigation.help': 'Открыта помощь',
  'navigation.cancel': 'Действие отменено',
  'navigation.instructions_open': 'Открыты инструкции',
  'navigation.guide_open': 'Открыта инструкция для устройства',
  'finance.balance_view': 'Просмотрен баланс',
  'finance.balance_history': 'Открыта детализация баланса',
  'finance.topup_open': 'Запрошено пополнение',
  'finance.payment_provider_select': 'Выбран способ оплаты',
  'finance.payment_amount_select': 'Выбрана сумма пополнения',
  'finance.payment_pre_checkout': 'Проверка платежа в Telegram',
  'finance.payment_successful': 'Telegram сообщил об успешном платеже',
  'vpn.config_list': 'Запрошен список конфигураций',
  'vpn.config_view': 'Запрошен просмотр VPN-конфигурации',
  'vpn.config_create_start': 'Запрошено создание конфигурации',
  'vpn.config_create_submit': 'Отправлена заявка на создание конфигурации',
  'vpn.config_server_select': 'Выбран VPN-сервер',
  'vpn.config_suspend': 'Запрошена приостановка конфигурации',
  'vpn.config_resume': 'Запрошено возобновление конфигурации',
  'vpn.config_delete_request': 'Запрошено удаление конфигурации',
  'vpn.config_delete_confirm': 'Подтверждено удаление конфигурации',
  'vpn.config_download': 'Запрошено скачивание VPN-конфигурации',
  'vpn.config_rename_start': 'Начато переименование конфигурации',
  'vpn.config_rename_submit': 'Конфигурация переименована',
  'referral.overview': 'Открыта реферальная программа',
  'message.command_received': 'Отправлена команда боту',
  'message.received': 'Отправлено сообщение боту',
  'message.unrecognized': 'Сообщение не распознано',
  'callback.received': 'Нажата кнопка в боте',
  'update.received': 'Получено событие Telegram',
  'privacy.non_private_input': 'Обращение к боту вне личного чата',
  'access.invite_lookup': 'Проверен доступ по приглашению',
  'access.invite_required': 'Доступ без приглашения отклонён',
  'bot.command.received': 'Отправлена команда боту',
  'bot.callback.received': 'Нажата кнопка в боте',
  'bot.message.received': 'Отправлено сообщение боту',
  'bot.start': 'Запущен бот',
  'bot.balance.opened': 'Открыт баланс',
  'bot.configs.opened': 'Открыты конфигурации',
  'bot.payment.opened': 'Открыто пополнение',
  'bot.instructions.opened': 'Открыты инструкции',
  'bot.referrals.opened': 'Открыта реферальная программа',
  'payment.created': 'Создан платёж',
  'payment.credited': 'Платёж зачислен',
  'balance.credited': 'Баланс пополнен',
  'balance.debited': 'Средства списаны',
  'referral.reward.credited': 'Начислено реферальное вознаграждение',
  'vpn.config.created': 'Создана VPN-конфигурация',
  'vpn.config.suspended': 'VPN-конфигурация приостановлена',
  'vpn.config.unsuspended': 'VPN-конфигурация возобновлена',
  'vpn.config.revoked': 'VPN-конфигурация отозвана',
  'account.registered': 'Пользователь зарегистрирован',
  'account.delivery_status.changed': 'Изменился статус Telegram',
  'admin.balance.adjusted': 'Администратор изменил баланс',
}

const resultLabels: Record<string, string> = {
  handled: 'Обработано ботом',
  processed: 'Обработано',
  success: 'Успешно',
  succeeded: 'Успешно',
  completed: 'Завершено',
  credited: 'Зачислено',
  accepted: 'Принято',
  pending: 'Ожидает',
  queued: 'В очереди',
  running: 'Выполняется',
  processing: 'Выполняется',
  failed: 'Ошибка',
  error: 'Ошибка',
  rejected: 'Отклонено',
  blocked: 'Бот заблокирован',
  deactivated: 'Деактивирован',
  cancelled: 'Отменено',
  expired: 'Истёк',
  ignored: 'Пропущено',
  invalid: 'Некорректное действие',
  unavailable: 'Недоступно',
  superseded: 'Заменено новой операцией',
  exhausted: 'Попытки исчерпаны',
}

const sensitiveKey = /(^|[_-])(authorization|cookie|credential|password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)($|[_-])/i

function safeMetadata(value: unknown, depth = 0): unknown {
  if (depth >= 5) return '[сокращено]'
  if (Array.isArray(value)) {
    const items = value.slice(0, 30).map((item) => safeMetadata(item, depth + 1))
    return value.length > 30 ? [...items, `[ещё ${value.length - 30}]`] : items
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).slice(0, 60)
    const result = Object.fromEntries(entries.map(([key, item]) => [key, sensitiveKey.test(key) ? '[скрыто]' : safeMetadata(item, depth + 1)]))
    if (Object.keys(value).length > entries.length) result['…'] = `[ещё ${Object.keys(value).length - entries.length} полей]`
    return result
  }
  if (typeof value === 'string' && value.length > 800) return `${value.slice(0, 800)}…`
  return value
}

function actorLabel(actor: TimelineActor | string | null | undefined) {
  if (!actor) return 'Система'
  if (typeof actor === 'string') return actor
  if (actor.label) return actor.label
  if (actor.type === 'user') return actor.id ? `Пользователь #${actor.id}` : 'Пользователь'
  if (actor.type === 'admin') return actor.id ? `Администратор #${actor.id}` : 'Администратор'
  return 'Система'
}

function actionLabel(action: string) {
  if (actionLabels[action]) return actionLabels[action]
  return action.replace(/[._-]+/g, ' ').replace(/^./, (letter) => letter.toUpperCase())
}

function TimelineItem({ event }: { event: UserTimelineEvent }) {
  const option = categoryOptions.find((item) => item.value === event.category) ?? categoryOptions[0]
  const Icon = option.icon
  const details = safeMetadata(event.metadata) as Record<string, unknown>
  const hasDetails = Object.keys(details).length > 0
  const result = event.result.toLowerCase()
  return (
    <li className={`timeline-event timeline-event--${event.category}`}>
      <span className="timeline-event__icon" aria-hidden="true"><Icon /></span>
      <article>
        <header>
          <div>
            <strong>{event.title || actionLabel(event.action)}</strong>
            <span>{categoryLabel[event.category]} · {actionLabel(event.action)}</span>
          </div>
          <StatusBadge value={result} label={resultLabels[result] ?? event.result} />
        </header>
        {event.description && <p>{event.description}</p>}
        <footer>
          <time dateTime={event.occurred_at}>{formatDateTime(event.occurred_at)}</time>
          <span>{actorLabel(event.actor)}</span>
          <code title={event.action}>{event.action}</code>
        </footer>
        {hasDetails && <details className="timeline-event__details"><summary>Технические детали</summary><pre>{JSON.stringify(details, null, 2)}</pre></details>}
      </article>
    </li>
  )
}

function validCategory(value: string | null): '' | TimelineCategory {
  return categoryOptions.some((option) => option.value === value) ? value as '' | TimelineCategory : ''
}

function validOffset(value: string | null) {
  const parsed = Number(value ?? 0)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : 0
}

function localDayBoundary(
  value: string,
  { exclusiveEnd = false }: { exclusiveEnd?: boolean } = {},
) {
  if (!value) return undefined
  const [year, month, day] = value.split('-').map(Number)
  const boundary = new Date(year, month - 1, day + (exclusiveEnd ? 1 : 0))
  return Number.isNaN(boundary.getTime()) ? undefined : boundary.toISOString()
}

export function UserTimeline({ userId }: { userId: string }) {
  const [params, setParams] = useSearchParams()
  const category = validCategory(params.get(PARAMS.category))
  const action = params.get(PARAMS.action) ?? ''
  const result = params.get(PARAMS.result) ?? ''
  const from = params.get(PARAMS.from) ?? ''
  const to = params.get(PARAMS.to) ?? ''
  const offset = validOffset(params.get(PARAMS.offset))
  const [actionDraft, setActionDraft] = useState(action)
  const [fromDraft, setFromDraft] = useState(from)
  const [toDraft, setToDraft] = useState(to)
  const [dateError, setDateError] = useState('')

  useEffect(() => { setActionDraft(action) }, [action])
  useEffect(() => { setFromDraft(from) }, [from])
  useEffect(() => { setToDraft(to) }, [to])

  const query = useQuery({
    queryKey: ['user-timeline', userId, category, action, result, from, to, offset],
    queryFn: () => usersApi.timeline(userId, {
      category: category || undefined,
      action: action || undefined,
      result: result || undefined,
      from: localDayBoundary(from),
      to: localDayBoundary(to, { exclusiveEnd: true }),
      limit: PAGE_SIZE,
      offset,
    }),
    placeholderData: (previous) => previous,
  })

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    if (key !== PARAMS.offset) next.delete(PARAMS.offset)
    setParams(next)
  }

  const applyAdvancedFilters = (event: FormEvent) => {
    event.preventDefault()
    if (fromDraft && toDraft && fromDraft > toDraft) {
      setDateError('Начальная дата не может быть позже конечной')
      return
    }
    setDateError('')
    const next = new URLSearchParams(params)
    for (const [key, value] of [[PARAMS.action, actionDraft.trim()], [PARAMS.from, fromDraft], [PARAMS.to, toDraft]]) {
      if (value) next.set(key, value); else next.delete(key)
    }
    next.delete(PARAMS.offset)
    setParams(next)
  }

  const clearFilters = () => {
    const next = new URLSearchParams(params)
    Object.values(PARAMS).forEach((key) => next.delete(key))
    setDateError('')
    setParams(next)
  }

  const filtered = Boolean(category || action || result || from || to)

  return (
    <section className="panel timeline-panel">
      <header className="panel__header">
        <div><h2>История пользователя</h2><p>Действия в боте, финансы, VPN, рефералы и административные изменения в одной хронологии</p></div>
        {query.isFetching && !query.isLoading && <span className="timeline-refresh" role="status">Обновляем…</span>}
      </header>
      <div className="timeline-filters">
        <div className="timeline-categories" role="group" aria-label="Категория событий">
          {categoryOptions.map((option) => {
            const Icon = option.icon
            return <button key={option.value || 'all'} type="button" className={category === option.value ? 'timeline-category timeline-category--active' : 'timeline-category'} aria-pressed={category === option.value} onClick={() => setFilter(PARAMS.category, option.value)}><Icon aria-hidden="true" />{option.label}</button>
          })}
        </div>
        <form className="timeline-filter-form" onSubmit={applyAdvancedFilters}>
          <label className="timeline-action-search"><Search aria-hidden="true" /><span className="sr-only">Действие</span><input aria-label="Действие" value={actionDraft} onChange={(event) => setActionDraft(event.target.value)} placeholder="Действие, например navigation.start" /></label>
          <label>Результат<select aria-label="Результат события" value={result} onChange={(event) => setFilter(PARAMS.result, event.target.value)}><option value="">Все</option><option value="handled">Обработано ботом</option><option value="processed">Обработано (старые события)</option><option value="completed">Завершено</option><option value="success">Успешно</option><option value="succeeded">Успешно (провайдер)</option><option value="credited">Зачислено</option><option value="accepted">Принято</option><option value="pending">Ожидает</option><option value="queued">В очереди</option><option value="running">Выполняется</option><option value="processing">Обрабатывается</option><option value="rejected">Отклонено</option><option value="invalid">Некорректно</option><option value="unavailable">Недоступно</option><option value="ignored">Пропущено</option><option value="failed">Ошибка</option><option value="exhausted">Попытки исчерпаны</option><option value="superseded">Заменено новой операцией</option><option value="blocked">Бот заблокирован</option><option value="deactivated">Деактивирован</option></select></label>
          <label>С даты<input aria-label="С даты" type="date" value={fromDraft} onChange={(event) => setFromDraft(event.target.value)} /></label>
          <label>По дату<input aria-label="По дату" type="date" value={toDraft} onChange={(event) => setToDraft(event.target.value)} /></label>
          <button className="button button--secondary button--small" type="submit"><CalendarDays /> Применить</button>
          {filtered && <button className="button button--ghost button--small" type="button" onClick={clearFilters}><RotateCcw /> Сбросить</button>}
        </form>
        {dateError && <p className="timeline-filter-error" role="alert">{dateError}</p>}
      </div>
      {query.isLoading ? <PageLoading label="Загружаем историю…" /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data!.items.length ? <><ol className="timeline-list">{query.data!.items.map((event) => <TimelineItem key={event.id} event={event} />)}</ol><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setFilter(PARAMS.offset, nextOffset ? String(nextOffset) : '')} /></> : <EmptyState title={filtered ? 'По фильтрам событий не найдено' : 'История пока пуста'} description={filtered ? 'Измените или сбросьте фильтры.' : 'События появятся после действий пользователя или системы.'} action={filtered ? <button type="button" className="button button--secondary" onClick={clearFilters}>Сбросить фильтры</button> : undefined} />}
    </section>
  )
}
