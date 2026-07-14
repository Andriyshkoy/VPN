import { LockKeyhole, ShieldCheck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '../api'
import { useAuth } from '../auth/AuthProvider'

export function LoginPage() {
  const { user, login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  if (user) return <Navigate to="/" replace />

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      await login(username.trim(), password)
      const destination = (location.state as { from?: { pathname?: string; search?: string; hash?: string } } | null)?.from
      const from = destination?.pathname ? `${destination.pathname}${destination.search ?? ''}${destination.hash ?? ''}` : undefined
      navigate(from || '/', { replace: true })
    } catch (failure) {
      if (failure instanceof ApiError && [401, 403].includes(failure.status)) setError('Неверный логин или пароль')
      else setError(failure instanceof Error ? failure.message : 'Не удалось войти. Попробуйте ещё раз.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-hero">
        <div className="login-hero__content">
          <span className="brand brand--light"><span className="brand__mark">V</span><strong>VPN Hub</strong></span>
          <div><span className="eyebrow">Единый центр управления</span><h1>Вся инфраструктура<br />под контролем</h1><p>Пользователи, финансы, реферальная сеть и VPN-серверы — в одной защищённой панели.</p></div>
          <div className="login-feature"><ShieldCheck /><span><strong>Защищённая сессия</strong><small>HttpOnly cookie и CSRF-защита</small></span></div>
        </div>
      </section>
      <section className="login-form-wrap">
        <form className="login-card" onSubmit={submit}>
          <div className="login-card__icon"><LockKeyhole /></div>
          <h2>Вход в панель</h2>
          <p>Используйте учётную запись администратора.</p>
          {error && <div className="form-error" role="alert">{error}</div>}
          <label className="field"><span>Логин</span><input autoComplete="username" autoFocus required value={username} onChange={(event) => setUsername(event.target.value)} placeholder="admin" /></label>
          <label className="field"><span>Пароль</span><input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Введите пароль" /></label>
          <button className="button button--primary button--wide" type="submit" disabled={submitting}>{submitting ? 'Входим…' : 'Войти'}</button>
          <small className="login-card__note">Доступ к панели журналируется.</small>
        </form>
      </section>
    </main>
  )
}
