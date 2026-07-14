import { http, HttpResponse } from 'msw'
import { financeApi, serversApi } from '../api'
import { server } from './server'

test('maps the server status filter to the backend lifecycle and health contract', async () => {
  const requests: URL[] = []
  server.use(
    http.get('/api/admin/v1/servers', ({ request }) => {
      requests.push(new URL(request.url))
      return HttpResponse.json({ items: [], total: 0, limit: 25, offset: 0 })
    }),
  )

  await serversApi.list({ status: 'active', limit: 25 })
  await serversApi.list({ status: 'unhealthy', limit: 25 })

  expect(requests[0].searchParams.get('lifecycle_state')).toBe('active')
  expect(requests[0].searchParams.has('status')).toBe(false)
  expect(requests[1].searchParams.get('health_state')).toBe('unhealthy')
  expect(requests[1].searchParams.has('status')).toBe(false)
})

test('loads global ledger rows with an exact period and nested user identity', async () => {
  let requested: URL | undefined
  server.use(
    http.get('/api/admin/v1/finance/ledger', ({ request }) => {
      requested = new URL(request.url)
      return HttpResponse.json({
        items: [{
          id: 10,
          user: { id: 7, username: 'alice', tg_id: 9_000_000_000 },
          amount: '50.00',
          balance_after: '150.00',
          kind: 'admin_adjustment',
          details: { comment: 'Support credit' },
        }],
        total: 1,
        limit: 25,
        offset: 0,
        snapshot_id: 10,
      })
    }),
  )

  const page = await financeApi.ledger({
    from: '2026-07-01',
    to: '2026-07-14',
    direction: 'credit',
    limit: 25,
  })

  expect(requested?.searchParams.get('from')).toBe('2026-06-30T17:00:00.000Z')
  expect(requested?.searchParams.get('to')).toBe('2026-07-14T17:00:00.000Z')
  expect(requested?.searchParams.get('direction')).toBe('credit')
  expect(page.snapshot_id).toBe(10)
  expect(page.items[0]).toMatchObject({
    user_id: 7,
    user_username: 'alice',
    user_tg_id: 9_000_000_000,
    amount: '50.00',
    description: 'Support credit',
  })
})
