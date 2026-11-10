import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { subscriptionsApi, planLabel, serviceLabel, type PlanCode, type SubscriptionView } from '../api/subscriptions'
import { useAuth } from '../auth/AuthProvider'
import { formatDateTime, formatMoney } from '../lib/format'
import { Modal } from './Modal'
import { ErrorState, PageLoading } from './States'
import { StatusBadge } from './StatusBadge'

export function UserSubscription({ userId, balance }: { userId: string; balance?: string }) {
  const { user } = useAuth()
  const [editing, setEditing] = useState(false)
  const canReadBalance = user?.permissions.includes('balance:read') ?? false
  const query = useQuery({ queryKey: ['user-subscription', userId], queryFn: () => subscriptionsApi.user(userId) })
  if (query.isLoading) return <PageLoading />
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />
  const subscription = query.data!.subscription
  return <section className="panel detail-list"><header className="panel__header"><div><h2>Подписка аккаунта</h2><p>Один почасовой тариф на аккаунт. Выпуск и перевыпуск профилей — бесплатно.</p></div>{user?.permissions.includes('subscriptions:write') && <button type="button" className="button button--secondary button--small" onClick={() => setEditing(true)}>{subscription ? 'Изменить тариф' : 'Назначить тариф'}</button>}</header>
    {subscription ? <><dl><div><dt>Тариф</dt><dd>{planLabel(subscription.plan_code)} · {formatMoney(subscription.hourly_rate_rub)} / час</dd></div><div><dt>Статус</dt><dd><StatusBadge value={subscription.status} /></dd></div><div><dt>Протоколы</dt><dd>{subscription.services.map(serviceLabel).join(' + ')}</dd></div><div><dt>Подключения</dt><dd>{subscription.max_sessions === null ? 'Без ограничения устройств' : `${subscription.max_sessions} одновременная сессия Hysteria`}</dd></div><div><dt>Оценка за 30 дней</dt><dd>{formatMoney(subscription.monthly_estimate_rub)} при непрерывном доступе</dd></div>{canReadBalance && typeof subscription.unlimited_balance === 'boolean' && <div><dt>Запас кошелька</dt><dd>{subscription.unlimited_balance ? 'Режим ∞ — без списаний' : walletRunway(balance, subscription.hourly_rate_rub)}</dd></div>}<div><dt>Списания</dt><dd>{subscription.billing_paused ? 'Выключены' : canReadBalance && subscription.unlimited_balance ? 'Режим ∞' : `Следующее: ${formatDateTime(subscription.next_charge_at)}`}</dd></div></dl>{subscription.accesses.length > 0 && <div className="subscription-accesses">{subscription.accesses.map((access) => <div key={`${access.service_id}-${access.config_id}`}><span>{serviceLabel(access.service_id)} · {access.config_id === null ? 'профиль ещё не выпущен' : `профиль #${access.config_id}`}</span><StatusBadge value={access.status} /></div>)}</div>}</> : <div className="panel__empty">Подписка ещё не назначена. <Link to="/subscriptions">Открыть тарифы и план миграции</Link></div>}
    {editing && <PlanChangeDialog userId={userId} subscription={subscription} onClose={() => setEditing(false)} />}
  </section>
}

function PlanChangeDialog({ userId, subscription, onClose }: { userId: string; subscription: SubscriptionView | null; onClose: () => void }) {
  const plans = useQuery({ queryKey: ['subscription-plans'], queryFn: subscriptionsApi.plans })
  const [code, setCode] = useState<PlanCode>(subscription?.plan_code ?? 'basic')
  const [reason, setReason] = useState('')
  const queryClient = useQueryClient()
  const intent = useRef<{ fingerprint: string; key: string } | null>(null)
  const mutation = useMutation({ mutationFn: () => {
    const input = { plan_code: code, reason: reason.trim(), expected_version: subscription?.version }
    const fingerprint = JSON.stringify(input)
    if (intent.current?.fingerprint !== fingerprint) intent.current = { fingerprint, key: crypto.randomUUID() }
    return subscriptionsApi.changePlan(userId, input, intent.current.key)
  }, onSuccess: async () => { await Promise.all([['user-subscription', userId], ['subscriptions'], ['audit']].map((queryKey) => queryClient.invalidateQueries({ queryKey }))); onClose() } })
  const submit = (event: FormEvent) => { event.preventDefault(); if (reason.trim().length >= 3 && !mutation.isPending && plans.data?.items.some((item) => item.code === code)) mutation.mutate() }
  return <Modal open title="Тариф подписки" description="Тариф изменится сразу. Почасовая стоимость считается за аккаунт; эта операция не списывает деньги." onClose={() => { if (!mutation.isPending) onClose() }}><form className="form-stack" onSubmit={submit}>
    {plans.isLoading ? <PageLoading /> : plans.isError ? <ErrorState error={plans.error} retry={() => void plans.refetch()} /> : <label className="field"><span>Тариф</span><select value={code} onChange={(event) => setCode(event.target.value as PlanCode)}>{plans.data!.items.map((plan) => <option key={plan.code} value={plan.code}>{plan.name} — {formatMoney(plan.hourly_rate_rub)} / час</option>)}</select></label>}
    <div className="product-notice"><p>{code === 'basic' ? 'Hysteria 2: одна одновременно активная клиентская сессия. Привязки к конкретному устройству нет.' : 'Hysteria 2 + VLESS / REALITY: неограниченное количество устройств.'}</p><p>Назначение тарифа не включает общие списания и не меняет режим ∞.</p></div>
    <label className="field"><span>Причина смены тарифа</span><textarea rows={3} required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
    {mutation.isError && <div className="form-error" role="alert">{mutation.error.message}</div>}
    <div className="form-actions"><button type="button" className="button button--secondary" disabled={mutation.isPending} onClick={onClose}>Отмена</button><button type="submit" className="button button--primary" disabled={!plans.data || reason.trim().length < 3 || mutation.isPending}>{mutation.isPending ? 'Сохраняем…' : 'Сохранить тариф'}</button></div>
  </form></Modal>
}

function walletRunway(balance: string | undefined, hourlyRate: string): string {
  const toCents = (value: string) => {
    const match = value.match(/^(\d+)(?:\.(\d{1,2}))?$/)
    return match ? BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0')) : null
  }
  const wallet = balance === undefined ? null : toCents(balance)
  const rate = toCents(hourlyRate)
  if (wallet === null || rate === null || rate <= 0n) return 'Недоступен'
  const hours = wallet / rate
  return hours < 1n ? 'Меньше часа при включённых списаниях' : `${hours.toLocaleString('ru-RU')} ч (≈ ${hours / 24n} дн.) при включённых списаниях`
}
