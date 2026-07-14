import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { authApi, type AdminIdentity } from '../api'

interface AuthContextValue {
  user: AdminIdentity | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

function normalizeIdentity(value: unknown): AdminIdentity | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const candidate = record.user && typeof record.user === 'object' ? record.user as Record<string, unknown> : record
  if (typeof candidate.username !== 'string') return null
  return {
    id: candidate.id as string | number | undefined,
    username: candidate.username,
    display_name: typeof candidate.display_name === 'string' ? candidate.display_name : undefined,
    roles: Array.isArray(candidate.roles) ? candidate.roles.map(String) : ['admin'],
    permissions: Array.isArray(candidate.permissions) ? candidate.permissions.map(String) : [],
    csrf_token: typeof candidate.csrf_token === 'string' ? candidate.csrf_token : undefined,
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AdminIdentity | null>(null)
  const [loading, setLoading] = useState(true)

  const loadIdentity = useCallback(async () => {
    try {
      const identity = normalizeIdentity(await authApi.me())
      setUser(identity)
      return identity
    } catch {
      setUser(null)
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadIdentity()
    const unauthorized = () => setUser(null)
    window.addEventListener('admin:unauthorized', unauthorized)
    return () => window.removeEventListener('admin:unauthorized', unauthorized)
  }, [loadIdentity])

  const login = useCallback(async (username: string, password: string) => {
    const response = await authApi.login(username, password)
    const identity = normalizeIdentity(response) ?? await loadIdentity()
    if (!identity) throw new Error('Сессия создана, но данные администратора недоступны')
    setUser(identity)
  }, [loadIdentity])

  const logout = useCallback(async () => {
    try { await authApi.logout() } finally { setUser(null) }
  }, [])

  const value = useMemo(() => ({ user, loading, login, logout }), [user, loading, login, logout])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
