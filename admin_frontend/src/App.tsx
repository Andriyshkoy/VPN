import { lazy, Suspense, type ReactNode } from 'react'
import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { ProtectedRoute } from './components/ProtectedRoute'
import { ForbiddenState, PageLoading } from './components/States'
import { useAuth } from './auth/AuthProvider'

const AuditPage = lazy(() => import('./pages/AuditPage').then((module) => ({ default: module.AuditPage })))
const ConfigsPage = lazy(() => import('./pages/ConfigsPage').then((module) => ({ default: module.ConfigsPage })))
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const FinancePage = lazy(() => import('./pages/FinancePage').then((module) => ({ default: module.FinancePage })))
const LoginPage = lazy(() => import('./pages/LoginPage').then((module) => ({ default: module.LoginPage })))
const MonitoringPage = lazy(() => import('./pages/MonitoringPage').then((module) => ({ default: module.MonitoringPage })))
const NotFoundPage = lazy(() => import('./pages/NotFoundPage').then((module) => ({ default: module.NotFoundPage })))
const OperationsPage = lazy(() => import('./pages/OperationsPage').then((module) => ({ default: module.OperationsPage })))
const ReferralsPage = lazy(() => import('./pages/ReferralsPage').then((module) => ({ default: module.ReferralsPage })))
const ServerDetailPage = lazy(() => import('./pages/ServerDetailPage').then((module) => ({ default: module.ServerDetailPage })))
const ServersPage = lazy(() => import('./pages/ServersPage').then((module) => ({ default: module.ServersPage })))
const UserDetailPage = lazy(() => import('./pages/UserDetailPage').then((module) => ({ default: module.UserDetailPage })))
const UsersPage = lazy(() => import('./pages/UsersPage').then((module) => ({ default: module.UsersPage })))

function Secured({ children, permission, anyOf }: { children: ReactNode; permission?: string; anyOf?: string[] }) {
  const { user } = useAuth()
  const required = anyOf ?? (permission ? [permission] : [])
  const allowed = required.length === 0 || required.some((candidate) => user?.permissions.includes(candidate))
  return <AppShell><Suspense fallback={<PageLoading />}>{allowed ? children : <ForbiddenState />}</Suspense></AppShell>
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Suspense fallback={<div className="fullscreen-state"><PageLoading /></div>}><LoginPage /></Suspense>} />
      <Route element={<ProtectedRoute />}>
        <Route path="/" element={<Secured permission="dashboard:read"><DashboardPage /></Secured>} />
        <Route path="/users" element={<Secured permission="users:read"><UsersPage /></Secured>} />
        <Route path="/users/:userId" element={<Secured permission="users:read"><UserDetailPage /></Secured>} />
        <Route path="/users/:userId/:tab" element={<Secured permission="users:read"><UserDetailPage /></Secured>} />
        <Route path="/finance" element={<Secured permission="finance:read"><FinancePage /></Secured>} />
        <Route path="/referrals" element={<Secured permission="referrals:read"><ReferralsPage /></Secured>} />
        <Route path="/configs" element={<Secured permission="configs:read"><ConfigsPage /></Secured>} />
        <Route path="/servers" element={<Secured permission="servers:read"><ServersPage /></Secured>} />
        <Route path="/servers/:serverId" element={<Secured permission="servers:read"><ServerDetailPage /></Secured>} />
        <Route path="/operations" element={<Secured anyOf={['configs:read', 'servers:read']}><OperationsPage /></Secured>} />
        <Route path="/monitoring" element={<Secured permission="metrics:read"><MonitoringPage /></Secured>} />
        <Route path="/audit" element={<Secured permission="audit:read"><AuditPage /></Secured>} />
        <Route path="*" element={<Secured><NotFoundPage /></Secured>} />
      </Route>
    </Routes>
  )
}
