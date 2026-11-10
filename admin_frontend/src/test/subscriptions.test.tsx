import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import App from '../App'
import { adaptFinanceOverview, adaptUserDetail, adaptUserListItem } from '../api/adapters'
import { UnlimitedBalanceDialog } from '../components/UnlimitedBalanceDialog'
import { UserSubscription } from '../components/UserSubscription'
import { FinanceActivity } from '../pages/FinanceActivity'
import { actor } from './fixtures'
import { renderApp, renderUnit } from './render'
import { server } from './server'

const plans = [{ code: 'basic', name: 'Basic', hourly_rate_rub: '0.07', monthly_price_rub: '50.00', monthly_estimate_rub: '50.40', currency: 'RUB', services: ['hysteria'], max_sessions: 1 }, { code: 'plus', name: 'Plus', hourly_rate_rub: '0.14', monthly_price_rub: '100.00', monthly_estimate_rub: '100.80', currency: 'RUB', services: ['hysteria', 'vless-reality'], max_sessions: null }]
const subscription = { id: 9, user_id: 42, plan_code: 'plus', status: 'active', hourly_rate_rub: '0.14', monthly_price_rub: '100.00', monthly_estimate_rub: '100.80', version: 1, max_sessions: null, billing_paused: true, unlimited_balance: false, services: ['hysteria', 'vless-reality'], accesses: [] }
function admin(permissions = [...actor.actor.permissions, 'subscriptions:read', 'subscriptions:write']) {
  server.use(http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ ...actor, actor: { ...actor.actor, permissions } })))
}

test('shows monthly plans without fractional prices and runs a read-only migration preview without any automatic mutation', async () => {
  admin()
  let reads = 0
  const writes: string[] = []
  server.use(
    http.get('/api/admin/v1/subscriptions/plans', () => HttpResponse.json({ items: plans })),
    http.get('/api/admin/v1/subscriptions', () => HttpResponse.json({ items: [subscription], total: 1, limit: 25, offset: 0 })),
    http.get('/api/admin/v1/subscriptions/migration/preview', () => { reads++; return HttpResponse.json({ billing_enabled: false, read_only: true, counts: { basic: 1, plus: 1, suspended: 1, unchanged: 0, total: 3 }, items: [{ user_id: 42, active_configs: 2, plan_code: 'plus', status: 'active', reason: '2 активных профиля' }], balance_changes: 0, ledger_changes: 0, unlimited_balance_changes: 0 }) }),
    http.patch('/api/admin/v1/*', ({ request }) => { writes.push(request.url); return HttpResponse.json({}) }),
    http.post('/api/admin/v1/*', ({ request }) => { writes.push(request.url); return HttpResponse.json({}) }),
  )
  renderApp(<App />, '/subscriptions')
  expect(await screen.findByRole('heading', { name: 'Подписки' })).toBeInTheDocument()
  expect(await screen.findByText(/Для простых задач, например Instagram/)).toBeInTheDocument()
  expect(screen.getByText(/Рекомендуем для нескольких устройств/)).toBeInTheDocument()
  expect(screen.getByText(/выберите второй в Hiddify вручную/)).toBeInTheDocument()
  const cards = screen.getByRole('region', { name: 'Тарифы' })
  expect(cards).toHaveTextContent(/50\s*₽\s*в месяц/)
  expect(cards).toHaveTextContent(/100\s*₽\s*в месяц/)
  expect(document.body).not.toHaveTextContent(/0,07|0,14|50,40|100,80|почас|720 часов|за час/i)
  expect(screen.getByText('Выключены')).toBeInTheDocument()
  expect(reads).toBe(0)
  await userEvent.click(screen.getByRole('button', { name: 'Рассчитать без изменений' }))
  expect(await screen.findByText('Только расчёт. Списания выключены.')).toBeInTheDocument()
  expect(screen.getByRole('table', { name: 'Расчёт миграции' })).toBeInTheDocument()
  expect(writes).toEqual([])
})

test('requires subscription read permission and does not fetch protected product data', async () => {
  admin(['users:read'])
  const call = vi.fn()
  server.use(http.get('/api/admin/v1/subscriptions/plans', () => { call(); return HttpResponse.json({ items: plans }) }))
  renderApp(<App />, '/subscriptions')
  expect(await screen.findByText(/Нет доступа к разделу/)).toBeInTheDocument()
  expect(call).not.toHaveBeenCalled()
  expect(screen.queryByRole('link', { name: 'Подписки' })).not.toBeInTheDocument()
})

test('infinite mode is an explicit audited toggle; it never submits wallet changes and reuses retry identity', async () => {
  const keys: string[] = []
  let requests = 0
  server.use(http.patch('/api/admin/v1/users/42/unlimited-balance', async ({ request }) => {
    requests++
    keys.push(request.headers.get('idempotency-key') ?? '')
    expect(await request.json()).toEqual({ enabled: true, expected_enabled: false, reason: 'Тестовый аккаунт' })
    if (requests === 1) return HttpResponse.json({ detail: 'Повторите запрос' }, { status: 504 })
    return HttpResponse.json({ user_id: 42, unlimited_balance: true, balance: '125.50', balance_display: '∞', replayed: true })
  }))
  const close = vi.fn()
  renderUnit(<UnlimitedBalanceDialog user={{ id: 42, balance: '125.50', unlimited_balance: false }} onClose={close} />)
  const user = userEvent.setup()
  const save = screen.getByRole('button', { name: 'Включить режим ∞' })
  expect(save).toBeDisabled()
  expect(requests).toBe(0)
  await user.type(screen.getByLabelText('Причина изменения'), 'Тестовый аккаунт')
  expect(save).toBeDisabled()
  await user.click(screen.getByRole('checkbox'))
  await user.click(save)
  expect(await screen.findByRole('alert')).toHaveTextContent('Повторите запрос')
  await user.click(save)
  await waitFor(() => expect(close).toHaveBeenCalled())
  expect(keys[0]).not.toBe('')
  expect(keys[1]).toBe(keys[0])
})

test('disabling infinite mode restores finite wallet and respects the expected flag', async () => {
  const close = vi.fn()
  server.use(http.patch('/api/admin/v1/users/42/unlimited-balance', async ({ request }) => {
    expect(await request.json()).toEqual({ enabled: false, expected_enabled: true, reason: 'Вернуть оплату' })
    return HttpResponse.json({ user_id: 42, unlimited_balance: false, balance: '125.50', balance_display: '125.50', replayed: false })
  }))
  renderUnit(<UnlimitedBalanceDialog user={{ id: 42, balance: '125.50', unlimited_balance: true }} onClose={close} />)
  const user = userEvent.setup()
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText(/Обычный баланс: 125,50/)).toBeInTheDocument()
  await user.type(screen.getByLabelText('Причина изменения'), 'Вернуть оплату')
  await user.click(screen.getByRole('checkbox'))
  await user.click(screen.getByRole('button', { name: 'Выключить режим ∞' }))
  await waitFor(() => expect(close).toHaveBeenCalled())
})

test('adapters keep finite money separate from infinite mode and preserve financial permission boundaries', () => {
  expect(adaptUserListItem({ id: 42, balance: '125.50', unlimited_balance: true })).toMatchObject({ balance: '125.50', unlimited_balance: true })
  expect(adaptUserDetail({ identity: { id: 42 }, finance: { balance: '125.50', unlimited_balance: true } })).toMatchObject({ balance: '125.50', unlimited_balance: true })
  expect(adaptUserDetail({ identity: { id: 42 }, unlimited_balance: true })).not.toHaveProperty('balance')
  expect(adaptUserDetail({ identity: { id: 42 }, unlimited_balance: true })).not.toHaveProperty('unlimited_balance')
})

test('plan changes are immediate, guarded by version and never toggle infinite mode or charge a wallet', async () => {
  admin()
  const calls: unknown[] = []
  server.use(
    http.get('/api/admin/v1/users/42/subscription', () => HttpResponse.json({ subscription })),
    http.get('/api/admin/v1/subscriptions/plans', () => HttpResponse.json({ items: plans })),
    http.patch('/api/admin/v1/users/42/subscription', async ({ request }) => {
      calls.push(await request.json())
      expect(request.headers.get('idempotency-key')).toBeTruthy()
      expect(request.headers.get('x-csrf-token')).toBe(actor.csrf_token)
      return HttpResponse.json({ ...subscription, plan_code: 'basic', version: 2 })
    }),
  )
  renderApp(<UserSubscription userId="42" balance="125.50" />)
  await userEvent.click(await screen.findByRole('button', { name: 'Изменить тариф' }))
  expect(screen.getByText(/Примерно 37 дн/)).toBeInTheDocument()
  expect(screen.getAllByText(/100\s*₽.*в месяц/).length).toBeGreaterThan(0)
  expect(document.body).not.toHaveTextContent(/0,07|0,14|50,40|100,80|почас|\/ час/i)
  expect(screen.getByText(/Тариф изменится сразу/)).toBeInTheDocument()
  await userEvent.selectOptions(await screen.findByLabelText('Тариф'), 'basic')
  expect(screen.getByRole('option', { name: /Basic.*50\s*₽ в месяц/ })).toBeInTheDocument()
  expect(screen.getByText(/Для простых задач, например Instagram/)).toBeInTheDocument()
  await userEvent.type(screen.getByLabelText('Причина смены тарифа'), 'По просьбе пользователя')
  await userEvent.click(screen.getByRole('button', { name: 'Сохранить тариф' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(calls).toEqual([{ plan_code: 'basic', reason: 'По просьбе пользователя', expected_version: 1 }])
})

test('support can read a subscription without gaining plan or infinite balance controls', async () => {
  admin(['subscriptions:read'])
  server.use(http.get('/api/admin/v1/users/42/subscription', () => HttpResponse.json({ subscription: { ...subscription, billing_paused: false, unlimited_balance: true } })))
  renderApp(<UserSubscription userId="42" />)
  expect(await screen.findByRole('heading', { name: 'Подписка аккаунта' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Изменить тариф' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Режим ∞/ })).not.toBeInTheDocument()
  expect(screen.queryByText('Запас кошелька')).not.toBeInTheDocument()
  expect(screen.queryByText('Режим ∞ — без списаний')).not.toBeInTheDocument()
  expect(screen.queryByText('Режим ∞')).not.toBeInTheDocument()
})

test('subscription forecast remains separate from historical revenue and finite wallet liability', () => {
  const result = adaptFinanceOverview({ finance: { cash_in: '500.00', recognized_revenue: '120.00', wallet_liability: '125.50' }, subscription_forecast: { scope: 'business', billing_paused: true, active_accounts: 2, paid_active_accounts: 1, unlimited_active_accounts: 1, hourly_rate_rub: '0.14', monthly_estimate_rub: '100.80', monthly_plan_total_rub: '100.00', currency: 'RUB' } })
  expect(result).toMatchObject({ deposits: '500.00', service_revenue: '120.00', balance_liability: '125.50', subscription_forecast: { billing_paused: true, active_accounts: 2, paid_active_accounts: 1, unlimited_active_accounts: 1, hourly_rate_rub: '0.14', monthly_estimate_rub: '100.80', monthly_plan_total_rub: '100.00' } })
  expect(adaptFinanceOverview({ finance: {} }).subscription_forecast).toBeUndefined()
})


test('finance shows nominal monthly plan total while retaining exact historical money', async () => {
  admin()
  server.use(
    http.get('/api/admin/v1/analytics/overview', () => HttpResponse.json({
      finance: { cash_in: '500.00', recognized_revenue: '120.07', wallet_liability: '125.50' }, users: {},
      subscription_forecast: { scope: 'business', billing_paused: true, active_accounts: 3, paid_active_accounts: 2, unlimited_active_accounts: 1, hourly_rate_rub: '0.21', monthly_estimate_rub: '151.20', monthly_plan_total_rub: '150.00', currency: 'RUB' },
    })),
    http.get('/api/admin/v1/analytics/finance/timeseries', () => HttpResponse.json({ items: [] })),
    http.get('/api/admin/v1/finance/ledger', () => HttpResponse.json({ items: [], total: 0, offset: 0, limit: 25 })),
  )
  renderApp(<App />, '/finance')
  expect(await screen.findByText('По тарифам в месяц')).toBeInTheDocument()
  const forecast = screen.getByRole('heading', { name: 'Оценка по активным подпискам' }).closest('section')!
  expect(forecast).toHaveTextContent(/150\s*₽/)
  expect(forecast).toHaveTextContent(/50 \/ 100 ₽ в месяц/)
  expect(forecast).not.toHaveTextContent(/0,21|151,20|почас|720/i)
  expect(screen.getByText(/125,50/)).toBeInTheDocument()
  expect(screen.getByText(/120,07/)).toBeInTheDocument()
})


test('historical billing preserves exact debit totals without a per-config tariff column', async () => {
  admin()
  server.use(http.get('/api/admin/v1/finance/billing-runs', () => HttpResponse.json({ items: [{ id: 1, status: 'completed', period_start: '2026-11-10T08:00:00Z', period_end: '2026-11-10T09:00:00Z', cost_per_config: '0.07', charged_users: 1, total_amount: '0.14' }], total: 1, limit: 25, offset: 0 })))
  renderApp(<FinanceActivity from="2026-11-10" to="2026-11-10" />, '/finance?finance_view=billing')
  const table = await screen.findByRole('table', { name: 'Биллинговые прогоны' })
  expect(within(table).queryByRole('columnheader', { name: 'Тариф за конфиг' })).not.toBeInTheDocument()
  expect(table).not.toHaveTextContent('0,07')
  expect(within(table).getByRole('columnheader', { name: 'Списано' })).toBeInTheDocument()
  expect(table).toHaveTextContent(/0,14/)
})
