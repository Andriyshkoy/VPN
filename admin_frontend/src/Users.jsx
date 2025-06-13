import { useEffect, useState } from 'react'
import { apiUrl, authHeaders, handleUnauthorized } from './api'

export default function Users() {
  const [users, setUsers] = useState([])
  const [error, setError] = useState('')

  const fetchUsers = async () => {
    try {
      const res = await fetch(`${apiUrl}/api/users`, {
        headers: authHeaders(),
      })
      if (handleUnauthorized(res.status)) return
      if (!res.ok) throw new Error('Failed to fetch users')
      setUsers(await res.json())
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    fetchUsers()
  }, [])

  const topup = async (id) => {
    const amount = prompt('Amount to top up')
    if (!amount) return
    const res = await fetch(`${apiUrl}/api/users/${id}/topup`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ amount: Number(amount) }),
    })
    if (handleUnauthorized(res.status)) return
    fetchUsers()
  }

  const withdraw = async (id) => {
    const amount = prompt('Amount to withdraw')
    if (!amount) return
    const res = await fetch(`${apiUrl}/api/users/${id}/withdraw`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ amount: Number(amount) }),
    })
    if (handleUnauthorized(res.status)) return
    fetchUsers()
  }

  return (
    <div className="container mt-3">
      <h3>Users</h3>
      {error && <div className="alert alert-danger">{error}</div>}
      <div className="d-flex flex-wrap">
        {users.map((u) => (
          <div className="card m-2" style={{ minWidth: '18rem' }} key={u.id}>
            <div className="card-body">
              <h5 className="card-title">{u.username || 'User'} (ID {u.id})</h5>
              <p className="card-text">
                TG: {u.tg_id}<br />
                Balance: {u.balance}
              </p>
              <button className="btn btn-sm btn-success me-2" onClick={() => topup(u.id)}>
                Top up
              </button>
              <button className="btn btn-sm btn-warning" onClick={() => withdraw(u.id)}>
                Withdraw
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

