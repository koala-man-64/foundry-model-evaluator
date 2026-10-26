import { type ReactNode, useCallback, useId, useRef, useState } from 'react'

export interface TooltipState {
  x: number
  y: number
  content: ReactNode
}

/** Tooltip anchored to a mark, positioned relative to the chart container. */
export function useTooltip() {
  const container = useRef<HTMLDivElement>(null)
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)
  const show = useCallback((target: Element, content: ReactNode) => {
    const box = container.current?.getBoundingClientRect()
    const mark = target.getBoundingClientRect()
    if (!box) return
    setTooltip({ x: mark.left + mark.width / 2 - box.left, y: mark.top - box.top, content })
  }, [])
  const hide = useCallback(() => setTooltip(null), [])
  /** Props for a focusable, hoverable mark. */
  const markProps = (label: string, content: ReactNode) => ({
    className: 'mark',
    tabIndex: 0,
    role: 'img',
    'aria-label': label,
    onMouseEnter: (event: React.MouseEvent) => show(event.currentTarget, content),
    onFocus: (event: React.FocusEvent) => show(event.currentTarget, content),
    onMouseLeave: hide,
    onBlur: hide,
  })
  return { container, tooltip, markProps }
}

export interface TableColumn<T> {
  header: string
  value: (row: T) => ReactNode
  numeric?: boolean
}

export function ChartFrame<T>({
  title,
  subtitle,
  summary,
  legend,
  controls,
  footer,
  rows,
  columns,
  rowKey,
  children,
}: {
  title: string
  subtitle: ReactNode
  summary: string
  legend?: ReactNode
  controls?: ReactNode
  footer?: ReactNode
  rows: T[]
  columns: TableColumn<T>[]
  rowKey: (row: T) => string
  children: ReactNode
}) {
  const [table, setTable] = useState(false)
  const id = useId()
  return (
    <figure className="card stack-sm chart-frame" aria-labelledby={`${id}-title`}>
      <div className="row chart-frame-head">
        <div>
          <h3 id={`${id}-title`}>{title}</h3>
          <p className="small muted">{subtitle}</p>
        </div>
        <div className="row">
          {controls}
          <button type="button" className="small" aria-pressed={table} onClick={() => setTable((v) => !v)}>
            {table ? 'Show chart' : 'Show table'}
          </button>
        </div>
      </div>
      {legend}
      {table ? (
        <div className="table-wrap">
          <table className="compact" aria-label={`${title} (table)`}>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.header} className={c.numeric ? 'num' : undefined}>
                    {c.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={rowKey(row)}>
                  {columns.map((c) => (
                    <td key={c.header} className={c.numeric ? 'num' : undefined}>
                      {c.value(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div role="group" aria-label={summary}>
          {children}
        </div>
      )}
      {footer}
    </figure>
  )
}

export function Tooltip({ state }: { state: TooltipState | null }) {
  if (!state) return null
  return (
    <div className="chart-tooltip" role="tooltip" style={{ left: state.x, top: state.y }}>
      {state.content}
    </div>
  )
}
