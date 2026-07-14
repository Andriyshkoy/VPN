import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Search, Users } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { referralsApi, type ReferralNode } from '../api'
import { useAuth } from '../auth/AuthProvider'
import { PageHeader } from '../components/PageHeader'
import { Pagination } from '../components/Pagination'
import { EmptyState, ErrorState, PageLoading } from '../components/States'
import { formatDateTime, formatMoney, formatNumber } from '../lib/format'
import { withParam } from '../lib/search'

export function ReferralsPage() {
  const { user } = useAuth()
  const canReadUsers = user?.permissions.includes('users:read') ?? false
  const [params, setParams] = useSearchParams()
  const [queryText, setQueryText] = useState(params.get('q') ?? '')
  const offset = Number(params.get('offset') ?? 0)
  const filters = { q: params.get('q') ?? undefined, max_depth: Number(params.get('depth') ?? 2), limit: 50, offset }
  const query = useQuery({ queryKey: ['referral-tree', filters], queryFn: () => referralsApi.tree(filters), placeholderData: (previous) => previous })
  const roots = query.data?.items ?? []
  const submit = (event: FormEvent) => { event.preventDefault(); setParams(withParam(params, 'q', queryText.trim(), true)) }
  return (
    <>
      <PageHeader eyebrow="Рост" title="Реферальная сеть" description="Цепочки приглашений, оборот и начисления по каждому уровню." />
      <section className="filter-bar"><form className="search-box" role="search" onSubmit={submit}><Search /><input aria-label="Поиск в реферальной сети" value={queryText} onChange={(event) => setQueryText(event.target.value)} placeholder="Username, Telegram ID или ID" /><button className="button button--primary button--small" type="submit">Найти</button></form><label className="inline-select">Глубина <select value={filters.max_depth} onChange={(event) => setParams(withParam(params, 'depth', event.target.value, true))}><option value="1">1 уровень</option><option value="2">2 уровня</option><option value="3">3 уровня</option><option value="5">5 уровней</option></select></label></section>
      <section className="panel referral-tree-panel"><header className="panel__header"><div><h2>Дерево приглашений</h2><p>{query.data ? `${formatNumber(query.data.total)} корневых цепочек` : 'Раскройте ветку, чтобы увидеть участников цепочки'}</p></div></header>{query.isLoading ? <PageLoading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : roots.length ? <><div className="tree-table"><div className="tree-table__header"><span>Пользователь</span><span>Уровень</span><span>Пополнения</span><span>Начисления</span><span>Рефералы</span></div>{roots.map((node) => <ReferralTreeRow key={node.user_id} node={node} depth={0} canReadUsers={canReadUsers} />)}</div><Pagination offset={query.data!.offset} limit={query.data!.limit} total={query.data!.total} onChange={(nextOffset) => setParams(withParam(params, 'offset', nextOffset ? String(nextOffset) : ''))} /></> : <EmptyState title="Цепочки не найдены" description="Измените поиск или дождитесь новых приглашений." />}</section>
    </>
  )
}

function ReferralTreeRow({ node, depth, canReadUsers }: { node: ReferralNode; depth: number; canReadUsers: boolean }) {
  const [open, setOpen] = useState(depth < 1)
  const children = node.children ?? []
  const label = node.username ? `@${node.username}` : `Пользователь #${node.user_id}`
  return <><div className="tree-table__row"><span className="tree-user" style={{ paddingLeft: `${depth * 28 + 12}px` }}>{children.length ? <button type="button" className="tree-toggle" aria-label={open ? 'Свернуть ветку' : 'Раскрыть ветку'} aria-expanded={open} onClick={() => setOpen((value) => !value)}>{open ? <ChevronDown /> : <ChevronRight />}</button> : <i className="tree-spacer" />}<span className="avatar">{(node.username || String(node.user_id)).slice(0, 1).toUpperCase()}</span><span>{canReadUsers ? <Link className="primary-link" to={`/users/${node.user_id}`}>{label}</Link> : <strong>{label}</strong>}<small>{node.tg_id ? `TG ${node.tg_id} · ` : ''}{formatDateTime(node.registered_at)}</small></span></span><span><span className="level-badge">L{node.level ?? depth}</span></span><strong>{formatMoney(node.deposits_total)}</strong><strong className="money-positive">{formatMoney(node.rewards_total)}</strong><span className="ref-count"><Users size={15} />{formatNumber(node.direct_referrals ?? children.length)}</span></div>{open && children.map((child) => <ReferralTreeRow key={child.user_id} node={child} depth={depth + 1} canReadUsers={canReadUsers} />)}</>
}
