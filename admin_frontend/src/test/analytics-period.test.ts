import { http, HttpResponse } from 'msw'
import { analyticsApi } from '../api'
import { server } from './server'

test('uses the same inclusive calendar-day range for overview and timeseries', async () => {
  const requests: URL[] = []
  server.use(
    http.get('/api/admin/v1/analytics/overview', ({ request }) => {
      requests.push(new URL(request.url))
      return HttpResponse.json({ finance: {}, users: {} })
    }),
    http.get('/api/admin/v1/analytics/finance/timeseries', ({ request }) => {
      requests.push(new URL(request.url))
      return HttpResponse.json({ items: [] })
    }),
  )

  const period = { from: '2026-07-01', to: '2026-07-14', granularity: 'day' }
  await Promise.all([analyticsApi.overview(period), analyticsApi.timeseries(period)])

  expect(requests).toHaveLength(2)
  for (const request of requests) {
    expect(request.searchParams.get('from')).toBe('2026-06-30T17:00:00.000Z')
    expect(request.searchParams.get('to')).toBe('2026-07-14T17:00:00.000Z')
  }
  expect(requests.find((request) => request.pathname.endsWith('/timeseries'))?.searchParams.get('timezone')).toBe('Asia/Novosibirsk')
})
