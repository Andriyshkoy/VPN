import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowDownLeft, ArrowUpRight } from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import { usersApi, type BalanceAdjustmentInput, type User } from '../api'
import { formatMoney, userLabel } from '../lib/format'
import { Modal } from './Modal'

export function BalanceAdjustmentDialog({ user, open, onClose }: { user: User; open: boolean; onClose: () => void }) {
  const [type, setType] = useState<'credit' | 'debit'>('credit')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const [comment, setComment] = useState('')
  const [validation, setValidation] = useState('')
  const intent = useRef<{ fingerprint: string; key: string } | null>(null)
  const queryClient = useQueryClient()
  const current = user.balance === undefined ? null : parseCents(user.balance)
  const adjustment = parseCents(amount)
  const after = (current ?? 0n) + (type === 'credit' ? adjustment ?? 0n : -(adjustment ?? 0n))
  const mutation = useMutation({
    mutationFn: ({ input, key }: { input: BalanceAdjustmentInput; key: string }) => usersApi.adjustBalance(String(user.id), input, key),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['users'] }),
        queryClient.invalidateQueries({ queryKey: ['user', String(user.id)] }),
        queryClient.invalidateQueries({ queryKey: ['user-ledger', String(user.id)] }),
      ])
      intent.current = null; setAmount(''); setReason(''); setComment(''); setValidation(''); onClose()
    },
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setValidation('')
    if (current === null) return setValidation('Текущий баланс недоступен. Обновите карточку пользователя и повторите попытку.')
    if (adjustment === null || adjustment <= 0n) return setValidation('Введите положительную сумму, максимум с двумя знаками после запятой.')
    if (!reason.trim()) return setValidation('Укажите причину корректировки.')
    if (comment.trim().length < 3) return setValidation('Добавьте комментарий длиной не менее трёх символов.')
    if (type === 'debit' && adjustment > current) return setValidation('Списание не может сделать баланс отрицательным.')
    const input: BalanceAdjustmentInput = {
      direction: type,
      amount: centsToMoney(adjustment),
      reason_code: reason.trim(),
      comment: comment.trim(),
      expected_balance: user.balance,
      expected_ledger_entry_id: user.latest_ledger_entry_id,
    }
    const fingerprint = JSON.stringify(input)
    if (!intent.current || intent.current.fingerprint !== fingerprint) intent.current = { fingerprint, key: crypto.randomUUID() }
    mutation.mutate({ input, key: intent.current.key })
  }

  return (
    <Modal open={open} onClose={onClose} title="Корректировка баланса" description={`${userLabel(user)} · текущий баланс ${user.balance === undefined ? 'недоступен' : formatMoney(user.balance)}`}>
      <form id="balance-adjustment-form" className="form-stack" onSubmit={submit}>
        <fieldset className="segmented-field"><legend>Тип операции</legend><div className="segmented">
          <label className={type === 'credit' ? 'segmented__option segmented__option--active' : 'segmented__option'}><input type="radio" name="adjustment-type" value="credit" checked={type === 'credit'} onChange={() => setType('credit')} /><ArrowDownLeft /> Пополнение</label>
          <label className={type === 'debit' ? 'segmented__option segmented__option--active segmented__option--danger' : 'segmented__option'}><input type="radio" name="adjustment-type" value="debit" checked={type === 'debit'} onChange={() => setType('debit')} /><ArrowUpRight /> Списание</label>
        </div></fieldset>
        <label className="field"><span>Сумма, ₽</span><input name="amount" inputMode="decimal" autoComplete="off" placeholder="0,00" value={amount} onChange={(event) => setAmount(event.target.value)} required /></label>
        <label className="field"><span>Причина <em>обязательно</em></span><select name="reason" value={reason} onChange={(event) => setReason(event.target.value)} required><option value="">Выберите причину</option><option value="manual_correction">Ручная корректировка</option><option value="support_compensation">Компенсация</option><option value="manual_payment">Ручное пополнение</option><option value="fraud_correction">Исправление ошибочной операции</option><option value="other">Другое</option></select></label>
        <label className="field"><span>Комментарий <em>обязательно</em></span><textarea name="comment" rows={3} minLength={3} maxLength={500} required placeholder="Контекст для журнала аудита" value={comment} onChange={(event) => setComment(event.target.value)} /></label>
        <div className={`balance-preview ${after < 0n ? 'balance-preview--danger' : ''}`}><span>Баланс после операции</span><strong>{formatMoney(centsToMoney(after))}</strong></div>
        {(validation || mutation.isError) && <div className="form-error" role="alert">{validation || (mutation.error instanceof Error ? mutation.error.message : 'Не удалось изменить баланс')}</div>}
        <div className="form-actions"><button type="button" className="button button--secondary" onClick={onClose}>Отмена</button><button type="submit" className={`button ${type === 'debit' ? 'button--danger' : 'button--primary'}`} disabled={mutation.isPending}>{mutation.isPending ? 'Сохраняем…' : type === 'credit' ? 'Пополнить баланс' : 'Списать средства'}</button></div>
      </form>
    </Modal>
  )
}

function parseCents(value: string): bigint | null {
  const match = value.trim().replace(',', '.').match(/^(-?)(\d+)(?:\.(\d{1,2}))?$/)
  if (!match) return null
  const absolute = BigInt(match[2]) * 100n + BigInt((match[3] ?? '').padEnd(2, '0'))
  return match[1] ? -absolute : absolute
}

function centsToMoney(value: bigint) {
  const negative = value < 0n
  const absolute = negative ? -value : value
  return `${negative ? '-' : ''}${absolute / 100n}.${String(absolute % 100n).padStart(2, '0')}`
}
