import { ChevronLeft, ChevronRight } from 'lucide-react'

export function Pagination({ offset, limit, total, onChange }: { offset: number; limit: number; total: number; onChange: (offset: number) => void }) {
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)
  return (
    <nav className="pagination" aria-label="Пагинация">
      <span>{from}–{to} из {total}</span>
      <div className="button-group">
        <button type="button" className="icon-button" aria-label="Предыдущая страница" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}><ChevronLeft /></button>
        <button type="button" className="icon-button" aria-label="Следующая страница" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}><ChevronRight /></button>
      </div>
    </nav>
  )
}
