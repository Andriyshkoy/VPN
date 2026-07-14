export function formatMoney(value: string | number | null | undefined, currency = 'RUB') {
  if (value === null || value === undefined || value === '') return '—'
  const amount = Number(value)
  if (!Number.isFinite(amount)) return '—'
  return new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency,
    maximumFractionDigits: 2,
  }).format(amount)
}

export function formatNumber(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? new Intl.NumberFormat('ru-RU').format(number) : '—'
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  const date = match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  }).format(date)
}

export function userLabel(user: { username?: string | null; display_name?: string | null; id?: string | number; tg_id?: string | number }) {
  if (user.username) return `@${user.username.replace(/^@/, '')}`
  return user.display_name || (user.tg_id ? `Telegram ${user.tg_id}` : `Пользователь #${user.id ?? '—'}`)
}

export function percent(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(number)}%` : '—'
}

export function shortId(value: string | number | null | undefined) {
  if (value === null || value === undefined) return '—'
  const text = String(value)
  return text.length > 14 ? `${text.slice(0, 7)}…${text.slice(-5)}` : text
}

export function formatBytes(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === '') return '—'
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes === 0) return '0 Б'
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ', 'ПБ']
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: unit ? 1 : 0 }).format(bytes / (1024 ** unit))} ${units[unit]}`
}
