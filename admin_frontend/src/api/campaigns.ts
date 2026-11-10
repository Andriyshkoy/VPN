import { normalizePage, queryString, request } from './client'

export interface CampaignInput {
  name: string
  text: string
  audience: { kind: 'all' | 'selected'; user_ids: number[] }
}

export interface Campaign extends CampaignInput {
  id: number | string
  status: string
  revision: number
  delivery_paused?: boolean
  created_at?: string
  launched_at?: string | null
  completed_at?: string | null
  counts: {
    total: number
    pending: number
    queued: number
    sending: number
    delivered: number
    failed: number
    unknown: number
    skipped: number
    cancelled: number
  }
}

export interface CampaignPreview {
  campaign_id: string
  revision: number
  preview_token: string
  eligible_count: number
  skipped_count: number
  missing_count: number
  sample: { user_id: number; display: string }[]
  expires_at?: string
}

export const campaignsApi = {
  list: async (params: { limit: number; offset: number }) => normalizePage<Campaign>(await request(`/campaigns${queryString(params)}`), params.limit, params.offset),
  detail: (id: string | number) => request<Campaign>(`/campaigns/${encodeURIComponent(id)}`),
  create: (input: CampaignInput, key: string) => request<Campaign>('/campaigns', { method: 'POST', body: input, idempotencyKey: key }),
  update: (id: string | number, input: CampaignInput, revision: number, key: string) => request<Campaign>(`/campaigns/${encodeURIComponent(id)}`, { method: 'PATCH', body: { ...input, revision }, idempotencyKey: key }),
  preview: (id: string | number) => request<CampaignPreview>(`/campaigns/${encodeURIComponent(id)}/preview`, { method: 'POST', body: {} }),
  launch: (id: string | number, preview: CampaignPreview, reason: string, key: string) => request<Campaign>(`/campaigns/${encodeURIComponent(id)}/launch`, { method: 'POST', body: { preview_token: preview.preview_token, revision: preview.revision, expected_recipient_count: preview.eligible_count, confirmation: 'SEND', reason }, idempotencyKey: key }),
  cancel: (id: string | number, reason: string, key: string) => request<Campaign>(`/campaigns/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: { reason }, idempotencyKey: key }),
  retry: (id: string | number, reason: string, key: string) => request<Campaign>(`/campaigns/${encodeURIComponent(id)}/retry`, { method: 'POST', body: { reason }, idempotencyKey: key }),
}
