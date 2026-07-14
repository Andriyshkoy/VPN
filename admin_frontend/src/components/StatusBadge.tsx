type Tone = 'success' | 'warning' | 'danger' | 'neutral' | 'info'

const statusTone: Record<string, Tone> = {
  active: 'success', healthy: 'success', ready: 'success', completed: 'success', succeeded: 'success', confirmed: 'success', success: 'success', online: 'success', enabled: 'success',
  pending: 'warning', queued: 'warning', retrying: 'warning', draining: 'warning', degraded: 'warning', suspended: 'warning', stale: 'warning', provisioning: 'info',
  failed: 'danger', error: 'danger', unhealthy: 'danger', offline: 'danger', disabled: 'danger', critical: 'danger', revoked: 'danger', blocked: 'danger', deactivated: 'danger', permanent_failure: 'danger', unreachable: 'danger', instance_mismatch: 'danger',
  info: 'info', running: 'info', processing: 'info', disabled_feature: 'neutral', retired: 'neutral', unknown: 'neutral',
}

const statusLabel: Record<string, string> = {
  active: 'Активен', healthy: 'Работает', ready: 'Готов', completed: 'Завершено', succeeded: 'Завершено', confirmed: 'Подтверждено', success: 'Успешно', online: 'Онлайн', enabled: 'Включено',
  pending: 'Ожидает', queued: 'В очереди', retrying: 'Повтор', draining: 'Drain', degraded: 'Есть проблемы', suspended: 'Приостановлен', stale: 'Данные устарели', provisioning: 'Создаётся',
  failed: 'Ошибка', error: 'Ошибка', unhealthy: 'Не работает', offline: 'Офлайн', disabled: 'Отключён', critical: 'Критично', revoked: 'Отозван', blocked: 'Бот заблокирован', deactivated: 'Деактивирован', permanent_failure: 'Недоступен', unreachable: 'Недоступен', instance_mismatch: 'Другой instance', retired: 'Выведен',
  running: 'Выполняется', processing: 'Обработка', unknown: 'Неизвестно', disabled_feature: 'Выключено',
}

export function StatusBadge({ value, label }: { value?: string | boolean | null; label?: string }) {
  const normalized = typeof value === 'boolean' ? (value ? 'active' : 'disabled') : String(value ?? 'unknown').toLowerCase()
  return <span className={`status status--${statusTone[normalized] ?? 'neutral'}`}>{label ?? statusLabel[normalized] ?? value ?? 'Неизвестно'}</span>
}
