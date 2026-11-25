interface ThemeToggleProps {
  theme: 'dark' | 'light'
  onToggle: () => void
}

/** Sun/moon switch; the label is text so it works without icons as well. */
export function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  const next = theme === 'dark' ? 'светлая' : 'тёмная'
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      title={`Переключить на ${next} тему`}
      aria-label={`Переключить на ${next} тему`}
    >
      <span aria-hidden="true">{theme === 'dark' ? '☾' : '☀'}</span>
      <span className="theme-toggle__label">{theme === 'dark' ? 'тёмная' : 'светлая'}</span>
    </button>
  )
}
