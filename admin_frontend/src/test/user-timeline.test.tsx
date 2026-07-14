import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'
import { usersApi } from '../api'
import { actor } from './fixtures'
import { renderApp } from './render'
import { server } from './server'

const userDetail = {
  identity: { id: 42, tg_id: 123456, username: 'alice', delivery_status: 'active', created_at: '2026-07-10T10:00:00Z' },
  finance: { balance: '125.50', provider_deposits: '500.00', service_charges: '20.00', config_fees: '5.00' },
  configs: { total: 2, active: 1, suspended: 1 },
  referral: { referrer: null, total_earned: '10.00' },
}

const timelineItem = {
  id: 'bot:100',
  source: 'bot',
  category: 'bot',
  action: 'finance.balance_view',
  result: 'handled',
  occurred_at: '2026-07-14T10:30:00Z',
  title: 'Открыт раздел баланса',
  description: 'Пользователь нажал кнопку «Баланс».',
  actor: { type: 'user', id: 42, label: '@alice' },
  metadata: { section: 'balance', bot_token: 'super-secret', nested: { password: 'also-secret' } },
}

test('timeline API follows the filtering contract and adapts its actor', async () => {
  let requested: URL | undefined
  server.use(http.get('/api/admin/v1/users/42/timeline', ({ request }) => {
    requested = new URL(request.url)
    return HttpResponse.json({ items: [timelineItem], total: 1, limit: 25, offset: 0 })
  }))

  const page = await usersApi.timeline('42', {
    category: 'bot',
    action: 'finance.balance_view',
    result: 'handled',
    from: '2026-07-01',
    to: '2026-07-14',
    limit: 25,
    offset: 0,
  })

  expect(requested?.pathname).toBe('/api/admin/v1/users/42/timeline')
  expect(Object.fromEntries(requested!.searchParams)).toMatchObject({
    category: 'bot',
    action: 'finance.balance_view',
    result: 'handled',
    from: '2026-07-01',
    to: '2026-07-14',
    limit: '25',
    offset: '0',
  })
  expect(page.items[0]).toMatchObject({
    id: 'bot:100',
    source: 'bot',
    category: 'bot',
    actor: { type: 'user', id: 42, label: '@alice' },
  })
})

test('renders the unified history, protects sensitive details and applies filters', async () => {
  const requests: URL[] = []
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json(actor)),
    http.get('/api/admin/v1/users/42', () => HttpResponse.json(userDetail)),
    http.get('/api/admin/v1/users/42/timeline', ({ request }) => {
      requests.push(new URL(request.url))
      return HttpResponse.json({ items: [timelineItem], total: 30, limit: 25, offset: Number(new URL(request.url).searchParams.get('offset') ?? 0) })
    }),
  )

  renderApp(<App />, '/users/42/history')
  expect(await screen.findByRole('heading', { name: 'История пользователя' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'История' })).toHaveAttribute('href', '/users/42/history')
  expect(await screen.findByText('Открыт раздел баланса')).toBeInTheDocument()
  expect(screen.getByText('Telegram-бот · Просмотрен баланс')).toBeInTheDocument()
  expect(screen.getAllByText('@alice').length).toBeGreaterThan(0)
  expect(screen.queryByText('super-secret')).not.toBeInTheDocument()

  const user = userEvent.setup()
  await user.click(screen.getByText('Технические детали'))
  expect(screen.getAllByText(/\[скрыто\]/).length).toBeGreaterThan(0)
  expect(screen.queryByText(/super-secret|also-secret/)).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Бот' }))
  await user.selectOptions(screen.getByLabelText('Результат события'), 'failed')
  await user.type(screen.getByLabelText('Действие'), 'navigation.start')
  await user.type(screen.getByLabelText('С даты'), '2026-07-01')
  await user.type(screen.getByLabelText('По дату'), '2026-07-14')
  await user.click(screen.getByRole('button', { name: /Применить/ }))

  await waitFor(() => expect(requests.at(-1)?.searchParams.get('action')).toBe('navigation.start'))
  const filtered = requests.at(-1)!
  expect(filtered.searchParams.get('category')).toBe('bot')
  expect(filtered.searchParams.get('result')).toBe('failed')
  expect(filtered.searchParams.get('from')).toBe(new Date(2026, 6, 1).toISOString())
  expect(filtered.searchParams.get('to')).toBe(new Date(2026, 6, 15).toISOString())

  await user.click(screen.getByRole('button', { name: 'Следующая страница' }))
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('offset')).toBe('25'))
})

test('does not request user history without audit:read', async () => {
  let timelineCalled = false
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ actor: { id: 2, username: 'support', role: 'support', permissions: ['users:read'] }, csrf_token: 'csrf' })),
    http.get('/api/admin/v1/users/42', () => HttpResponse.json({ identity: userDetail.identity, configs: userDetail.configs })),
    http.get('/api/admin/v1/users/42/timeline', () => {
      timelineCalled = true
      return HttpResponse.json({ items: [], total: 0, limit: 25, offset: 0 })
    }),
  )

  renderApp(<App />, '/users/42/history')
  expect(await screen.findByText('Нет доступа к разделу')).toBeInTheDocument()
  expect(timelineCalled).toBe(false)
})
