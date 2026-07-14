import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import App from '../App'
import { actor } from './fixtures'
import { renderApp } from './render'
import { server } from './server'

test.each([
  ['/configs', '/api/admin/v1/configs', { items: [], total: 0, limit: 25, offset: 0 }, 'Конфигурации'],
  ['/operations', '/api/admin/v1/operations', { items: [], total: 0, limit: 50, offset: 0 }, 'VPN-операции'],
  ['/audit', '/api/admin/v1/audit-events', { items: [], total: 0, limit: 50, offset: 0 }, 'Журнал аудита'],
])('route %s renders its page', async (route, endpoint, response, heading) => {
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json(actor)),
    http.get(endpoint, () => HttpResponse.json(response)),
  )
  renderApp(<App />, route)
  expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument()
})

test('unknown protected route renders a useful 404', async () => {
  server.use(http.get('/api/admin/v1/auth/me', () => HttpResponse.json(actor)))
  renderApp(<App />, '/does-not-exist')
  expect(await screen.findByRole('heading', { name: 'Страница не найдена' })).toBeInTheDocument()
})

test('direct URL shows a local forbidden state without calling protected data', async () => {
  let auditCalled = false
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ actor: { id: 2, username: 'support', role: 'support', permissions: ['users:read'] }, csrf_token: 'csrf' })),
    http.get('/api/admin/v1/audit-events', () => { auditCalled = true; return HttpResponse.json({ items: [] }) }),
  )
  renderApp(<App />, '/audit')
  expect(await screen.findByText('Нет доступа к разделу')).toBeInTheDocument()
  expect(auditCalled).toBe(false)
})

test('operations are available with configs:read even without servers:read', async () => {
  let operationsCalled = false
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ actor: { id: 3, username: 'operator', role: 'operator', permissions: ['configs:read'] }, csrf_token: 'csrf' })),
    http.get('/api/admin/v1/operations', () => {
      operationsCalled = true
      return HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 })
    }),
  )
  renderApp(<App />, '/operations')
  expect(await screen.findByRole('heading', { name: 'VPN-операции' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'VPN-операции' })).toBeInTheDocument()
  expect(operationsCalled).toBe(true)
})
