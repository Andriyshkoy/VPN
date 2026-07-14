import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BalanceAdjustmentDialog } from '../components/BalanceAdjustmentDialog'
import { renderUnit } from './render'
import { server } from './server'

test('submits exact money and optimistic balance guards with CSRF', async () => {
  document.cookie = 'vpn_admin_csrf=cookie-csrf; path=/'
  let called = false
  server.use(http.post('/api/admin/v1/users/42/balance-adjustments', async ({ request }) => {
    called = true
    expect(request.headers.get('x-csrf-token')).toBe('cookie-csrf')
    expect(request.headers.get('idempotency-key')).toBeTruthy()
    expect(await request.json()).toEqual({
      direction: 'debit',
      amount: '25.50',
      reason_code: 'support_compensation',
      comment: 'Исправление поддержки',
      expected_balance: '125.50',
      expected_ledger_entry_id: 99,
    })
    return HttpResponse.json({ user_id: 42, balance: '100.00', ledger_entry_id: 100 })
  }))
  const close = vi.fn()
  renderUnit(<BalanceAdjustmentDialog open onClose={close} user={{ id: 42, username: 'alice', balance: '125.50', latest_ledger_entry_id: 99 }} />)
  const user = userEvent.setup()
  await user.click(screen.getByLabelText(/Списание/))
  await user.type(screen.getByLabelText('Сумма, ₽'), '25,50')
  await user.selectOptions(screen.getByLabelText(/Причина/), 'support_compensation')
  await user.type(screen.getByLabelText(/Комментарий/), 'Исправление поддержки')
  expect(screen.getByText(/100,00/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Списать средства' }))
  await waitFor(() => expect(called).toBe(true))
  await waitFor(() => expect(close).toHaveBeenCalled())
})

test('reuses the idempotency key when the same adjustment is retried', async () => {
  const keys: Array<string | null> = []
  let calls = 0
  server.use(http.post('/api/admin/v1/users/42/balance-adjustments', ({ request }) => {
    calls += 1
    keys.push(request.headers.get('idempotency-key'))
    if (calls === 1) return HttpResponse.json({ detail: 'Gateway timeout' }, { status: 504 })
    return HttpResponse.json({ user_id: 42, balance: '135.50', ledger_entry_id: 100 })
  }))
  const close = vi.fn()
  renderUnit(<BalanceAdjustmentDialog open onClose={close} user={{ id: 42, username: 'alice', balance: '125.50', latest_ledger_entry_id: 99 }} />)
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Сумма, ₽'), '10')
  await user.selectOptions(screen.getByLabelText(/Причина/), 'manual_payment')
  await user.type(screen.getByLabelText(/Комментарий/), 'Ручной платёж')
  await user.click(screen.getByRole('button', { name: 'Пополнить баланс' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Gateway timeout')
  await user.click(screen.getByRole('button', { name: 'Пополнить баланс' }))
  await waitFor(() => expect(close).toHaveBeenCalled())
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBeTruthy()
  expect(keys[1]).toBe(keys[0])
})
