import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { campaignsApi, type Campaign } from '../api/campaigns'
import { CampaignsPage } from '../pages/CampaignsPage'
import { actor } from './fixtures'
import { renderApp } from './render'
import { server } from './server'

const api = '/api/admin/v1'
const permissions = ['campaigns:read', 'campaigns:write', 'campaigns:send']
const counts = { total: 3, pending: 0, queued: 0, sending: 0, delivered: 1, failed: 1, unknown: 1, skipped: 0, cancelled: 0 }
const initial: Campaign = { id: 'demo-campaign', name: 'DEMO: новости VPN', text: 'DEMO: сервис восстановлен', audience: { kind: 'all', user_ids: [] }, revision: 1, status: 'draft', counts: { ...counts, total: 0, delivered: 0, failed: 0, unknown: 0 } }

function mocks(granted = permissions, items: Campaign[] = []) {
  let saved = { ...initial }
  server.use(
    http.get(`${api}/auth/me`, () => HttpResponse.json({ ...actor, actor: { ...actor.actor, permissions: granted } })),
    http.get(`${api}/campaigns`, () => HttpResponse.json({ items, total: items.length, limit: 25, offset: 0 })),
    http.get(`${api}/campaigns/:id`, () => HttpResponse.json(saved)),
    http.post(`${api}/campaigns`, async ({ request }) => { saved = { ...saved, ...await request.json() as object }; return HttpResponse.json(saved) }),
    http.patch(`${api}/campaigns/:id`, async ({ request }) => { saved = { ...saved, ...await request.json() as object, revision: saved.revision + 1 }; return HttpResponse.json(saved) }),
    http.post(`${api}/campaigns/:id/preview`, () => HttpResponse.json({ campaign_id: saved.id, revision: saved.revision, preview_token: 'demo-preview', eligible_count: 3, skipped_count: 2, missing_count: 0, sample: [{ user_id: 42, display: 'user #42' }], expires_at: '2099-01-01T00:00:00Z' })),
  )
}

async function prepare() {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Название рассылки'), 'DEMO: новости VPN')
  await user.type(screen.getByLabelText('Текст сообщения'), 'DEMO: сервис восстановлен')
  await user.click(screen.getByRole('button', { name: 'Сохранить и проверить аудиторию' }))
  await screen.findByText('Проверенная аудитория')
  return user
}

test('requires reviewed recipient count, SEND and reason before an explicit launch with CSRF', async () => {
  mocks()
  let sent = false
  server.use(http.post(`${api}/campaigns/:id/launch`, async ({ request }) => {
    sent = true
    expect(request.headers.get('x-csrf-token')).toBe('csrf-test-token')
    expect(request.headers.get('idempotency-key')).toBeTruthy()
    expect(await request.json()).toEqual({ preview_token: 'demo-preview', revision: 1, expected_recipient_count: 3, confirmation: 'SEND', reason: 'Сервис восстановлен' })
    return HttpResponse.json({ ...initial, status: 'queued', counts: { ...initial.counts, total: 3, queued: 3 } })
  }))
  renderApp(<CampaignsPage />, '/campaigns')
  const user = await prepare()
  expect(sent).toBe(false)
  expect(screen.getByText('Пропущены: бот заблокирован или недоступен')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Проверить и запустить' }))
  const dialog = within(screen.getByRole('dialog', { name: 'Запуск рассылки' }))
  const launch = dialog.getByRole('button', { name: 'ЗАПУСТИТЬ РАССЫЛКУ' })
  expect(launch).toBeDisabled()
  await user.type(dialog.getByLabelText('Введите число получателей'), '2')
  await user.type(dialog.getByLabelText('Для подтверждения введите SEND'), 'SEND')
  await user.type(dialog.getByLabelText('Причина запуска'), 'Сервис восстановлен')
  expect(launch).toBeDisabled()
  await user.clear(dialog.getByLabelText('Введите число получателей'))
  await user.type(dialog.getByLabelText('Введите число получателей'), '3')
  await user.click(launch)
  await waitFor(() => expect(sent).toBe(true))
})

test('paused delivery is visible before confirmation and an explicit launch only creates a waiting queue', async () => {
  mocks()
  let queued = false
  const pausedDraft = { ...initial, delivery_paused: true }
  const waiting = { ...pausedDraft, status: 'queued', counts: { ...initial.counts, total: 3, pending: 3 } }
  server.use(
    http.post(`${api}/campaigns`, () => HttpResponse.json(pausedDraft)),
    http.get(`${api}/campaigns/:id`, () => HttpResponse.json(queued ? waiting : pausedDraft)),
    http.post(`${api}/campaigns/:id/launch`, async ({ request }) => {
      expect(await request.json()).toMatchObject({ confirmation: 'SEND', expected_recipient_count: 3, reason: 'Подготовить очередь' })
      queued = true
      return HttpResponse.json(waiting)
    }),
  )
  renderApp(<CampaignsPage />, '/campaigns')
  const user = await prepare()
  expect(screen.getByText('Доставка приостановлена')).toBeInTheDocument()
  expect(screen.getByText(/Создание очереди не включает доставку/)).toBeInTheDocument()
  expect(queued).toBe(false)
  await user.click(screen.getByRole('button', { name: 'Проверить и запустить' }))
  const dialog = within(screen.getByRole('dialog', { name: 'Запуск рассылки' }))
  expect(dialog.getByText('Доставка приостановлена')).toBeInTheDocument()
  expect(dialog.getByText(/После запуска очередь будет ждать включения доставки оператором/)).toBeInTheDocument()
  const launch = dialog.getByRole('button', { name: 'СОЗДАТЬ ОЧЕРЕДЬ РАССЫЛКИ' })
  expect(launch).toBeDisabled()
  await user.type(dialog.getByLabelText('Введите число получателей'), '3')
  await user.type(dialog.getByLabelText('Для подтверждения введите SEND'), 'SEND')
  expect(launch).toBeDisabled()
  await user.type(dialog.getByLabelText('Причина запуска'), 'Подготовить очередь')
  await user.click(launch)
  await waitFor(() => expect(queued).toBe(true))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(await screen.findByText('Доставка приостановлена')).toBeInTheDocument()
  expect(screen.getByText(/Очередь создана и будет ждать включения доставки оператором/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Включить доставку/ })).not.toBeInTheDocument()
})

test('editing text or recipients invalidates the reviewed audience and selected IDs are deduplicated', async () => {
  mocks()
  let body: unknown
  server.use(http.post(`${api}/campaigns`, async ({ request }) => { body = await request.json(); return HttpResponse.json({ ...initial, ...body as object }) }))
  renderApp(<CampaignsPage />, '/campaigns?user_id=42')
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Название рассылки'), 'DEMO')
  await user.type(screen.getByLabelText('Текст сообщения'), 'DEMO: уведомление')
  await user.type(screen.getByLabelText('ID пользователей'), ', 42, 57')
  await user.click(screen.getByRole('button', { name: 'Сохранить и проверить аудиторию' }))
  await screen.findByText('Проверенная аудитория')
  expect(body).toEqual({ name: 'DEMO', text: 'DEMO: уведомление', audience: { kind: 'selected', user_ids: [42, 57] } })
  await user.type(screen.getByLabelText('Текст сообщения'), '!')
  expect(screen.queryByText('Проверенная аудитория')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Проверить и запустить' })).not.toBeInTheDocument()
})

test('retrying an ambiguous launch response keeps its idempotency key and review token', async () => {
  mocks()
  const keys: (string | null)[] = []
  server.use(http.post(`${api}/campaigns/:id/launch`, ({ request }) => {
    keys.push(request.headers.get('idempotency-key'))
    return keys.length === 1 ? HttpResponse.json({ detail: 'Gateway timeout' }, { status: 504 }) : HttpResponse.json({ ...initial, status: 'queued' })
  }))
  renderApp(<CampaignsPage />, '/campaigns')
  const user = await prepare()
  await user.click(screen.getByRole('button', { name: 'Проверить и запустить' }))
  const dialog = within(screen.getByRole('dialog'))
  await user.type(dialog.getByLabelText('Введите число получателей'), '3')
  await user.type(dialog.getByLabelText('Для подтверждения введите SEND'), 'SEND')
  await user.type(dialog.getByLabelText('Причина запуска'), 'DEMO проверка')
  await user.click(dialog.getByRole('button', { name: 'ЗАПУСТИТЬ РАССЫЛКУ' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Gateway timeout')
  await user.click(dialog.getByRole('button', { name: 'ЗАПУСТИТЬ РАССЫЛКУ' }))
  await waitFor(() => expect(keys).toHaveLength(2))
  expect(keys[0]).toBeTruthy()
  expect(keys[1]).toBe(keys[0])
})

test('read-only staff can inspect campaign results without composer or dispatch controls', async () => {
  const completed = { ...initial, status: 'completed', counts }
  mocks(['campaigns:read'], [completed])
  server.use(http.get(`${api}/campaigns/:id`, () => HttpResponse.json(completed)))
  renderApp(<CampaignsPage />, '/campaigns')
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: completed.name }))
  expect(await screen.findByRole('progressbar', { name: 'Ход рассылки' })).toHaveAttribute('aria-valuenow', '3')
  expect(screen.getByText(/повторно автоматически не отправляется/)).toBeInTheDocument()
  expect(screen.queryByLabelText('Текст сообщения')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Повторить ошибки/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Новый черновик' })).not.toBeInTheDocument()
})

test('a stale server preview is discarded and cannot be launched again without a fresh review', async () => {
  mocks()
  let attempts = 0
  server.use(http.post(`${api}/campaigns/:id/launch`, () => { attempts += 1; return HttpResponse.json({ detail: 'Аудитория изменилась' }, { status: 409 }) }))
  renderApp(<CampaignsPage />, '/campaigns')
  const user = await prepare()
  await user.click(screen.getByRole('button', { name: 'Проверить и запустить' }))
  const dialog = within(screen.getByRole('dialog'))
  await user.type(dialog.getByLabelText('Введите число получателей'), '3')
  await user.type(dialog.getByLabelText('Для подтверждения введите SEND'), 'SEND')
  await user.type(dialog.getByLabelText('Причина запуска'), 'DEMO проверка')
  await user.click(dialog.getByRole('button', { name: 'ЗАПУСТИТЬ РАССЫЛКУ' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Аудитория изменилась')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Проверить и запустить' })).not.toBeInTheDocument()
  expect(attempts).toBe(1)
})

test('cancel and retry send only explicit audited actions, retaining unknown deliveries', async () => {
  const completed = { ...initial, status: 'completed', counts }
  mocks(permissions, [completed])
  server.use(http.get(`${api}/campaigns/:id`, () => HttpResponse.json(completed)))
  let actionBody: unknown
  server.use(http.post(`${api}/campaigns/:id/retry`, async ({ request }) => { actionBody = await request.json(); expect(request.headers.get('idempotency-key')).toBeTruthy(); return HttpResponse.json({ ...completed, status: 'queued' }) }))
  renderApp(<CampaignsPage />, '/campaigns')
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: completed.name }))
  await user.click(await screen.findByRole('button', { name: 'Повторить ошибки (1)' }))
  const dialog = within(screen.getByRole('dialog'))
  expect(dialog.getByText(/неизвестные результаты не будут отправлены повторно/)).toBeInTheDocument()
  const retry = dialog.getByRole('button', { name: 'Повторить только ошибки' })
  expect(retry).toBeDisabled()
  await user.type(dialog.getByLabelText('Причина действия'), 'DEMO: повтор временных ошибок')
  await user.click(retry)
  await waitFor(() => expect(actionBody).toEqual({ reason: 'DEMO: повтор временных ошибок' }))
})

test('campaign API cancellation preserves exact reason and idempotency headers', async () => {
  let called = false
  server.use(http.post(`${api}/campaigns/demo-campaign/cancel`, async ({ request }) => { called = true; expect(request.headers.get('idempotency-key')).toBe('demo-cancel-key'); expect(await request.json()).toEqual({ reason: 'DEMO: остановка' }); return HttpResponse.json({ ...initial, status: 'cancelled' }) }))
  await campaignsApi.cancel('demo-campaign', 'DEMO: остановка', 'demo-cancel-key')
  expect(called).toBe(true)
})

test('a new retry cycle gets a new key after the previous action succeeded', async () => {
  const completed = { ...initial, status: 'completed', counts }
  mocks(permissions, [completed])
  const keys: (string | null)[] = []
  server.use(
    http.get(`${api}/campaigns/:id`, () => HttpResponse.json(completed)),
    http.post(`${api}/campaigns/:id/retry`, ({ request }) => { keys.push(request.headers.get('idempotency-key')); return HttpResponse.json(completed) }),
  )
  renderApp(<CampaignsPage />, '/campaigns')
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: completed.name }))
  for (let cycle = 0; cycle < 2; cycle += 1) {
    await user.click(await screen.findByRole('button', { name: 'Повторить ошибки (1)' }))
    const dialog = within(screen.getByRole('dialog'))
    await user.type(dialog.getByLabelText('Причина действия'), 'DEMO: повтор временных ошибок')
    await user.click(dialog.getByRole('button', { name: 'Повторить только ошибки' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  }
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBeTruthy()
  expect(keys[1]).toBeTruthy()
  expect(keys[1]).not.toBe(keys[0])
})
