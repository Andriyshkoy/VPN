import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'
import { actor, dashboard } from './fixtures'
import { renderApp } from './render'
import { server } from './server'

test('creates a cookie session and opens the protected dashboard', async () => {
  server.use(
    http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 })),
    http.post('/api/admin/v1/auth/login', async ({ request }) => {
      expect(await request.json()).toEqual({ username: 'admin', password: 'secret' })
      return HttpResponse.json(actor)
    }),
    http.get('/api/admin/v1/dashboard', () => HttpResponse.json(dashboard)),
  )
  renderApp(<App />, '/login')
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Логин'), 'admin')
  await user.type(screen.getByLabelText('Пароль'), 'secret')
  await user.click(screen.getByRole('button', { name: 'Войти' }))
  expect(await screen.findByRole('heading', { name: 'Обзор системы' })).toBeInTheDocument()
  expect(screen.getByText('12')).toBeInTheDocument()
})

test('redirects an expired session to login', async () => {
  server.use(http.get('/api/admin/v1/auth/me', () => HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 })))
  renderApp(<App />, '/users')
  expect(await screen.findByRole('heading', { name: 'Вход в панель' })).toBeInTheDocument()
})
