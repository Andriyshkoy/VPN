import { useState } from 'react'

const envUser = import.meta.env.VITE_ADMIN_USERNAME
const envPass = import.meta.env.VITE_ADMIN_PASSWORD

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    if (username === envUser && password === envPass) {
      localStorage.setItem('loggedIn', 'true')
      onLogin()
    } else {
      setError('Invalid credentials')
    }
  }

  return (
    <div className="login-container">
      <h2>Admin Login</h2>
      <form onSubmit={handleSubmit}>
        <input
          type="text"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit">Login</button>
      </form>
      {error && <div className="error">{error}</div>}
    </div>
  )
}
