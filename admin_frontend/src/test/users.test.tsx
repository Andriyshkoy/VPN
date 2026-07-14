import { http, HttpResponse } from 'msw'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'
import { actor, userPage } from './fixtures'
import { renderApp } from './render'
import { server } from './server'

test('renders normalized users and keeps search in the URL-driven view', async () => {
  let search = ''
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json(actor)),
    http.get('/api/admin/v1/users', ({ request }) => {
      search = new URL(request.url).searchParams.get('q') ?? ''
      return HttpResponse.json(userPage)
    }),
  )
  renderApp(<App />, '/users')
  expect(await screen.findByRole('link', { name: '@alice' })).toHaveAttribute('href', '/users/42')
  const row = screen.getByRole('link', { name: '@alice' }).closest('tr')!
  expect(within(row).getByText(/125,50/)).toBeInTheDocument()
  expect(within(row).getByRole('link', { name: '@bob' })).toHaveAttribute('href', '/users/7')
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Поиск пользователей'), 'alice')
  await user.click(screen.getByRole('button', { name: 'Найти' }))
  await screen.findByRole('link', { name: '@alice' })
  expect(search).toBe('alice')
})

test('does not invent financial or referral data when RBAC projection omits it', async () => {
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ actor: { id: 2, username: 'support', role: 'support', permissions: ['users:read'] }, csrf_token: 'csrf' })),
    http.get('/api/admin/v1/users', () => HttpResponse.json({
      items: [{ id: 42, username: 'alice', delivery_status: 'active', config_counts: { total: 0, active: 0, suspended: 0 } }],
      total: 1,
      limit: 25,
      offset: 0,
    })),
  )
  renderApp(<App />, '/users?sort=-balance')
  expect(await screen.findByRole('link', { name: '@alice' })).toBeInTheDocument()
  expect(screen.queryByRole('columnheader', { name: 'Баланс' })).not.toBeInTheDocument()
  expect(screen.queryByRole('columnheader', { name: 'Пригласил' })).not.toBeInTheDocument()
  expect(screen.queryByRole('option', { name: /Баланс:/ })).not.toBeInTheDocument()
  expect(screen.queryByText(/0,00/)).not.toBeInTheDocument()
})

test('User 360 overview skips protected projections and operations query', async () => {
  let operationsCalled = false
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ actor: { id: 2, username: 'support', role: 'support', permissions: ['users:read'] }, csrf_token: 'csrf' })),
    http.get('/api/admin/v1/users/42', () => HttpResponse.json({
      identity: { id: 42, username: 'alice', delivery_status: 'active', created_at: '2026-07-10T10:00:00Z' },
      configs: { total: 0, active: 0, suspended: 0 },
    })),
    http.get('/api/admin/v1/users/42/vpn-operations', () => {
      operationsCalled = true
      return HttpResponse.json({ items: [], total: 0, limit: 5, offset: 0 })
    }),
  )
  renderApp(<App />, '/users/42')
  expect(await screen.findByRole('heading', { name: '@alice' })).toBeInTheDocument()
  expect(screen.queryByText('Баланс')).not.toBeInTheDocument()
  expect(screen.queryByText('Пополнения')).not.toBeInTheDocument()
  expect(screen.queryByText('Реферальные начисления')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Последние VPN-операции' })).not.toBeInTheDocument()
  expect(operationsCalled).toBe(false)
})
