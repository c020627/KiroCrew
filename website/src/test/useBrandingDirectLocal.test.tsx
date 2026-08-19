import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

// Mock api.branding at module level — returns `direct_local: true` by default.
const brandingMock = vi.fn()

vi.mock('../api/client', () => ({
  api: { branding: (...args: unknown[]) => brandingMock(...args) },
}))

// Import the real provider + hook (after the mock is installed).
// eslint-disable-next-line import/first
import { BrandingProvider, useBranding } from '../hooks/useBranding'

function wrapper({ children }: { children: ReactNode }) {
  return <BrandingProvider>{children}</BrandingProvider>
}

describe('useBranding — directLocal mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('maps direct_local: true from the branding response to directLocal === true', async () => {
    brandingMock.mockResolvedValue({ bot_name: 'Bot', avatar: '/a.png', direct_local: true })
    const { result } = renderHook(() => useBranding(), { wrapper })

    await waitFor(() => expect(result.current.directLocal).toBe(true))
    expect(result.current.botName).toBe('Bot')
    expect(result.current.avatar).toBe('/a.png')
  })

  it('defaults directLocal to false when the response omits direct_local', async () => {
    brandingMock.mockResolvedValue({ bot_name: 'Remote', avatar: '/b.png' })
    const { result } = renderHook(() => useBranding(), { wrapper })

    await waitFor(() => expect(result.current.botName).toBe('Remote'))
    expect(result.current.directLocal).toBe(false)
  })

  it('defaults directLocal to false when the branding call fails', async () => {
    brandingMock.mockRejectedValue(new Error('network'))
    const { result } = renderHook(() => useBranding(), { wrapper })

    // The provider catches errors and keeps defaults.
    expect(result.current.directLocal).toBe(false)
    expect(result.current.botName).toBe('Kiro Crew')
  })
})
