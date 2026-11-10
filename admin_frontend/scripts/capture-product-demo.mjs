#!/usr/bin/env node
/** Local UI review only: every API request is intercepted; external traffic is denied. */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = resolve(root, 'artifacts/product-demo')
const port = Number(process.env.DEMO_PORT ?? 4178)
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('DEMO_PORT must be a local port between 1024 and 65535')
const origin = `http://127.0.0.1:${port}`
const modulePath = process.env.PLAYWRIGHT_MODULE ?? '/tmp/vpn-product-playwright/node_modules/playwright/index.mjs'
const { chromium } = await import(pathToFileURL(resolve(modulePath)).href)
const cache = resolve(process.env.HOME ?? '', '.cache/ms-playwright')
const candidates = existsSync(cache) ? (await readdir(cache)).filter((name) => /^chromium-\d+$/.test(name)).sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1])) : []
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ?? candidates.map((name) => resolve(cache, name, 'chrome-linux64/chrome')).find(existsSync)

const permissions = ['dashboard:read', 'users:read', 'balance:read', 'balance:write', 'finance:read', 'subscriptions:read', 'subscriptions:write', 'campaigns:read', 'campaigns:write', 'campaigns:send', 'configs:read', 'configs:write', 'servers:read', 'metrics:read', 'audit:read']
const plans = [
  { code: 'basic', name: 'Basic', hourly_rate_rub: '0.07', currency: 'RUB', monthly_price_rub: '50.00', monthly_estimate_rub: '50.40', services: ['hysteria'], max_sessions: 1 },
  { code: 'plus', name: 'Plus', hourly_rate_rub: '0.14', currency: 'RUB', monthly_price_rub: '100.00', monthly_estimate_rub: '100.80', services: ['hysteria', 'vless-reality'], max_sessions: null },
]
const subscriptions = [
  { ...plans[0], id: 'demo-basic', user_id: 42, plan_code: 'basic', status: 'active', version: 1, billing_paused: true, unlimited_balance: false, next_charge_at: null, accesses: [{ service_id: 'hysteria', config_id: 4201, status: 'active' }] },
  { ...plans[1], id: 'demo-plus', user_id: 57, plan_code: 'plus', status: 'active', version: 1, billing_paused: true, unlimited_balance: false, next_charge_at: null, accesses: [{ service_id: 'hysteria', config_id: 5701, status: 'active' }, { service_id: 'vless-reality', config_id: 5702, status: 'active' }] },
]
const migration = {
  read_only: true, billing_enabled: false, counts: { basic: 1, plus: 1, suspended: 1, unchanged: 1, total: 4 }, balance_changes: 0, ledger_changes: 0, unlimited_balance_changes: 0,
  items: [
    { user_id: 42, active_configs: 1, plan_code: 'basic', status: 'active', reason: 'DEMO: один активный профиль' },
    { user_id: 57, active_configs: 2, plan_code: 'plus', status: 'active', reason: 'DEMO: два активных профиля' },
    { user_id: 68, active_configs: 0, plan_code: null, status: 'suspended', reason: 'DEMO: все профили приостановлены' },
    { user_id: 79, active_configs: 0, plan_code: null, status: 'unchanged', reason: 'DEMO: активных профилей нет' },
  ],
}
const demoUser = { identity: { id: 42, username: 'demo_user', display_name: 'DEMO User', created_at: '2026-10-01T10:00:00Z', delivery_status: 'active' }, finance: { balance: '137.50', unlimited_balance: false, provider_deposits: '200.00', service_charges: '62.50', config_fees: '0.00', latest_ledger_entry_id: 1 }, configs: { total: 1, active: 1, suspended: 0 } }
const emptyCounts = { total: 0, pending: 0, queued: 0, sending: 0, delivered: 0, failed: 0, unknown: 0, skipped: 0, cancelled: 0 }
let campaign
let queuedCampaign
const apiCalls = []
const blockedRequests = []
const unexpectedRequests = []
const browserErrors = []
const shots = []
const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function mockApi(route) {
  const request = route.request()
  const path = new URL(request.url()).pathname.replace('/api/admin/v1', '')
  const method = request.method()
  apiCalls.push({ method, path })
  if (path === '/auth/me') return route.fulfill(json({ actor: { id: 1, username: 'demo_admin', display_name: 'DEMO admin', roles: ['owner'], permissions }, csrf_token: 'demo-only-not-a-real-token' }))
  if (path === '/subscriptions/plans') return route.fulfill(json({ items: plans }))
  if (path === '/subscriptions/migration/preview') return route.fulfill(json(migration))
  if (path === '/subscriptions') return route.fulfill(json({ items: subscriptions, total: 2, limit: 25, offset: 0 }))
  if (path === '/users/42') return route.fulfill(json(demoUser))
  if (path === '/users/42/subscription') return route.fulfill(json({ subscription: subscriptions[0] }))
  if (path === '/campaigns' && method === 'GET') return route.fulfill(json({ items: campaign ? [campaign] : [], total: campaign ? 1 : 0, limit: 25, offset: 0 }))
  if (path === '/campaigns' && method === 'POST') {
    campaign = { ...request.postDataJSON(), id: 'demo-local-campaign', revision: 1, status: 'draft', delivery_paused: true, counts: { ...emptyCounts }, created_at: '2026-11-09T10:00:00Z' }
    return route.fulfill(json(campaign))
  }
  if (campaign && path === `/campaigns/${campaign.id}` && method === 'GET') return route.fulfill(json(campaign))
  if (path === '/campaigns/demo-local-campaign/preview' && method === 'POST') return route.fulfill(json({ campaign_id: campaign.id, revision: campaign.revision, preview_token: 'demo-local-preview', eligible_count: 3, skipped_count: 1, missing_count: 0, sample: [{ user_id: 42, display: 'user #42' }, { user_id: 57, display: 'user #57' }, { user_id: 79, display: 'user #79' }], expires_at: '2099-01-01T00:00:00Z' }))
  if (path === '/campaigns/demo-local-campaign/launch' && method === 'POST') {
    const body = request.postDataJSON()
    if (body.confirmation !== 'SEND' || body.expected_recipient_count !== 3 || body.preview_token !== 'demo-local-preview') throw new Error('Invalid mock launch confirmation')
    campaign = { ...campaign, status: 'queued', delivery_paused: true, launched_at: '2026-11-09T10:05:00Z', counts: { total: 3, pending: 3, queued: 0, sending: 0, delivered: 0, failed: 0, unknown: 0, skipped: 0, cancelled: 0 } }
    queuedCampaign = structuredClone(campaign)
    return route.fulfill(json(campaign))
  }
  unexpectedRequests.push({ method, path })
  return route.fulfill({ status: 501, contentType: 'application/json', body: JSON.stringify({ detail: `Unmocked demo route: ${method} ${path}` }) })
}

await mkdir(output, { recursive: true })
const vite = spawn(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, ADMIN_API_PROXY: 'http://127.0.0.1:1' } })
let viteLog = ''
vite.stdout.on('data', (data) => { viteLog += data })
vite.stderr.on('data', (data) => { viteLog += data })
let browser
try {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (vite.exitCode !== null) throw new Error(`Vite failed: ${viteLog}`)
    try { if ((await fetch(origin)).ok) break } catch { /* wait for local Vite */ }
    if (attempt === 99) throw new Error(`Vite did not start: ${viteLog}`)
    await new Promise((done) => setTimeout(done, 100))
  }
  browser = await chromium.launch({ headless: true, executablePath, args: ['--no-sandbox'] })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, locale: 'ru-RU', timezoneId: 'UTC', serviceWorkers: 'block', reducedMotion: 'reduce' })
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) { blockedRequests.push(url.href); return route.abort('blockedbyclient') }
    if (url.pathname.startsWith('/api/')) return mockApi(route)
    return route.continue()
  })
  if (context.routeWebSocket) await context.routeWebSocket('**/*', (socket) => { const url = new URL(socket.url()); if (url.hostname !== '127.0.0.1' || url.port !== String(port)) { blockedRequests.push(url.href); socket.close() } else socket.connectToServer() })
  await context.addInitScript(() => {
    const addDemo = () => {
      if (!document.getElementById('local-demo-watermark')) {
        const mark = document.createElement('div')
        mark.id = 'local-demo-watermark'
        mark.textContent = 'DEMO · локальные фикстуры · отправка выключена'
        mark.setAttribute('style', 'position:fixed;z-index:2147483647;top:8px;left:50%;transform:translateX(-50%);max-width:95vw;padding:7px 12px;border:1px solid #e5ae2d;border-radius:8px;background:#fff6d7;color:#69480b;font:700 11px/1.4 sans-serif;text-align:center;pointer-events:none;box-shadow:0 2px 10px #0001')
        document.body?.append(mark)
      }
      const footer = document.querySelector('.system-pulse')
      if (footer?.textContent?.includes('Production')) footer.lastChild.textContent = 'LOCAL DEMO'
    }
    document.addEventListener('DOMContentLoaded', () => { addDemo(); new MutationObserver(addDemo).observe(document.body, { childList: true, subtree: true }) })
  })
  const page = await context.newPage()
  page.on('pageerror', (error) => browserErrors.push(error.message))
  async function shot(file, label, fullPage = true) {
    await page.getByText('DEMO · локальные фикстуры · отправка выключена', { exact: true }).waitFor()
    await page.evaluate(() => document.fonts.ready)
    const dimensions = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }))
    await page.screenshot({ path: resolve(output, file), fullPage, animations: 'disabled' })
    shots.push({ file, label, route: new URL(page.url()).pathname, ...dimensions, overflow: dimensions.document > dimensions.viewport })
  }
  await page.goto(`${origin}/subscriptions`)
  await page.getByRole('heading', { name: 'Подписки', exact: true }).waitFor()
  await page.getByText('0,14').first().waitFor()
  await shot('01-subscription-plans.png', 'Почасовые тарифы Basic / Plus; списания выключены')
  await page.getByRole('button', { name: 'Рассчитать без изменений' }).click()
  await page.getByText('Только расчёт. Списания выключены.').waitFor()
  await shot('02-migration-preview.png', 'Миграция: только чтение; кошельки, ledger и ∞ без изменений')
  await page.goto(`${origin}/users/42/subscription`)
  await page.getByRole('heading', { name: 'Подписка аккаунта' }).waitFor()
  await shot('03-user-subscription.png', 'DEMO account42: Basic и сохранённый конечный кошелёк')
  await page.getByRole('button', { name: 'Режим ∞', exact: true }).click()
  await page.getByRole('dialog', { name: 'Включить бесконечный баланс' }).waitFor()
  await page.getByLabel('Причина изменения').fill('DEMO: ручное назначение после проверки аккаунта')
  await shot('04-unlimited-balance-confirmation.png', 'Ручное включение ∞: отдельное подтверждение; конечный кошелёк сохранён', false)
  await page.getByRole('button', { name: 'Отмена', exact: true }).click()
  await page.goto(`${origin}/campaigns`)
  await page.getByLabel('Название рассылки').fill('DEMO: обновление сервиса')
  await page.getByLabel('Текст сообщения', { exact: true }).fill('DEMO — пример сообщения.\n\nПоявились два тарифа: Basic и Plus. Выпуск профилей бесплатный. Списания пока выключены.\n\nЭто локальный макет для проверки админки; сообщение никому не отправляется.')
  await page.getByRole('button', { name: 'Сохранить и проверить аудиторию' }).click()
  await page.getByText('Проверенная аудитория', { exact: true }).waitFor()
  await shot('05-campaign-reviewed-audience.png', 'Черновик, текст и проверенная аудитория; доставки ещё нет')
  await page.getByRole('button', { name: 'Проверить и запустить' }).click()
  await page.getByLabel('Введите число получателей').fill('3')
  await page.getByLabel('Для подтверждения введите SEND').fill('SEND')
  await page.getByLabel('Причина запуска').fill('DEMO: локальная проверка интерфейса без Telegram')
  await shot('06-campaign-launch-confirmation.png', 'Подтверждение SEND, количества и причины; создание очереди при выключенной доставке', false)
  await page.getByRole('button', { name: 'СОЗДАТЬ ОЧЕРЕДЬ РАССЫЛКИ', exact: true }).click()
  await page.getByRole('dialog').waitFor({ state: 'hidden' })
  await page.getByRole('progressbar', { name: 'Ход рассылки' }).scrollIntoViewIfNeeded()
  await shot('07-campaign-progress.png', 'Очередь на паузе: 3 ожидают, 0 отправлено; отдельное включение доставки здесь недоступно')
  await page.getByRole('button', { name: 'Остановить рассылку' }).click()
  await page.getByLabel('Причина действия').fill('DEMO: остановка тестового макета')
  await shot('10-campaign-cancel-confirmation.png', 'Остановка: явная причина и предупреждение о текущей доставке', false)
  await page.getByRole('button', { name: 'Вернуться', exact: true }).click()
  // Historical results are synthetic: current delivery remains paused.
  campaign = { ...campaign, id: 'demo-historical-campaign', name: 'DEMO: завершённая рассылка (история)', status: 'completed', delivery_paused: true, counts: { ...emptyCounts, total: 3, delivered: 1, failed: 1, unknown: 1 } }
  await page.goto(`${origin}/campaigns`)
  await page.getByRole('button', { name: campaign.name, exact: true }).click()
  await page.getByRole('button', { name: 'Повторить ошибки (1)' }).click()
  await page.getByLabel('Причина действия').fill('DEMO: повтор только временных ошибок')
  await shot('11-campaign-retry-confirmation.png', 'Синтетическая история: повтор ошибок исключает доставленные и неизвестные результаты; доставка на паузе', false)
  await page.getByRole('button', { name: 'Вернуться', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${origin}/subscriptions`)
  await page.getByRole('heading', { name: 'Подписки', exact: true }).waitFor()
  await page.getByText('0,14').first().waitFor()
  await shot('08-mobile-subscriptions.png', 'Мобильная ширина390: тарифы и список подписок')
  await page.goto(`${origin}/campaigns?user_id=42`)
  await page.getByLabel('Название рассылки').fill('DEMO: выбранный получатель')
  await page.getByLabel('Текст сообщения', { exact: true }).fill('DEMO: сообщение выбранному аккаунту; локальная проверка.')
  await shot('09-mobile-targeted-campaign.png', 'Мобильная ширина390: сообщение конкретному аккаунту')
  if (unexpectedRequests.length || blockedRequests.length || browserErrors.length) throw new Error(`Demo validation failed: ${JSON.stringify({ unexpectedRequests, blockedRequests, browserErrors })}`)
  await writeFile(resolve(output, 'capture-report.json'), JSON.stringify({ local_only: true, real_dispatch: false, delivery_paused: true, feature_toggles_changed: false, blocked_external_requests: blockedRequests, unexpected_api_requests: unexpectedRequests, browser_errors: browserErrors, api_calls: apiCalls, screenshots: shots }, null, 2) + '\n')
  await writeFile(resolve(output, 'fixtures.json'), JSON.stringify({ warning: 'DEMO synthetic fixture data. No real users, access keys or API tokens.', plans, subscriptions, migration, demo_user: demoUser, queued_campaign: queuedCampaign, historical_completed_campaign: campaign, historical_counts_note: 'Synthetic results from a historical example, not from this queued campaign; delivery remains paused throughout the capture.' }, null, 2) + '\n')
  console.log(JSON.stringify({ output, screenshots: shots, actual_dispatch: false }, null, 2))
} finally {
  await browser?.close()
  vite.kill('SIGTERM')
}
