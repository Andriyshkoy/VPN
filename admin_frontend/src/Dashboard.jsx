import { useEffect, useState } from 'react'

const apiUrl = import.meta.env.VITE_ADMIN_API_URL
const apiKey = import.meta.env.VITE_ADMIN_API_KEY

export default function Dashboard({ onLogout }) {
  const [servers, setServers] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    async function fetchServers() {
      try {
        const res = await fetch(`${apiUrl}/api/servers`, {
          headers: { 'X-API-Key': apiKey },
        })
        if (!res.ok) {
          throw new Error('Failed to fetch servers')
        }
        const data = await res.json()
        setServers(data)
      } catch (err) {
        setError(err.message)
      }
    }
    fetchServers()
  }, [])

  const handleLogout = () => {
    localStorage.removeItem('loggedIn')
    onLogout()
  }

  return (
    <div className="dashboard">
      <h2>Admin Panel</h2>
      <button onClick={handleLogout}>Logout</button>
      {error && <div className="error">{error}</div>}
      <div className="server-list">
        <h3>Servers</h3>
        <ul>
          {servers.map((srv) => (
            <li key={srv.id}>
              {srv.name} ({srv.location})
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
