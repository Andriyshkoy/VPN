import { useEffect, useState } from 'react'
import { apiUrl, authHeaders, handleUnauthorized } from './api'

export default function Configs() {
  const [configs, setConfigs] = useState([])
  const [filters, setFilters] = useState({ server_id: '', owner_id: '', suspended: '' })
  const [error, setError] = useState('')

  const fetchConfigs = async (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    try {
      const res = await fetch(`${apiUrl}/api/configs?${qs}`, {
        headers: authHeaders(),
      })
      if (handleUnauthorized(res.status)) return
      if (!res.ok) throw new Error('Failed to fetch configs')
      setConfigs(await res.json())
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    fetchConfigs()
  }, [])

  const handleChange = (e) => {
    setFilters({ ...filters, [e.target.name]: e.target.value })
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    const params = {}
    if (filters.server_id) params.server_id = filters.server_id
    if (filters.owner_id) params.owner_id = filters.owner_id
    if (filters.suspended !== '') params.suspended = filters.suspended
    fetchConfigs(params)
  }

  return (
    <div className="container mt-3">
      <h3>Configs</h3>
      {error && <div className="alert alert-danger">{error}</div>}
      <form className="row g-2" onSubmit={handleSubmit}>
        <div className="col-md-2">
          <input name="server_id" value={filters.server_id} onChange={handleChange} className="form-control" placeholder="Server id" />
        </div>
        <div className="col-md-2">
          <input name="owner_id" value={filters.owner_id} onChange={handleChange} className="form-control" placeholder="Owner id" />
        </div>
        <div className="col-md-2">
          <select name="suspended" value={filters.suspended} onChange={handleChange} className="form-select">
            <option value="">Any</option>
            <option value="true">Suspended</option>
            <option value="false">Active</option>
          </select>
        </div>
        <div className="col-md-2">
          <button className="btn btn-primary" type="submit">Filter</button>
        </div>
      </form>
      <div className="d-flex flex-wrap mt-4">
        {configs.map((c) => (
          <div className="card m-2" style={{ minWidth: '18rem' }} key={c.id}>
            <div className="card-body">
              <h5 className="card-title">{c.name}</h5>
              <p className="card-text">
                Owner: {c.owner_id} | Server: {c.server_id}
                <br />
                Suspended: {String(c.suspended)}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

