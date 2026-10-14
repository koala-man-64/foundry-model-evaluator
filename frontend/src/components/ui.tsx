import { cloneElement, isValidElement, useEffect, useId, useRef, type ReactElement, type ReactNode } from 'react'

import { STATUS_LABELS, STATUS_TONES } from '../lib/format'

export function Badge({ tone = 'neutral', children, title }: { tone?: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${tone}`} title={title}>
      {children}
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONES[status] ?? 'neutral'}>{STATUS_LABELS[status] ?? status}</Badge>
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span role="status" aria-label={label}>
      <span className="spinner" />
    </span>
  )
}

export function ErrorMessage({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="callout error" role="alert">
      {message}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

interface DialogProps {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

/** Native <dialog> (focus trapping and Escape handling come from the browser). */
export function Dialog({ open, title, onClose, children, footer }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal()
      else dialog.setAttribute('open', '')
    }
    if (!open && dialog.open) {
      if (typeof dialog.close === 'function') dialog.close()
      else dialog.removeAttribute('open')
    }
  }, [open])
  return (
    <dialog ref={ref} onClose={onClose} onCancel={onClose} aria-label={title}>
      {open && (
        <>
          <div className="dialog-header">
            <h2>{title}</h2>
            <button type="button" className="ghost" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </div>
          <div className="dialog-body">{children}</div>
          {footer && <div className="dialog-footer">{footer}</div>}
        </>
      )}
    </dialog>
  )
}

export function JsonView({ value }: { value: unknown }) {
  return <pre>{JSON.stringify(value, null, 2)}</pre>
}

interface FieldProps {
  label: string
  hint?: ReactNode
  error?: string | null
  children: ReactNode
  htmlFor?: string
}

type ControlProps = { id?: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean }

/**
 * A labelled form control. The label names the control and the hint and error describe it, so a
 * control's accessible name stays exactly the label text.
 */
export function Field({ label, hint, error, children, htmlFor }: FieldProps) {
  const generated = useId()
  if (!isValidElement(children)) {
    return (
      <label className={`field${error ? ' invalid' : ''}`} htmlFor={htmlFor}>
        <span className="label">{label}</span>
        {children}
        {hint && <span className="hint">{hint}</span>}
        {error && <span className="error-text">{error}</span>}
      </label>
    )
  }
  const control = children as ReactElement<ControlProps>
  const id = htmlFor ?? control.props.id ?? generated
  const hintId = hint ? `${id}-hint` : undefined
  const errorId = error ? `${id}-error` : undefined
  const describedBy = [control.props['aria-describedby'], hintId, errorId].filter(Boolean).join(' ') || undefined
  return (
    <div className={`field${error ? ' invalid' : ''}`}>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      {cloneElement(control, { id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined })}
      {hint && (
        <span className="hint" id={hintId}>
          {hint}
        </span>
      )}
      {error && (
        <span className="error-text" id={errorId}>
          {error}
        </span>
      )}
    </div>
  )
}
