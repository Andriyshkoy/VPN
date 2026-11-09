import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { ConfigProfileDialog, profileText } from '../components/ConfigProfileDialog'
import type { NodeProfile } from '../api'
import { server } from './server'

const content = JSON.stringify({ outbounds: [{ type: 'hysteria2', password: 'secret<&>', tls: { enabled: true, insecure: false, certificate_sha256: 'a'.repeat(64) } }] })
const profile: NodeProfile = { config_id: 15, access_id: 'access', service_id: 'hysteria', format: 'singbox-json', uri: null, content, contains_secret: true }

test('exports JSON only after explicit action, keeps secrets outside shared caches and copies exact content', async () => {
  let exports = 0
  server.use(http.get('/api/admin/v1/configs/15/profile', ({ request }) => {
    exports++
    expect(request.cache).toBe('no-store')
    return HttpResponse.json(profile)
  }))
  const client = new QueryClient()
  const user = userEvent.setup()
  const onClose = vi.fn()
  render(<QueryClientProvider client={client}><ConfigProfileDialog config={{ id: 15, service_id: 'hysteria' }} onClose={onClose} /></QueryClientProvider>)
  expect(exports).toBe(0)
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Получить профиль' }))
  expect(await screen.findByLabelText('JSON для Hiddify')).toHaveValue(content)
  expect(screen.getByRole('button', { name: 'Скачать JSON' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Скопировать ссылку' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Скопировать JSON' }))
  expect(await navigator.clipboard.readText()).toBe(content)
  expect(client.getQueryCache().getAll()).toHaveLength(0)
  expect(client.getMutationCache().getAll()).toHaveLength(0)
  expect(document.querySelector('script')).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Закрыть' }))
  expect(onClose).toHaveBeenCalledOnce()
})

test('keeps VLESS as a URI and rejects a Hysteria URI security downgrade', () => {
  const uri = 'vless://key@example.com:9443?security=reality'
  expect(profileText({ ...profile, service_id: 'vless-reality', format: 'uri', uri, content: null })).toBe(uri)
  expect(() => profileText({ ...profile, format: 'uri', content: null, uri: 'hysteria2://secret@example.com:1935?insecure=1' })).toThrow('Неподдерживаемый безопасный формат')
})

test('a rejected export never renders raw secret data from an error response', async () => {
  server.use(http.get('/api/admin/v1/configs/15/profile', () => HttpResponse.json({ detail: 'private-hysteria-password' }, { status: 503 })))
  render(<ConfigProfileDialog config={{ id: 15 }} onClose={vi.fn()} />)
  await userEvent.click(screen.getByRole('button', { name: 'Получить профиль' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось получить профиль')
  expect(screen.queryByText('private-hysteria-password')).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})
