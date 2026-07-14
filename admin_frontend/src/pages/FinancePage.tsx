import { useQuery } from '@tanstack/react-query'
import { CircleHelp } from 'lucide-react'
import { useMemo } from 'react'
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useSearchParams } from 'react-router-dom'
import { analyticsApi } from '../api'
import { PageHeader, StatCard } from '../components/PageHeader'
import { ErrorState, PageLoading } from '../components/States'
import { formatDate, formatMoney, formatNumber } from '../lib/format'
import { FinanceActivity } from './FinanceActivity'

function reportingDate(date: Date) {
  const parts = new Intl.DateTimeFormat('en', { timeZone: 'Asia/Novosibirsk', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date)
  const part = (type: 'year' | 'month' | 'day') => parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')}`
}

function shiftDate(value: string, days: number) {
  const [year, month, day] = value.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10)
}

export function FinancePage() {
  const [search, setSearch] = useSearchParams()
  const defaults = useMemo(() => { const to = reportingDate(new Date()); return { from: shiftDate(to, -29), to } }, [])
  const from = search.get('from') ?? defaults.from
  const to = search.get('to') ?? defaults.to
  const params = { from, to, granularity: 'day' }
  const overview = useQuery({ queryKey: ['finance-overview', params], queryFn: () => analyticsApi.overview(params) })
  const timeseries = useQuery({ queryKey: ['finance-timeseries', params], queryFn: () => analyticsApi.timeseries(params) })
  const setDate = (key: 'from' | 'to', value: string) => { const next = new URLSearchParams(search); next.set(key, value); setSearch(next) }
  if (overview.isLoading) return <PageLoading />
  if (overview.isError) return <ErrorState error={overview.error} retry={() => void overview.refetch()} />
  const data = overview.data!
  return (
    <>
      <PageHeader eyebrow="Аналитика" title="Финансы" description="Пополнения, потребление услуги и экономика сервиса." actions={<div className="date-range"><label>С <input aria-label="Начало периода" type="date" value={from} max={to} onChange={(event) => setDate('from', event.target.value)} /></label><label>По <input aria-label="Конец периода" type="date" value={to} min={from} onChange={(event) => setDate('to', event.target.value)} /></label></div>} />
      <section className="stats-grid stats-grid--four"><StatCard label="Пополнения" value={formatMoney(data.deposits)} hint={`${formatNumber(data.paying_users)} плательщиков`} tone="positive" /><StatCard label="Выручка от услуги" value={formatMoney(data.service_revenue)} hint={`Создание конфигов ${formatMoney(data.config_fees)}`} tone="positive" /><StatCard label="Реферальные расходы" value={formatMoney(data.referral_costs)} hint={`Возвраты ${formatMoney(data.refunds)}`} /><StatCard label="Оценочная маржа" value={formatMoney(data.estimated_margin)} hint={`Инфраструктура ${formatMoney(data.infrastructure_costs)}`} tone={Number(data.estimated_margin) >= 0 ? 'positive' : 'danger'} /></section>
      <div className="content-grid content-grid--finance"><section className="panel chart-panel"><header className="panel__header"><div><h2>Динамика финансов</h2><p>{formatDate(from)} — {formatDate(to)}</p></div></header>{timeseries.isLoading ? <PageLoading /> : timeseries.isError ? <ErrorState error={timeseries.error} /> : timeseries.data!.length ? <div className="chart-wrap" role="img" aria-label="График пополнений, выручки и реферальных расходов"><ResponsiveContainer width="100%" height="100%"><AreaChart data={timeseries.data!} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}><defs><linearGradient id="deposits" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#6457d9" stopOpacity={0.3}/><stop offset="95%" stopColor="#6457d9" stopOpacity={0}/></linearGradient><linearGradient id="revenue" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#1aa981" stopOpacity={0.3}/><stop offset="95%" stopColor="#1aa981" stopOpacity={0}/></linearGradient></defs><CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e8e9f0"/><XAxis dataKey="date" tickFormatter={(value: string) => new Date(value).toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' })} axisLine={false} tickLine={false} fontSize={12}/><YAxis tickFormatter={(value: number) => `${value} ₽`} axisLine={false} tickLine={false} fontSize={12}/><Tooltip formatter={(value) => formatMoney(Number(value))} labelFormatter={(value) => formatDate(String(value))}/><Legend /><Area type="monotone" dataKey="deposits" name="Пополнения" stroke="#6457d9" fill="url(#deposits)" strokeWidth={2}/><Area type="monotone" dataKey="service_revenue" name="Выручка" stroke="#1aa981" fill="url(#revenue)" strokeWidth={2}/><Area type="monotone" dataKey="referral_costs" name="Реферальные расходы" stroke="#e59037" fill="transparent" strokeWidth={2}/></AreaChart></ResponsiveContainer></div> : <div className="panel__empty">Нет данных за выбранный период</div>}</section><aside className="stack"><section className="panel liability-card"><header className="panel__header"><div><h2>Обязательства</h2><p>Остатки на балансах</p></div></header><strong>{formatMoney(data.balance_liability)}</strong><p>Средства пользователей, которые ещё не стали выручкой сервиса.</p></section><section className="panel definitions"><header className="panel__header"><div><h2><CircleHelp size={18} /> Как считаем</h2></div></header><dl><div><dt>Пополнения</dt><dd>Подтверждённые платежи</dd></div><div><dt>Выручка</dt><dd>Списания за услугу и создание конфигов</dd></div><div><dt>Маржа</dt><dd>Выручка минус рефералы, возвраты и серверы</dd></div><div><dt>Средний платёж</dt><dd>{formatMoney(data.average_payment)}</dd></div></dl></section></aside></div>
      <FinanceActivity from={from} to={to} />
    </>
  )
}
