import { useEffect, useState } from 'react'
import { apiUrl, authHeaders, handleUnauthorized } from './api'

export default function Servers() {
  const empty = { name: '', ip: '', port: 22, host: '', location: '', api_key: '', monthly_cost: 0 }
  const [servers, setServers] = useState([])
  const [form, setForm] = useState(empty)
  const [error, setError] = useState('')

  const fetchServers = async () => {
    try {
      const res = await fetch(`${apiUrl}/api/servers`, {
        headers: authHeaders(),
      })
      if (handleUnauthorized(res.status)) return
      if (!res.ok) throw new Error('Failed to fetch servers')
      setServers(await res.json())
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    fetchServers()
  }, [])

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value })
  }

  const createServer = async (e) => {
    e.preventDefault()
    try {
      const res = await fetch(`${apiUrl}/api/servers`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...authHeaders(),
        },
        body: JSON.stringify({
          ...form,
          port: Number(form.port),
          monthly_cost: Number(form.monthly_cost),
        }),
      })
      if (handleUnauthorized(res.status)) return
      if (!res.ok) throw new Error('Failed to create server')
      setForm(empty)
      fetchServers()
    } catch (err) {
      setError(err.message)
    }
  }

  const deleteServer = async (id) => {
    if (!confirm('Delete server?')) return
    const res = await fetch(`${apiUrl}/api/servers/${id}`, {
      method: 'DELETE',
      headers: authHeaders(),
    })
    if (handleUnauthorized(res.status)) return
    fetchServers()
  }

  const editServer = async (srv) => {
    const name = prompt('Name', srv.name)
    if (name === null) return
    const location = prompt('Location', srv.location)
    if (location === null) return
    const host = prompt('Host', srv.host)
    if (host === null) return
    const ip = prompt('IP', srv.ip)
    if (ip === null) return
    const port = prompt('Port', srv.port)
    if (port === null) return
    const monthly_cost = prompt('monthly_cost', srv.monthly_cost)
    if (monthly_cost === null) return
    const res = await fetch(`${apiUrl}/api/servers/${srv.id}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ name, location, host, ip, port: Number(port), monthly_cost: Number(monthly_cost) }),
    })
    if (handleUnauthorized(res.status)) return
    fetchServers()
  }

  return (
    <div className="container mt-3">
      <h3>Servers</h3>
      {error && <div className="alert alert-danger">{error}</div>}
      <form className="row g-2" onSubmit={createServer}>
        <div className="col-md-2">
          <input name="name" value={form.name} onChange={handleChange} className="form-control" placeholder="Name" required />
        </div>
        <div className="col-md-2">
          <input name="ip" value={form.ip} onChange={handleChange} className="form-control" placeholder="IP" required />
        </div>
        <div className="col-md-1">
          <input name="port" value={form.port} onChange={handleChange} className="form-control" placeholder="Port" />
        </div>
        <div className="col-md-2">
          <input name="host" value={form.host} onChange={handleChange} className="form-control" placeholder="Host" required />
        </div>
        <div className="col-md-2">
          <input name="location" value={form.location} onChange={handleChange} className="form-control" placeholder="Location" required />
        </div>
        <div className="col-md-2">
          <input name="api_key" value={form.api_key} onChange={handleChange} className="form-control" placeholder="API key" required />
        </div>
        <div className="col-md-1">
          <input name="monthly_cost" value={form.monthly_cost} onChange={handleChange} className="form-control" placeholder="monthly_cost" />
        </div>
        <div className="col-md-12">
          <button className="btn btn-primary" type="submit">Add server</button>
        </div>
      </form>
      <div className="d-flex flex-wrap mt-4">
        {servers.map((srv) => (
          <div className="card m-2" style={{ minWidth: '18rem' }} key={srv.id}>
            <div className="card-body">
              <h5 className="card-title">{srv.name}</h5>
              <h6 className="card-subtitle mb-2 text-muted">{srv.location}</h6>
              <p className="card-text">
                Host: {srv.host}<br />
                IP: {srv.ip}:{srv.port}<br />
                monthly_cost: {srv.monthly_cost}
              </p>
              <button className="btn btn-sm btn-secondary me-2" onClick={() => editServer(srv)}>
                Edit
              </button>
              <button className="btn btn-sm btn-danger" onClick={() => deleteServer(srv.id)}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

