import type { ProjectionResult } from '../../api/types'
import { Badge } from '../../components/ui'
import { formatMoney } from '../../lib/money'

/** Percentiles of the cost per request, and what a month would cost if every request cost the p50 or p95. */
export function CostSpreadTable({ result }: { result: ProjectionResult }) {
  const requests = result.monthly_requests.toLocaleString('en-US')
  const money = (value: string | null | undefined, places?: number) => (value ? formatMoney(value, result.currency, places) : '—')
  return (
    <div className="stack-sm">
      <h4>Cost per request across the measured requests</h4>
      <p className="small muted">
        Nearest-rank percentiles over the requests with complete estimates. The monthly figures are what {requests} requests
        would cost if every request cost the p50 or the p95; they are not a forecast range.
      </p>
      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>Candidate</th>
              <th className="num">p50</th>
              <th className="num">p90</th>
              <th className="num">p95</th>
              <th className="num">Max</th>
              <th className="num wrap">Per month if every request cost the p50</th>
              <th className="num wrap">Per month if every request cost the p95</th>
              <th>Based on</th>
            </tr>
          </thead>
          <tbody>
            {result.candidates.map((item) => {
              const spread = item.cost_spread
              return (
                <tr key={item.candidate_key} data-testid="projection-spread-row">
                  <td>{item.label}</td>
                  <td className="num">{money(spread?.p50)}</td>
                  <td className="num">{money(spread?.p90)}</td>
                  <td className="num">{money(spread?.p95)}</td>
                  <td className="num">{money(spread?.max)}</td>
                  <td className="num">{money(spread?.month_if_every_request_p50, 2)}</td>
                  <td className="num">{money(spread?.month_if_every_request_p95, 2)}</td>
                  <td className="small">
                    {spread ? (
                      <>
                        <div>{spread.basis}</div>
                        {spread.sample_warning && (
                          <div>
                            <Badge tone="warn">small sample</Badge> {spread.sample_warning}
                          </div>
                        )}
                      </>
                    ) : (
                      <Badge tone="warn">no request priced completely</Badge>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
