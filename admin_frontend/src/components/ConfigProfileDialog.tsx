import { useEffect, useRef, useState } from 'react'
import { configsApi, type NodeProfile, type VpnConfig } from '../api'
import { Modal } from './Modal'

export function profileText(profile: NodeProfile): string {
  if (!['hysteria', 'vless-reality'].includes(profile.service_id)) throw new Error('Неизвестный протокол профиля')
  if ((profile.service_id === 'hysteria' && profile.format !== 'singbox-json') || (profile.service_id === 'vless-reality' && profile.format !== 'uri')) throw new Error('Неподдерживаемый безопасный формат профиля')
  const value = profile.format === 'singbox-json' ? profile.content : profile.format === 'uri' ? profile.uri : undefined
  if (profile.contains_secret !== true || typeof value !== 'string' || !value || value.length > 65536) throw new Error('Некорректный формат профиля')
  return value
}

export function downloadProfile(profile: NodeProfile) {
  const json = profile.format === 'singbox-json'
  const blob = new Blob([profileText(profile)], { type: json ? 'application/json;charset=utf-8' : 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `hiddify-${profile.service_id}.${json ? 'json' : 'txt'}`
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

export function ConfigProfileDialog({ config, onClose }: { config: VpnConfig; onClose: () => void }) {
  // Profile secrets stay in this mounted dialog only, outside the shared query
  // or mutation caches. Opening it does not fetch a secret automatically.
  const [profile, setProfile] = useState<NodeProfile | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const closed = useRef(false)
  useEffect(() => { closed.current = false; return () => { closed.current = true } }, [])
  const close = () => { closed.current = true; setProfile(null); onClose() }
  const load = async () => {
    setPending(true); setError('')
    try {
      const result = await configsApi.profile(String(config.id))
      profileText(result)
      if (String(result.config_id) !== String(config.id)) throw new Error('Профиль не соответствует конфигурации')
      if (!closed.current) setProfile(result)
    } catch {
      // Backend errors may include contextual information. Never echo a
      // rejected profile body or transport exception into the UI.
      if (!closed.current) setError('Не удалось получить профиль. Проверьте доступ и состояние конфигурации.')
    } finally {
      if (!closed.current) setPending(false)
    }
  }
  const copy = async () => {
    if (!profile) return
    try { await navigator.clipboard.writeText(profileText(profile)); setCopied(true) }
    catch { setError('Буфер обмена недоступен. Скопируйте содержимое вручную или скачайте файл.') }
  }
  return <Modal open title={`Профиль ${config.display_name || config.name || `#${config.id}`}`} description="Профиль содержит ключ доступа. Получение записывается в аудит; не отправляйте содержимое посторонним." onClose={close}>
    <div className="form-stack">
      {!profile && <button type="button" className="button button--primary" onClick={() => void load()} disabled={pending}>{pending ? 'Получаем…' : 'Получить профиль'}</button>}
      {profile && <>
        <p>{profile.format === 'singbox-json' ? 'JSON-профиль Hysteria для Hiddify с проверкой сертификата. Импортируйте JSON целиком, включая фигурные скобки; не заменяйте его обычной hysteria2:// ссылкой.' : 'Ссылка VLESS / REALITY. Скопируйте её целиком и импортируйте в Hiddify.'}</p>
        <label className="field"><span>{profile.format === 'singbox-json' ? 'JSON для Hiddify' : 'Ссылка подключения'}</span><textarea readOnly rows={8} value={profileText(profile)} spellCheck={false} /></label>
        <div className="form-actions"><button type="button" className="button button--secondary" onClick={() => void copy()}>{copied ? 'Скопировано' : profile.format === 'singbox-json' ? 'Скопировать JSON' : 'Скопировать ссылку'}</button><button type="button" className="button button--primary" onClick={() => downloadProfile(profile)}>{profile.format === 'singbox-json' ? 'Скачать JSON' : 'Скачать ссылку'}</button></div>
      </>}
      {error && <div className="form-error" role="alert">{error}</div>}
    </div>
  </Modal>
}
