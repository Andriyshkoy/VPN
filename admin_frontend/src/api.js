export const apiUrl = import.meta.env.VITE_ADMIN_API_URL

export function authHeaders() {
  const token = localStorage.getItem('authToken')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export function handleUnauthorized(status) {
  if (status === 401) {
    localStorage.removeItem('authToken')
    localStorage.removeItem('loggedIn')
    window.location.reload()
    return true
  }
  return false
}
