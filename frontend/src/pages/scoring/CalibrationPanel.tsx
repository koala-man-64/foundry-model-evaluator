import { useCalibration } from '../../api/hooks'
import type { CalibrationGroup } from '../../api/types'
import { ErrorMessage, Spinner } from '../../components/ui'

function correlation(group: CalibrationGroup, minPairs: number): string {
  if (group.spearman === null || group.spearman === undefined) {
    return group.pairs < minPairs ? `needs ${minPairs}+ pairs` : 'no variation'
  }
  const range =
    group.spearman_low !== null && group.spearman_low !== undefined && group.spearman_high !== null && group.spearman_high !== undefined
      ? ` (95% ${group.spearman_low.toFixed(2)} to ${group.spearman_high.toFixed(2)})`
      : ''
  return `${group.spearman.toFixed(2)}${range}`
}

function bias(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (Math.abs(value) < 0.005) return 'none'
  return `judge ${value > 0 ? 'higher' : 'lower'} by ${Math.abs(value).toFixed(2)}`
}

/** How far the judge can stand in for you: agreement with your manual scores, per judge and rubric. */
export function CalibrationPanel({ runIds }: { runIds: string[] }) {
  const calibration = useCalibration(runIds)
  if (calibration.isLoading) return <Spinner />
  if (calibration.error) return <ErrorMessage error={calibration.error} />
  const data = calibration.data
  if (!data) return null
  return (
    <section aria-label="Calibration against your scores" className="stack-sm">
      <h2>Judge vs. your scores</h2>
      {data.groups.length === 0 ? (
        <p className="small muted">Judge this run and score some answers by hand to see how closely the judge agrees with you.</p>
      ) : (
        <div className="table-wrap card">
          <table className="compact">
            <thead>
              <tr>
                <th>Judge</th>
                <th>Rubric</th>
                <th className="num">Answers scored by both</th>
                <th className="num" title="Spearman rank correlation between your scores and the judge's (1 = same order)">
                  Rank agreement
                </th>
                <th className="num" title="Share of answers where the judge, rescaled to 1–5, is within one point of you">
                  Within 1 point
                </th>
                <th className="num">Average difference</th>
                <th className="num" title="Evaluations where the judge preferred the answer you scored highest (unique top score only)">
                  Same top pick
                </th>
              </tr>
            </thead>
            <tbody>
              {data.groups.map((group) => (
                <tr key={`${group.judge}|${group.rubric}`}>
                  <td>{group.judge}</td>
                  <td>
                    {group.rubric}
                    <div className="small muted">
                      scale {group.scale_min}–{group.scale_max}
                    </div>
                  </td>
                  <td className="num">
                    {group.pairs}
                    {group.participant_pairs_excluded > 0 && (
                      <div className="small muted">{group.participant_pairs_excluded} left out (judge took part)</div>
                    )}
                  </td>
                  <td className="num">{correlation(group, data.min_pairs)}</td>
                  <td className="num">
                    {group.within_one === null || group.within_one === undefined ? '—' : `${Math.round(group.within_one * 100)}%`}
                  </td>
                  <td className="num">{bias(group.mean_bias)}</td>
                  <td className="num">{group.top_pick_total > 0 ? `${group.top_pick_agree} of ${group.top_pick_total}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.notes.map((note) => (
        <p key={note} className="small muted">
          {note}
        </p>
      ))}
    </section>
  )
}
