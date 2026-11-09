import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { subscriptionsApi, planLabel, serviceLabel, type SubscriptionView, type SubscriptionMigrationPreview } from '../api/subscriptions'
import { useAuth } from '../auth/AuthProvider'
import { DataTable, type Column } from '../components/DataTable'
import { PageHeader, StatCard } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateTime, formatMoney, formatNumber } from '../lib/format'
import { withParam } from '../lib/search'

export function SubscriptionsPage() {
  const { user } = useAuth()
  const canReadUsers = user?.permissions.includes('users:read') ?? false
  const canReadBalance = user?.permissions.includes('balance:read') ?? false
  const [params, setParams] = useSearchParams()
  const [showPreview, setShowPreview] = useState(false)
  const parsedOffset = Number(params.get('offset') ?? 0)
  const offset = Number.isInteger(parsedOffset) && parsedOffset >= 0 ? parsedOffset : 0
  const filters = { plan_code: params.get('plan_code') || undefined, status: params.get('status') || undefined, offset, limit: 25 }
  const plans = useQuery({ queryKey: ['subscription-plans'], queryFn: subscriptionsApi.plans })
  const subscriptions = useQuery({ queryKey: ['subscriptions', filters], queryFn: () => subscriptionsApi.list(filters), placeholderData: (previous) => previous })
  const migration = useQuery({ queryKey: ['subscription-migration-preview'], queryFn: subscriptionsApi.migrationPreview, enabled: showPreview })
  const columns: Column<SubscriptionView>[] = [
    { key: 'user', header: 'Аккаунт', render: (row) => canReadUsers ? <Link className="primary-link" to={`/users/${row.user_id}/subscription`}>Пользователь #{row.user_id}</Link> : `#${row.user_id}` },
    { key: 'plan', header: 'Тариф', render: (row) => <strong>{planLabel(row.plan_code)}</strong> },
    { key: 'price', header: 'За час', render: (row) => formatMoney(row.hourly_rate_rub) },
    { key: 'status', header: 'Доступ', render: (row) => <StatusBadge value={row.status} /> },
    { key: 'billing', header: 'Списания', render: (row) => row.billing_paused ? <StatusBadge value="disabled_feature" label="Выключены" /> : canReadBalance && row.unlimited_balance ? <span aria-label="Бесконечный баланс">∞</span> : formatDateTime(row.next_charge_at) },
  ]
  const filter = (key: string, value: string) => setParams(withParam(params, key, value, true))
  return <>
    <PageHeader eyebrow="Продукт" title="Подписки" description="Почасовой тариф принадлежит аккаунту. Число технических профилей не увеличивает стоимость подписки." />
    {plans.isLoading ? <PageLoading /> : plans.isError ? <ErrorState error={plans.error} retry={() => void plans.refetch()} /> : <section className="content-grid content-grid--two plan-grid" aria-label="Тарифы">{plans.data!.items.map((plan) => <article className="panel plan-card" key={plan.code}><header><span className="eyebrow">{planLabel(plan.code)}</span><h2>{plan.name}</h2><strong>≈ {formatMoney(plan.monthly_price_rub)} <small>/ 30 дней</small></strong></header><p><strong>{formatMoney(plan.hourly_rate_rub)} / час</strong> · {formatMoney(plan.monthly_estimate_rub)} за 720 часов</p><p>{plan.services.map(serviceLabel).join(' + ')}</p><p>{plan.max_sessions === null ? 'Неограниченное количество устройств' : `${plan.max_sessions} одновременная сессия Hysteria`}</p><small>{plan.code === 'basic' ? 'Один активный клиент, без привязки к устройству.' : 'Оба протокола включены в одну подписку.'}</small></article>)}</section>}
    <div className="product-notice"><strong>Выпуск и перевыпуск профилей — 0 ₽</strong><p>Деньги списываются почасово за аккаунт. Сумма за 30 дней — ориентир, авансовой оплаты месяца нет. Режим ∞ назначается администратором отдельно и сохраняет обычный кошелёк.</p></div>
    <section className="filter-bar"><label className="inline-select">Тариф <select aria-label="Фильтр тарифа" value={filters.plan_code ?? ''} onChange={(event) => filter('plan_code', event.target.value)}><option value="">Все</option><option value="basic">Basic</option><option value="plus">Plus</option></select></label><label className="inline-select">Доступ <select aria-label="Фильтр подписки" value={filters.status ?? ''} onChange={(event) => filter('status', event.target.value)}><option value="">Все</option><option value="active">Активные</option><option value="suspended">Приостановленные</option><option value="expired">Истёкшие</option><option value="pending">Ожидающие</option></select></label></section>
    <section className="panel"><header className="panel__header"><div><h2>Подписки аккаунтов</h2><p>{subscriptions.data ? `${formatNumber(subscriptions.data.total)} записей` : 'Загрузка…'}</p></div></header>{subscriptions.isLoading ? <PageLoading /> : subscriptions.isError ? <ErrorState error={subscriptions.error} retry={() => void subscriptions.refetch()} /> : subscriptions.data!.items.length ? <><DataTable label="Подписки аккаунтов" columns={columns} rows={subscriptions.data!.items} rowKey={(row) => row.id} /><Pagination offset={subscriptions.data!.offset} limit={subscriptions.data!.limit} total={subscriptions.data!.total} onChange={(next) => setParams(withParam(params, 'offset', next ? String(next) : ''))} /></> : <EmptyState title="Подписок пока нет" description="Существующие кошельки и история операций сохранены. План переноса можно проверить ниже." />}</section>
    <section className="panel migration-panel"><header className="panel__header"><div><h2>Предпросмотр миграции</h2><p>1 активный профиль → Basic; 2 и больше → Plus. Полностью приостановленные аккаунты остаются выключенными.</p></div><button type="button" className="button button--secondary" disabled={migration.isFetching} onClick={() => { setShowPreview(true); if (showPreview) void migration.refetch() }}>{migration.isFetching ? 'Считаем…' : showPreview ? 'Обновить расчёт' : 'Рассчитать без изменений'}</button></header>{showPreview && (migration.isLoading ? <PageLoading /> : migration.isError ? <ErrorState error={migration.error} retry={() => void migration.refetch()} /> : migration.data ? <MigrationPreview preview={migration.data} canReadUsers={canReadUsers} /> : null)}</section>
  </>
}

function MigrationPreview({ preview, canReadUsers }: { preview: SubscriptionMigrationPreview; canReadUsers: boolean }) {
  const columns: Column<SubscriptionMigrationPreview['items'][number]>[] = [
    { key: 'user', header: 'Аккаунт', render: (row) => canReadUsers ? <Link to={`/users/${row.user_id}/subscription`}>#{row.user_id}</Link> : `#${row.user_id}` },
    { key: 'configs', header: 'Активные профили', render: (row) => row.active_configs },
    { key: 'plan', header: 'План', render: (row) => row.plan_code ? planLabel(row.plan_code) : 'Без активной подписки' },
    { key: 'status', header: 'Доступ', render: (row) => <StatusBadge value={row.status} label={row.status === 'unchanged' ? 'Без изменений' : undefined} /> },
    { key: 'reason', header: 'Основание', render: (row) => row.reason },
  ]
  const safe = preview.read_only && !preview.billing_enabled && preview.balance_changes === 0 && preview.ledger_changes === 0 && preview.unlimited_balance_changes === 0
  return <div className="migration-preview"><div className={safe ? 'product-notice' : 'form-error'} role="status"><strong>{safe ? 'Только расчёт. Списания выключены.' : 'Проверьте настройки миграции перед применением.'}</strong><p>Изменений кошельков: {preview.balance_changes}; записей в ledger: {preview.ledger_changes}; изменений режима ∞: {preview.unlimited_balance_changes}. В этой панели перенос не запускается.</p></div><section className="stats-grid stats-grid--four"><StatCard label="Basic" value={preview.counts.basic} /><StatCard label="Plus" value={preview.counts.plus} /><StatCard label="Останутся выключенными" value={preview.counts.suspended} /><StatCard label="Без изменений" value={preview.counts.unchanged} hint={`Всего: ${preview.counts.total}`} /></section>{preview.items.length > 0 ? <DataTable label="Расчёт миграции" columns={columns} rows={preview.items} rowKey={(row) => row.user_id} /> : <EmptyState title="Нет аккаунтов для переноса" />}</div>
}
