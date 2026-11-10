import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState, type FormEvent } from 'react'
import type { User } from '../api'
import { subscriptionsApi } from '../api/subscriptions'
import { formatMoney, userLabel } from '../lib/format'
import { Modal } from './Modal'

export function UnlimitedBalanceDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const current = user.unlimited_balance === true
  const intent = useRef<{ fingerprint: string; key: string } | null>(null)
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => {
      const input = { enabled: !current, expected_enabled: current, reason: reason.trim() }
      const fingerprint = JSON.stringify(input)
      if (intent.current?.fingerprint !== fingerprint) intent.current = { fingerprint, key: crypto.randomUUID() }
      return subscriptionsApi.setUnlimitedBalance(String(user.id), input, intent.current.key)
    },
    onSuccess: async () => {
      await Promise.all([['users'], ['user', String(user.id)], ['user-subscription', String(user.id)], ['subscriptions'], ['audit']].map((queryKey) => queryClient.invalidateQueries({ queryKey })))
      onClose()
    },
  })
  const submit = (event: FormEvent) => { event.preventDefault(); if (confirmed && reason.trim().length >= 3 && !mutation.isPending) mutation.mutate() }
  return <Modal open title={current ? 'Выключить бесконечный баланс' : 'Включить бесконечный баланс'} description={userLabel(user)} onClose={() => { if (!mutation.isPending) onClose() }}><form className="form-stack" onSubmit={submit}>
    <div className="product-notice"><strong>{current ? `Обычный баланс: ${formatMoney(user.balance)}` : 'Баланс будет отображаться как ∞'}</strong><p>{current ? 'Оплата подписки снова будет учитывать сохранённый кошелёк. Если общие списания выключены, они останутся выключенными.' : 'Почасовая оплата подписки не расходует деньги этого аккаунта. Сохранённый кошелёк и история операций остаются без изменений.'}</p><p>Сохранённый кошелёк: <strong>{formatMoney(user.balance)}</strong>. Переключение записывается в аудит.</p></div>
    <label className="field"><span>Причина изменения</span><textarea required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} rows={3} /></label>
    <label className="check-field"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />Подтверждаю изменение режима для этого аккаунта</label>
    {mutation.isError && <div className="form-error" role="alert">{mutation.error.message}</div>}
    <div className="form-actions"><button type="button" className="button button--secondary" disabled={mutation.isPending} onClick={onClose}>Отмена</button><button type="submit" className="button button--primary" disabled={!confirmed || reason.trim().length < 3 || mutation.isPending}>{mutation.isPending ? 'Сохраняем…' : current ? 'Выключить режим ∞' : 'Включить режим ∞'}</button></div>
  </form></Modal>
}
