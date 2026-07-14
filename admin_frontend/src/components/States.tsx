import { AlertTriangle, Ban, Inbox, LoaderCircle, RefreshCw } from 'lucide-react'
import type { ReactNode } from 'react'

export function PageLoading({ label = 'Загружаем данные…' }: { label?: string }) {
  return <div className="page-state" role="status"><LoaderCircle className="spin" aria-hidden="true" /><span>{label}</span></div>
}

export function InlineLoading() {
  return <span className="inline-loading" role="status"><LoaderCircle className="spin" aria-hidden="true" /> Загрузка…</span>
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : 'Не удалось загрузить данные'
  return (
    <div className="page-state page-state--error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <div><strong>Что-то пошло не так</strong><p>{message}</p></div>
      {retry && <button className="button button--secondary" type="button" onClick={retry}><RefreshCw size={16} /> Повторить</button>}
    </div>
  )
}

export function EmptyState({ title = 'Здесь пока пусто', description, action }: { title?: string; description?: string; action?: ReactNode }) {
  return (
    <div className="page-state page-state--empty">
      <Inbox aria-hidden="true" />
      <div><strong>{title}</strong>{description && <p>{description}</p>}</div>
      {action}
    </div>
  )
}

export function ForbiddenState() {
  return <div className="page-state page-state--forbidden" role="alert"><Ban aria-hidden="true" /><div><strong>Нет доступа к разделу</strong><p>Для вашей роли не выдано необходимое разрешение. Если это ошибка, обратитесь к владельцу панели.</p></div></div>
}
