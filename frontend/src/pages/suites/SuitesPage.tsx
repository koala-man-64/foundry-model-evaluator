import { useState } from 'react'
import { Link, useNavigate } from 'react-router'

import { useSuites } from '../../api/hooks'
import { Badge, Empty, ErrorMessage, Spinner } from '../../components/ui'
import { formatDate } from '../../lib/format'
import { SuiteEditor } from './SuiteEditor'

export function SuitesPage() {
  const navigate = useNavigate()
  const [showArchived, setShowArchived] = useState(false)
  const suites = useSuites(showArchived)
  const [creating, setCreating] = useState(false)
  if (creating) {
    return <SuiteEditor onSaved={(suite) => navigate(`/suites/${suite.id}`)} onCancel={() => setCreating(false)} />
  }
  return (
    <div className="stack">
      <div className="row-between">
        <label className="checkbox">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show archived
        </label>
        <button type="button" className="primary" onClick={() => setCreating(true)}>
          New suite
        </button>
      </div>
      {suites.isLoading && <Spinner />}
      <ErrorMessage error={suites.error} />
      {suites.data?.length === 0 && (
        <Empty>No suites yet. A suite saves prompts, models and settings so you can run them again with repeated trials.</Empty>
      )}
      {suites.data && suites.data.length > 0 && (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>Suite</th>
                <th className="num">Prompts</th>
                <th className="num">Candidates</th>
                <th className="num">Trials</th>
                <th>Automatic judging</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {suites.data.map((suite) => (
                <tr key={suite.id}>
                  <td>
                    <Link to={`/suites/${suite.id}`}>{suite.name}</Link> {suite.archived && <Badge>archived</Badge>}
                    {suite.description && <div className="small muted">{suite.description}</div>}
                  </td>
                  <td className="num">{suite.prompts.length}</td>
                  <td className="num">{suite.candidates.length}</td>
                  <td className="num">{suite.trials}</td>
                  <td>{suite.judge?.enabled ? <Badge tone="info">on</Badge> : <span className="muted">off</span>}</td>
                  <td className="nowrap small">{formatDate(suite.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
