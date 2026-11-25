import { useCallback, useEffect, useState } from 'react'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'transcribe.theme'

function systemTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === 'dark' || value === 'light' ? value : null
  } catch {
    // Private mode or a policy that blocks storage: fall back to the system.
    return null
  }
}

function apply(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
}

/**
 * Dark by default, follows the system preference until the operator picks a
 * theme, then remembers that choice. The choice is applied to <html> so the
 * background paints correctly before React renders anything.
 */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(() => storedTheme() ?? systemTheme())

  useEffect(() => {
    apply(theme)
  }, [theme])

  // Follow the system while the operator has not made a choice.
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: light)')
    const onChange = () => {
      if (!storedTheme()) setTheme(media.matches ? 'light' : 'dark')
    }
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark'
      try {
        window.localStorage.setItem(STORAGE_KEY, next)
      } catch {
        // Storage is a nicety, not a requirement.
      }
      return next
    })
  }, [])

  return { theme, toggle }
}
