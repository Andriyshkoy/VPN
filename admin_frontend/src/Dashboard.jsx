import { useState } from 'react'
import Servers from './Servers'
import Users from './Users'
import Configs from './Configs'

export default function Dashboard({ onLogout }) {
  const [page, setPage] = useState('servers')

  return (
    <div>
      <nav className="navbar navbar-expand-lg navbar-dark bg-dark">
        <div className="container-fluid">
          <span className="navbar-brand">Admin</span>
          <div className="navbar-nav">
            <button className="nav-link btn btn-link" onClick={() => setPage('servers')}>Servers</button>
            <button className="nav-link btn btn-link" onClick={() => setPage('users')}>Users</button>
            <button className="nav-link btn btn-link" onClick={() => setPage('configs')}>Configs</button>
            <button className="nav-link btn btn-link" onClick={() => { localStorage.removeItem('loggedIn'); onLogout() }}>Logout</button>
          </div>
        </div>
      </nav>
      {page === 'servers' && <Servers />}
      {page === 'users' && <Users />}
      {page === 'configs' && <Configs />}
    </div>
  )
}

