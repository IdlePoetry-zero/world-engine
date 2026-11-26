import { useEffect, useLayoutEffect, useRef } from 'react'
import type { RefObject } from 'react'

type ModalKeyboardOptions = {
  onClose: () => void
  busy: boolean
  focusKey?: string | number
  active?: boolean
  inertBackground?: boolean
}

const focusableSelector = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  '[contenteditable="true"]',
  '[tabindex]',
].join(',')

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) => {
    if (element.tabIndex < 0 || element.closest('[hidden], [inert], [aria-hidden="true"]')) return false
    return element.getClientRects().length > 0
  })
}

function focusInitialElement(root: HTMLElement) {
  const target = root.querySelector<HTMLElement>('[data-dialog-initial-focus]')
    ?? focusableElements(root)[0]
    ?? root
  target.focus()
}

function makeBackgroundInert(dialog: HTMLElement) {
  const changed: HTMLElement[] = []
  let modalLayer = dialog.parentElement ?? dialog

  while (modalLayer.parentElement) {
    const parent = modalLayer.parentElement
    for (const sibling of Array.from(parent.children)) {
      if (sibling === modalLayer || !(sibling instanceof HTMLElement) || sibling.inert) continue
      sibling.inert = true
      changed.push(sibling)
    }
    if (parent === document.body) break
    modalLayer = parent
  }

  return () => {
    for (const element of changed) element.inert = false
  }
}

/** Keeps focus within an open modal, handles Escape, and restores focus to its opener. */
export function useModalKeyboard(
  dialogRef: RefObject<HTMLElement | null>,
  { onClose, busy, focusKey, active = true, inertBackground = true }: ModalKeyboardOptions,
) {
  const latest = useRef({ onClose, busy })
  const invokingElement = useRef<HTMLElement | null>(null)
  const previousFocusKey = useRef(focusKey)

  useLayoutEffect(() => {
    latest.current = { onClose, busy }
  })

  useLayoutEffect(() => {
    const dialog = dialogRef.current
    if (!active || !dialog) return

    invokingElement.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const restoreBackground = inertBackground ? makeBackgroundInert(dialog) : undefined
    focusInitialElement(dialog)

    return () => {
      restoreBackground?.()
      if (invokingElement.current?.isConnected && !invokingElement.current.hasAttribute('disabled')) {
        invokingElement.current.focus()
      }
    }
  }, [active, dialogRef, inertBackground])

  useLayoutEffect(() => {
    if (previousFocusKey.current === focusKey) return
    previousFocusKey.current = focusKey
    const dialog = dialogRef.current
    if (active && dialog) focusInitialElement(dialog)
  }, [active, dialogRef, focusKey])

  useEffect(() => {
    if (!active) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return

      const dialog = dialogRef.current
      if (!dialog) return

      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        if (!latest.current.busy) latest.current.onClose()
        return
      }

      if (event.key !== 'Tab') return
      const tabbable = focusableElements(dialog)
      if (tabbable.length === 0) {
        event.preventDefault()
        dialog.focus()
        return
      }

      const first = tabbable[0]
      const last = tabbable[tabbable.length - 1]
      const active = document.activeElement
      if (!dialog.contains(active)) {
        event.preventDefault()
        ;(event.shiftKey ? last : first).focus()
      } else if (event.shiftKey && (active === first || active === dialog)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || active === dialog)) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [active, dialogRef])
}
