import {
  Activity,
  ChartNoAxesCombined,
  ChevronDown,
  CircleDollarSign,
  FileKey2,
  Gauge,
  GitFork,
  LogOut,
  Menu,
  ScrollText,
  ServerCog,
  Users,
  X,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'

const navigation = [
  { to: '/', label: 'Обзор', icon: Gauge, end: true, anyOf: ['dashboard:read'] },
  { to: '/users', label: 'Пользователи', icon: Users, anyOf: ['users:read'] },
  { to: '/finance', label: 'Финансы', icon: CircleDollarSign, anyOf: ['finance:read'] },
  { to: '/referrals', label: 'Рефералы', icon: GitFork, anyOf: ['referrals:read'] },
  { to: '/configs', label: 'Конфигурации', icon: FileKey2, anyOf: ['configs:read'] },
  { to: '/servers', label: 'Серверы', icon: ServerCog, anyOf: ['servers:read'] },
  { to: '/operations', label: 'VPN-операции', icon: Activity, anyOf: ['configs:read', 'servers:read'] },
  { to: '/monitoring', label: 'Мониторинг', icon: ChartNoAxesCombined, anyOf: ['metrics:read'] },
  { to: '/audit', label: 'Журнал аудита', icon: ScrollText, anyOf: ['audit:read'] },
]

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  return (
    <div className="app-shell">
      {open && <button className="sidebar-backdrop" type="button" aria-label="Закрыть меню" onClick={() => setOpen(false)} />}
      <aside className={`sidebar ${open ? 'sidebar--open' : ''}`}>
        <div className="brand"><span className="brand__mark">V</span><div><strong>VPN Hub</strong><small>Control center</small></div><button type="button" className="icon-button sidebar__close" aria-label="Закрыть меню" onClick={() => setOpen(false)}><X /></button></div>
        <nav className="sidebar__nav" aria-label="Основная навигация">
          <span className="sidebar__label">Управление</span>
          {navigation.filter((item) => item.anyOf.some((permission) => user?.permissions.includes(permission))).map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)} className={({ isActive }) => `nav-item ${isActive ? 'nav-item--active' : ''}`}>
              <Icon aria-hidden="true" /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar__footer"><span className="system-pulse"><i />Production</span><small>Панель v2</small></div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <button type="button" className="icon-button menu-button" aria-label="Открыть меню" onClick={() => setOpen(true)}><Menu /></button>
          <div className="topbar__spacer" />
          <div className="account">
            <button type="button" className="account__trigger" aria-expanded={accountOpen} onClick={() => setAccountOpen((value) => !value)}>
              <span className="account__avatar">{(user?.display_name || user?.username || 'A').slice(0, 1).toUpperCase()}</span>
              <span className="account__name"><strong>{user?.display_name || user?.username}</strong><small>{user?.roles.join(', ') || 'Администратор'}</small></span>
              <ChevronDown size={16} />
            </button>
            {accountOpen && <div className="account__menu"><button type="button" onClick={() => void logout()}><LogOut size={16} /> Выйти</button></div>}
          </div>
        </header>
        <main className="main-content">{children}</main>
      </div>
    </div>
  )
}
