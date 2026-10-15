import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router'

import { ComparePage } from './pages/ComparePage'
import { HistoryPage } from './pages/HistoryPage'
import { ModelsPage } from './pages/models/ModelsPage'
import { RunPage } from './pages/RunPage'
import { RubricsPanel } from './pages/scoring/RubricsPanel'
import { ScoringPage } from './pages/ScoringPage'
import { SuiteDetailPage } from './pages/suites/SuiteDetailPage'
import { SuitesPage } from './pages/suites/SuitesPage'

function SuitesNav() {
  return (
    <nav className="subnav" aria-label="Suites and history sections">
      <NavLink to="/suites" end>
        Suites
      </NavLink>
      <NavLink to="/history">History</NavLink>
    </nav>
  )
}

function ScoringNav() {
  return (
    <nav className="subnav" aria-label="Scoring sections">
      <NavLink to="/scoring" className={({ isActive }) => (isActive ? 'active' : '')}>
        Responses
      </NavLink>
      <NavLink to="/rubrics">Rubrics</NavLink>
    </nav>
  )
}

function Area({ nav, children }: { nav: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      {nav}
      {children}
    </div>
  )
}

export function App() {
  const { pathname } = useLocation()
  const inHistoryArea = pathname.startsWith('/suites') || pathname.startsWith('/runs')
  const inScoringArea = pathname.startsWith('/rubrics')
  return (
    <>
      <header className="app-header">
        <NavLink to="/compare" className="brand">
          <img src="/favicon.svg" alt="" />
          <span>Foundry Model Evaluator</span>
        </NavLink>
        <nav className="nav" aria-label="Main">
          <NavLink to="/compare">Compare</NavLink>
          <NavLink to="/history" className={({ isActive }) => (isActive || inHistoryArea ? 'active' : '')}>
            Suites &amp; history
          </NavLink>
          <NavLink to="/scoring" className={({ isActive }) => (isActive || inScoringArea ? 'active' : '')}>
            Scoring
          </NavLink>
          <NavLink to="/models">Models &amp; pricing</NavLink>
        </nav>
      </header>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/compare" replace />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/suites" element={<Area nav={<SuitesNav />}><SuitesPage /></Area>} />
          <Route path="/suites/:suiteId" element={<Area nav={<SuitesNav />}><SuiteDetailPage /></Area>} />
          <Route path="/history" element={<Area nav={<SuitesNav />}><HistoryPage /></Area>} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/scoring" element={<Area nav={<ScoringNav />}><ScoringPage /></Area>} />
          <Route path="/scoring/:runId" element={<Area nav={<ScoringNav />}><ScoringPage /></Area>} />
          <Route path="/rubrics" element={<Area nav={<ScoringNav />}><RubricsPanel /></Area>} />
          <Route path="/models/*" element={<ModelsPage />} />
          <Route path="*" element={<Navigate to="/compare" replace />} />
        </Routes>
      </main>
    </>
  )
}
