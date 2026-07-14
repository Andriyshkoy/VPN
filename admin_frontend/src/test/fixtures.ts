export const actor = {
  actor: { id: 1, username: 'admin', role: 'owner', permissions: ['dashboard:read', 'users:read', 'balance:read', 'balance:write', 'finance:read', 'referrals:read', 'configs:read', 'configs:write', 'servers:read', 'servers:write', 'metrics:read', 'audit:read'] },
  csrf_token: 'csrf-test-token',
}

export const dashboard = {
  users: { total: 12, new: 2, with_configs: 8, paying: 5, invited: 3, delivery: { active: 11, blocked: 1 } },
  configs: { active: 9, suspended: 1 },
  finance: { cash_in: '500.00', recognized_revenue: '120.00', wallet_liability: '800.00', referral_rewards: '12.00' },
  operations: { attention: [] },
  servers: [{ id: 1, name: 'ams-1', location: 'Amsterdam', config_total: 10, config_active: 9, config_suspended: 1 }],
}

export const userPage = {
  items: [{
    id: 42,
    tg_id: 123456,
    username: 'alice',
    created_at: '2026-07-10T10:00:00Z',
    balance: '125.50',
    delivery_status: 'active',
    referrer: { id: 7, username: 'bob' },
    config_counts: { total: 2, active: 1, suspended: 1, pending: 0 },
    credited_total: '500.00',
  }],
  total: 1,
  limit: 25,
  offset: 0,
}
