import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router'

import { ComparePage } from './pages/ComparePage'
import { HistoryPage } from './pages/HistoryPage'
import { ModelsPage } from './pages/models/ModelsPage'
import { RunPage } from './pages/RunPage'
import { ScoringPage } from './pages/ScoringPage'
import { SuitesSlot } from './slots'

function SuitesAndHistory() {
  return (
    <div>
      <nav className="subnav" aria-label="Suites and history sections">
        <NavLink to="/suites" end>
          Suites
        </NavLink>
        <NavLink to="/history">History</NavLink>
      </nav>
      <SuitesSlot />
    </div>
  )
}

function HistoryArea() {
  return (
    <div>
      <nav className="subnav" aria-label="Suites and history sections">
        <NavLink to="/suites" end>
          Suites
        </NavLink>
        <NavLink to="/history">History</NavLink>
      </nav>
      <HistoryPage />
    </div>
  )
}

export function App() {
  const { pathname } = useLocation()
  const inHistoryArea = pathname.startsWith('/suites') || pathname.startsWith('/runs')
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
          <NavLink to="/scoring">Scoring</NavLink>
          <NavLink to="/models">Models &amp; pricing</NavLink>
        </nav>
      </header>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/compare" replace />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/suites/*" element={<SuitesAndHistory />} />
          <Route path="/history" element={<HistoryArea />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/scoring" element={<ScoringPage />} />
          <Route path="/scoring/:runId" element={<ScoringPage />} />
          <Route path="/models/*" element={<ModelsPage />} />
          <Route path="*" element={<Navigate to="/compare" replace />} />
        </Routes>
      </main>
    </>
  )
}
