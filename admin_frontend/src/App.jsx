import { useState, useEffect } from 'react'
import './App.css'
import Login from './Login'
import Dashboard from './Dashboard'

function App() {
  const [loggedIn, setLoggedIn] = useState(false)

  useEffect(() => {
    setLoggedIn(localStorage.getItem('loggedIn') === 'true')
  }, [])

  const handleLogin = () => setLoggedIn(true)
  const handleLogout = () => setLoggedIn(false)

  return loggedIn ? (
    <Dashboard onLogout={handleLogout} />
  ) : (
    <Login onLogin={handleLogin} />
  )
}

export default App
