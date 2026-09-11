import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Markdown from './Markdown.jsx'

// Capture-or-ask box shared by the boards. Capture persists the text before any
// classification runs, so a failing LLM never loses the input.
export default function CompactCapture({ onCaptured }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')
  const [answer, setAnswer] = useState(null)
  const [error, setError] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    const focus = () => inputRef.current?.focus()
    window.addEventListener('watson:focus-capture', focus)
    window.addEventListener('watson:focus-ask', focus)
    return () => { window.removeEventListener('watson:focus-capture', focus); window.removeEventListener('watson:focus-ask', focus) }
  }, [])

  const submit = async (kind) => {
    const value = text.trim()
    if (!value || busy) return
    setBusy(kind); setError(''); setAnswer(null)
    try {
      if (kind === 'capture') {
        await api.capture(value)
        setText('')
        onCaptured?.()
      } else {
        setAnswer(await api.ask(value))
      }
    } catch (err) { setError(err.message) } finally { setBusy('') }
  }
  const keyDown = (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault(); submit(event.shiftKey ? 'ask' : 'capture')
    }
  }

  return <section className="compact-capture" aria-label="Capture or ask Watson">
    {answer && <div className="compact-answer"><div><span>Watson says</span><button type="button" onClick={() => setAnswer(null)} aria-label="Dismiss answer">×</button></div><Markdown text={answer.answer} /></div>}
    <div className="compact-capture-row">
      <input ref={inputRef} value={text} onChange={(event) => setText(event.target.value)} onKeyDown={keyDown} disabled={Boolean(busy)} placeholder="Capture a thought or ask Watson…" />
      <button type="button" disabled={!text.trim() || Boolean(busy)} onClick={() => submit('ask')}>{busy === 'ask' ? 'Asking…' : 'Ask'}</button>
      <button type="button" className="primary" disabled={!text.trim() || Boolean(busy)} onClick={() => submit('capture')}>{busy === 'capture' ? 'Capturing…' : 'Capture'}</button>
    </div>
    <span className="compact-capture-hint">⌘↵ Capture · ⌘⇧↵ Ask</span>{error && <p className="work-inline-error" role="alert">{error}</p>}
  </section>
}
