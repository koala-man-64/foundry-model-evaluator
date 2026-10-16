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

/** Position swaps: how often reversing the order moved the judge's preference to another answer. */
function swaps(group: CalibrationGroup) {
  const pairs = group.swap_pairs
  if (pairs === 0) return <span className="muted">—</span>
  return (
    <>
      {group.swap_flips} of {pairs} flipped
      <div className="small muted">
        {group.swap_consistent} of {pairs} consistent
      </div>
    </>
  )
}

function bias(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (Math.abs(value) < 0.005) return 'none'
  return `judge ${value > 0 ? 'higher' : 'lower'} by ${Math.abs(value).toFixed(2)}`
}

/** How far the judge can stand in for you: agreement with your manual scores, per judge and rubric. */
export function CalibrationPanel({ runIds, live = false }: { runIds: string[]; live?: boolean }) {
  const calibration = useCalibration(runIds, live)
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
                <th
                  className="num"
                  title="Swap passes re-judge the same answers in reversed order. A flip means the judge preferred another answer once the order changed, so its preference followed the position; consistent means it kept the same answer (or a tie)."
                >
                  Order swap
                </th>
              </tr>
            </thead>
            <tbody>
              {data.groups.map((group) => (
                <tr key={group.key}>
                  <td>
                    {group.judge}
                    <div className="small muted">{group.settings}</div>
                  </td>
                  <td>
                    {group.rubric}
                    <div className="small muted">
                      scale {group.scale_min}–{group.scale_max}
                    </div>
                  </td>
                  <td className="num">
                    {group.pairs}
                    <div className="small muted">
                      from {group.prompts} prompt{group.prompts === 1 ? '' : 's'}
                    </div>
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
                  <td className="num">{swaps(group)}</td>
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
