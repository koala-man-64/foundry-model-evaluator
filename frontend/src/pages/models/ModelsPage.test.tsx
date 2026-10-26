import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { App } from '../../App'

// The panels fetch data; only the routing between them is under test here.
vi.mock('./EndpointsPanel', () => ({ EndpointsPanel: () => 'Endpoints panel' }))
vi.mock('./ProfilesPanel', () => ({ ProfilesPanel: () => 'Profiles panel' }))
vi.mock('./PricingPanel', () => ({ PricingPanel: () => 'Pricing panel' }))
vi.mock('./ProjectionsPanel', () => ({ ProjectionsPanel: () => 'Projections panel' }))
vi.mock('./SettingsPanel', () => ({ SettingsPanel: () => 'Settings panel' }))

const TABS = [
  { name: 'Endpoints & keys', path: '/models/endpoints', panel: 'Endpoints panel' },
  { name: 'Model profiles', path: '/models/profiles', panel: 'Profiles panel' },
  { name: 'Rates', path: '/models/pricing', panel: 'Pricing panel' },
  { name: 'Projections', path: '/models/projections', panel: 'Projections panel' },
  { name: 'Settings', path: '/models/settings', panel: 'Settings panel' },
]

function Pathname() {
  return <output data-testid="pathname">{useLocation().pathname}</output>
}

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Pathname />
    </MemoryRouter>,
  )
  return within(screen.getByRole('navigation', { name: 'Models and pricing sections' }))
}

describe('Models and pricing sub-navigation', () => {
  it('opens each section from every other section', async () => {
    const user = userEvent.setup()
    const nav = renderAt('/models/endpoints')
    for (const from of TABS) {
      for (const to of TABS) {
        await user.click(nav.getByRole('link', { name: from.name }))
        await user.click(nav.getByRole('link', { name: to.name }))
        expect(screen.getByTestId('pathname')).toHaveTextContent(new RegExp(`^${to.path}$`))
        expect(screen.getByText(to.panel)).toBeInTheDocument()
        expect(nav.getByRole('link', { name: to.name })).toHaveAttribute('aria-current', 'page')
      }
    }
  })

  it('links to the same absolute paths on a nested rates page', () => {
    const nav = renderAt('/models/pricing/profile-1')
    expect(screen.getByText('Pricing panel')).toBeInTheDocument()
    expect(nav.getByRole('link', { name: 'Rates' })).toHaveAttribute('aria-current', 'page')
    for (const tab of TABS) expect(nav.getByRole('link', { name: tab.name })).toHaveAttribute('href', tab.path)
  })

  it('opens endpoints for the bare area path', () => {
    const nav = renderAt('/models')
    expect(screen.getByTestId('pathname')).toHaveTextContent(/^\/models\/endpoints$/)
    expect(nav.getByRole('link', { name: 'Endpoints & keys' })).toHaveAttribute('aria-current', 'page')
  })
})
