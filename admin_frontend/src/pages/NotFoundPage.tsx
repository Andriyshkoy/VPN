import { ArrowLeft } from 'lucide-react'
import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return <div className="not-found"><strong>404</strong><h1>Страница не найдена</h1><p>Возможно, ссылка устарела или раздел был перемещён.</p><Link className="button button--primary" to="/"><ArrowLeft size={16} /> Вернуться на главную</Link></div>
}
