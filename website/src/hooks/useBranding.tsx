import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api } from '../api/client'

interface Branding { botName: string; avatar: string; directLocal: boolean }

const defaults: Branding = { botName: 'Kiro Crew', avatar: '/logo.png', directLocal: false }
const BrandingContext = createContext<Branding>(defaults)

export function BrandingProvider({ children }: { children: ReactNode }) {
  const [b, setB] = useState<Branding>(defaults)
  useEffect(() => { api.branding().then(d => setB({ botName: d.bot_name || defaults.botName, avatar: d.avatar || defaults.avatar, directLocal: !!d.direct_local })).catch(() => {}) }, [])
  return <BrandingContext.Provider value={b}>{children}</BrandingContext.Provider>
}

export const useBranding = () => useContext(BrandingContext)
