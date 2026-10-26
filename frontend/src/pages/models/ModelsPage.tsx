import { NavLink, Navigate, Route, Routes } from 'react-router'

import { EndpointsPanel } from './EndpointsPanel'
import { PricingPanel } from './PricingPanel'
import { ProfilesPanel } from './ProfilesPanel'
import { ProjectionsPanel } from './ProjectionsPanel'
import { SettingsPanel } from './SettingsPanel'

export function ModelsPage() {
  return (
    <div>
      {/* Absolute paths: inside the /models/* splat route a relative link resolves against the current URL. */}
      <nav className="subnav" aria-label="Models and pricing sections">
        <NavLink to="/models/endpoints">Endpoints &amp; keys</NavLink>
        <NavLink to="/models/profiles">Model profiles</NavLink>
        <NavLink to="/models/pricing">Rates</NavLink>
        <NavLink to="/models/projections">Projections</NavLink>
        <NavLink to="/models/settings">Settings</NavLink>
      </nav>
      <Routes>
        <Route index element={<Navigate to="endpoints" replace />} />
        <Route path="endpoints" element={<EndpointsPanel />} />
        <Route path="profiles" element={<ProfilesPanel />} />
        <Route path="pricing" element={<PricingPanel />} />
        <Route path="pricing/:profileId" element={<PricingPanel />} />
        <Route path="projections" element={<ProjectionsPanel />} />
        <Route path="settings" element={<SettingsPanel />} />
      </Routes>
    </div>
  )
}
