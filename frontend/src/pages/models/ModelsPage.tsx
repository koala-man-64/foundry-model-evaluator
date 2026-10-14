import { NavLink, Navigate, Route, Routes } from 'react-router'

import { EndpointsPanel } from './EndpointsPanel'
import { PricingPanel } from './PricingPanel'
import { ProfilesPanel } from './ProfilesPanel'
import { SettingsPanel } from './SettingsPanel'
import { ProjectionsSlot } from '../../slots'

export function ModelsPage() {
  return (
    <div>
      <nav className="subnav" aria-label="Models and pricing sections">
        <NavLink to="endpoints">Endpoints &amp; keys</NavLink>
        <NavLink to="profiles">Model profiles</NavLink>
        <NavLink to="pricing">Rates</NavLink>
        <NavLink to="projections">Projections</NavLink>
        <NavLink to="settings">Settings</NavLink>
      </nav>
      <Routes>
        <Route index element={<Navigate to="endpoints" replace />} />
        <Route path="endpoints" element={<EndpointsPanel />} />
        <Route path="profiles" element={<ProfilesPanel />} />
        <Route path="pricing" element={<PricingPanel />} />
        <Route path="pricing/:profileId" element={<PricingPanel />} />
        <Route path="projections" element={<ProjectionsSlot />} />
        <Route path="settings" element={<SettingsPanel />} />
      </Routes>
    </div>
  )
}
