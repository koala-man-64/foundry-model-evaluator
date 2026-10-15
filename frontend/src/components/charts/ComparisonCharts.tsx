import { useState } from 'react'

import type { Comparison } from '../../api/types'
import { LatencyBars, TokenBars } from './BarCharts'
import { CostScoreScatter } from './CostScoreScatter'

const TABS = [
  { id: 'quality', label: 'Quality vs cost' },
  { id: 'tokens', label: 'Token use' },
  { id: 'latency', label: 'Latency' },
] as const

type TabId = (typeof TABS)[number]['id']

/** Charts for a comparison: quality against cost, token use and latency. */
export function ComparisonCharts({ comparison }: { comparison: Comparison }) {
  const [tab, setTab] = useState<TabId>('quality')
  return (
    <section className="stack-sm" aria-label="Comparison charts">
      <div className="chart-tabs" role="tablist" aria-label="Chart">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-label={TABS.find((t) => t.id === tab)?.label}>
        {tab === 'quality' && <CostScoreScatter comparison={comparison} />}
        {tab === 'tokens' && <TokenBars comparison={comparison} />}
        {tab === 'latency' && <LatencyBars comparison={comparison} />}
      </div>
    </section>
  )
}
