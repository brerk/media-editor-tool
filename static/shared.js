// shared.js — shared utilities for all tools
// API base URL: injected by server at serve time (window.__API_URL__),
// or falls back to same origin so it works locally without any config.
export const API = (window.__API_URL__ || window.location.origin).replace(/\/$/, '')

export function fmtTime(t) {
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = (t % 60).toFixed(1)
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(parseFloat(s).toFixed(1)).padStart(4,'0')}`
}

export function fmtSeconds(t) {
  const m = Math.floor(t / 60)
  const s = (t % 60).toFixed(1)
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

export async function getVideoInfo(path) {
  const r = await fetch(`${API}/info?path=${encodeURIComponent(path)}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

// ── Upload helper (used by FileUploader and direct callers) ───────────────────
export async function uploadFile(file) {
  const fd = new FormData()
  fd.append('file', file)
  const r = await fetch(`${API}/upload`, { method: 'POST', body: fd })
  if (!r.ok) {
    const msg = await r.text()
    throw new Error(`Upload failed: ${msg}`)
  }
  return r.json()
  // returns { path, filename, size, is_video, is_image, ...video_info? }
}

// ── Canvas scrubber (shared by all tools) ─────────────────────────────────
export class Scrubber {
  constructor(canvas, { onSeek, onDrag } = {}) {
    this.canvas   = canvas
    this.ctx      = canvas.getContext('2d')
    this.duration = 0
    this.current  = 0
    this.enabled  = false
    this.dragging = false
    this.hoverX   = -1
    this.onSeek   = onSeek || (() => {})
    this.onDrag   = onDrag || (() => {})
    this.marks    = []  // [{time, color, label}]

    canvas.addEventListener('mousedown', e => this._down(e))
    canvas.addEventListener('mousemove', e => this._move(e))
    canvas.addEventListener('mouseup',   () => this._up())
    canvas.addEventListener('mouseleave',() => { this.dragging = false; this.hoverX = -1; this.draw() })
  }

  setDuration(d) { this.duration = d; this.enabled = d > 0; this.draw() }
  setCurrent(t)  { this.current = t; this.draw() }
  setMarks(m)    { this.marks = m; this.draw() }

  _tToX(t) {
    const pad = 16, w = this.canvas.offsetWidth - 2*pad
    return pad + w * (t / Math.max(this.duration, 0.001))
  }
  _xToT(x) {
    const pad = 16, w = this.canvas.offsetWidth - 2*pad
    return Math.max(0, Math.min(this.duration, (x-pad)/w * this.duration))
  }

  _down(e) {
    if (!this.enabled) return
    this.dragging = true
    this.current = this._xToT(e.offsetX)
    this.onDrag(this.current)
    this.draw()
  }
  _move(e) {
    this.hoverX = e.offsetX
    if (this.dragging) {
      this.current = this._xToT(e.offsetX)
      this.onDrag(this.current)
    }
    this.draw()
  }
  _up() {
    if (this.dragging) this.onSeek(this.current)
    this.dragging = false
    this.draw()
  }

  draw() {
    const W = this.canvas.offsetWidth, H = 52
    this.canvas.width = W; this.canvas.height = H
    const ctx = this.ctx
    const pad = 16, ty = H/2
    ctx.fillStyle = '#1a1a22'; ctx.fillRect(0,0,W,H)

    if (!this.enabled) {
      ctx.fillStyle = '#7a7890'; ctx.font = '10px JetBrains Mono'
      ctx.textAlign = 'center'
      ctx.fillText('Load a video', W/2, ty+4)
      return
    }

    const dur = this.duration
    // Track
    ctx.strokeStyle = '#2e2e3e'; ctx.lineWidth = 2
    ctx.beginPath(); ctx.moveTo(pad, ty); ctx.lineTo(W-pad, ty); ctx.stroke()

    // Filled
    const hx = this._tToX(this.current)
    if (hx > pad) {
      ctx.strokeStyle = '#f7a35c'; ctx.lineWidth = 3
      ctx.beginPath(); ctx.moveTo(pad, ty); ctx.lineTo(hx, ty); ctx.stroke()
    }

    // Ticks
    ctx.font = '7px JetBrains Mono'; ctx.textAlign = 'center'
    const step = Math.max(1, Math.round(dur / 10))
    for (let t = 0; t <= dur; t += step) {
      const x = this._tToX(t)
      ctx.strokeStyle = '#2e2e3e'; ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(x, ty-4); ctx.lineTo(x, ty+4); ctx.stroke()
      ctx.fillStyle = '#7a7890'
      ctx.fillText(`${t.toFixed(0)}s`, x, ty+18)
    }

    // Custom marks (IN/OUT for trim)
    this.marks.forEach(m => {
      const mx = this._tToX(m.time)
      ctx.strokeStyle = m.color; ctx.lineWidth = 2
      ctx.setLineDash([])
      ctx.beginPath(); ctx.moveTo(mx, 4); ctx.lineTo(mx, H-4); ctx.stroke()
      if (m.label) {
        ctx.fillStyle = m.color; ctx.font = 'bold 8px JetBrains Mono'
        ctx.textAlign = 'center'
        ctx.fillText(m.label, mx, 12)
      }
    })

    // Hover
    if (this.hoverX > 0 && !this.dragging) {
      ctx.strokeStyle = '#f9c08a44'; ctx.lineWidth = 1
      ctx.setLineDash([3,3])
      ctx.beginPath(); ctx.moveTo(this.hoverX, 6); ctx.lineTo(this.hoverX, H-6); ctx.stroke()
      ctx.setLineDash([])
    }

    // Playhead
    ctx.fillStyle = this.dragging ? '#f9c08a' : '#f7a35c'
    ctx.beginPath()
    ctx.moveTo(hx-7, 4); ctx.lineTo(hx+7, 4); ctx.lineTo(hx, ty-2)
    ctx.closePath(); ctx.fill()
    ctx.strokeStyle = '#f7a35c'; ctx.lineWidth = 2
    ctx.beginPath(); ctx.moveTo(hx, ty-2); ctx.lineTo(hx, H-4); ctx.stroke()

    // Time label
    ctx.fillStyle = '#e8e6f0'; ctx.font = '9px JetBrains Mono'
    const lbl = fmtSeconds(this.current)
    const lx = Math.max(pad, Math.min(hx-16, W-pad-50))
    ctx.textAlign = 'left'
    ctx.fillText(lbl, lx, ty-10)
  }
}

// ── File uploader ─────────────────────────────────────────────────────────────
/**
 * FileUploader — replaces FileBrowser for Docker-friendly upload flow.
 *
 * Usage:
 *   const up = new FileUploader({ onVideo, onImage })
 *   up.open('video')   // or 'image' / 'any'
 *
 * Callbacks receive the server response object:
 *   { path, filename, size, is_video, is_image, ...video_info? }
 *
 * The component injects a hidden <input type="file"> and a modal drop zone
 * directly into <body>. Only one instance should exist per page.
 */
export class FileUploader {
  constructor({ onVideo, onImage, onAny } = {}) {
    this.onVideo = onVideo || (() => {})
    this.onImage = onImage || (() => {})
    this.onAny   = onAny   || (() => {})
    this.mode    = 'video'
    this._build()
  }

  _build() {
    // If DOM already exists (2nd/3rd instance), just reuse it.
    // open() sets FileUploader._active so _handleFiles uses the right callbacks.
    if (document.getElementById('fu-overlay')) {
      this._input   = document.getElementById('fu-input')
      this._overlay = document.getElementById('fu-overlay')
      return
    }

    // Hidden native file input — gives us the OS picker for free
    this._input = document.createElement('input')
    this._input.type = 'file'
    this._input.id   = 'fu-input'
    this._input.style.display = 'none'
    document.body.appendChild(this._input)
    this._input.addEventListener('change', () => {
      FileUploader._active?._handleFiles(FileUploader._active._input.files)
    })

    // Modal overlay
    this._overlay = document.createElement('div')
    this._overlay.id = 'fu-overlay'
    this._overlay.className = 'modal-overlay'
    this._overlay.innerHTML = `
      <div class="modal fu-modal">
        <div class="modal-header">
          <span class="fu-title">Upload file</span>
          <button class="flat" id="fu-close">✕</button>
        </div>
        <div class="fu-drop-zone" id="fu-drop">
          <div class="fu-icon">⬆</div>
          <div class="fu-hint">Drag &amp; drop here</div>
          <div class="fu-or">or</div>
          <button class="fu-pick-btn" id="fu-pick">Choose file</button>
          <div class="fu-accepted" id="fu-accepted"></div>
        </div>
        <div class="fu-progress" id="fu-progress" style="display:none">
          <div class="fu-bar-wrap"><div class="fu-bar" id="fu-bar"></div></div>
          <div class="fu-status" id="fu-status">Uploading…</div>
        </div>
      </div>`
    document.body.appendChild(this._overlay)

    // Inject styles once
    if (!document.getElementById('fu-styles')) {
      const s = document.createElement('style')
      s.id = 'fu-styles'
      s.textContent = `
        .fu-modal { max-width: 420px; width: 90%; }
        .fu-drop-zone {
          border: 2px dashed #3e3e52;
          border-radius: 10px;
          padding: 40px 24px;
          text-align: center;
          cursor: pointer;
          transition: border-color .2s, background .2s;
          margin: 16px 0 0;
        }
        .fu-drop-zone.drag-over {
          border-color: #f7a35c;
          background: #f7a35c11;
        }
        .fu-icon { font-size: 2.4rem; margin-bottom: 8px; opacity: .7; }
        .fu-hint { color: #c8c6d8; font-size: .9rem; }
        .fu-or   { color: #7a7890; font-size: .8rem; margin: 10px 0; }
        .fu-pick-btn {
          background: #f7a35c22;
          border: 1px solid #f7a35c66;
          color: #f7a35c;
          padding: 7px 20px;
          border-radius: 6px;
          cursor: pointer;
          font-size: .85rem;
          transition: background .2s;
        }
        .fu-pick-btn:hover { background: #f7a35c33; }
        .fu-accepted { color: #7a7890; font-size: .72rem; margin-top: 12px; }
        .fu-progress { padding: 12px 0 4px; }
        .fu-bar-wrap {
          background: #2e2e3e;
          border-radius: 4px;
          overflow: hidden;
          height: 6px;
          margin-bottom: 8px;
        }
        .fu-bar {
          height: 100%;
          background: #f7a35c;
          width: 0%;
          transition: width .15s;
        }
        .fu-status { color: #c8c6d8; font-size: .82rem; }
      `
      document.head.appendChild(s)
    }

    // Events
    document.getElementById('fu-close').onclick = () => this.close()
    document.getElementById('fu-pick').onclick  = () => this._input.click()

    const drop = document.getElementById('fu-drop')
    drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over') })
    drop.addEventListener('dragleave',() => drop.classList.remove('drag-over'))
    drop.addEventListener('drop', e => {
      e.preventDefault()
      drop.classList.remove('drag-over')
      this._handleFiles(e.dataTransfer.files)
    })
    // Clicking the drop zone also opens picker
    drop.addEventListener('click', e => {
      if (e.target.id === 'fu-pick') return  // button handles itself
      this._input.click()
    })
  }

  open(mode = 'video') {
    FileUploader._active = this   // so the shared input knows who to callback
    this.mode = mode
    // Set accepted types on the native input
    const videoExts = '.mp4,.mkv,.mov,.webm,.avi,.ts,.m4v,.flv'
    const imageExts = '.png,.jpg,.jpeg,.webp,.gif'
    this._input.accept = mode === 'image' ? imageExts
                       : mode === 'video' ? videoExts
                       : videoExts + ',' + imageExts

    document.getElementById('fu-accepted').textContent =
      mode === 'image' ? 'PNG, JPG, WEBP, GIF'
    : mode === 'video' ? 'MP4, MKV, MOV, WEBM, AVI…'
    : 'Video or image'

    document.querySelector('.fu-title').textContent =
      mode === 'image' ? 'Upload image'
    : mode === 'video' ? 'Upload video'
    : 'Upload file'

    // Reset state
    this._setProgress(false)
    this._overlay.classList.add('open')
  }

  close() {
    this._overlay.classList.remove('open')
    this._input.value = ''
  }

  async _handleFiles(files) {
    if (!files || !files.length) return
    const file = files[0]

    this._setProgress(true, 0, `Uploading ${file.name}…`)

    // Fake progress animation while XHR runs (fetch doesn't expose upload progress easily)
    let fake = 0
    const ticker = setInterval(() => {
      fake = Math.min(fake + 4, 85)
      this._setProgress(true, fake, `Uploading ${file.name}…`)
    }, 120)

    let result
    try {
      result = await uploadFile(file)
    } catch (err) {
      clearInterval(ticker)
      this._setProgress(true, 0, `❌ ${err.message}`)
      return
    }
    clearInterval(ticker)
    this._setProgress(true, 100, `✓ ${file.name} ready`)

    setTimeout(() => {
      this.close()
      if (result.is_video && (this.mode === 'video' || this.mode === 'any')) {
        this.onVideo(result)
      } else if (result.is_image && (this.mode === 'image' || this.mode === 'any')) {
        this.onImage(result)
      } else {
        this.onAny(result)
      }
    }, 400)
  }

  _setProgress(show, pct = 0, msg = '') {
    const prog = document.getElementById('fu-progress')
    const drop = document.getElementById('fu-drop')
    const bar  = document.getElementById('fu-bar')
    const status = document.getElementById('fu-status')
    if (show) {
      prog.style.display = 'block'
      drop.style.display = 'none'
      bar.style.width    = `${pct}%`
      status.textContent = msg
    } else {
      prog.style.display = 'none'
      drop.style.display = 'block'
    }
  }
}

// ── Progress watcher ──────────────────────────────────────────────────────
export function watchJob(tool, jobId, { onProgress, onDone, onError }) {
  const ws = new WebSocket(`ws://127.0.0.1:7070/ws/progress/${tool}/${jobId}`)
  ws.onmessage = e => {
    const d = JSON.parse(e.data)
    if (d.error) { onError?.(d.error); ws.close(); return }
    onProgress?.(d.progress, d.status, d.log)
    if (d.status === 'done') { onDone?.(); ws.close() }
    else if (d.status === 'error') { onError?.('Export failed'); ws.close() }
  }
  ws.onerror = async () => {
    // Fallback polling
    const poll = setInterval(async () => {
      const d = await fetch(`${API}/${tool}/job/${jobId}`).then(r=>r.json())
      onProgress?.(d.progress, d.status, d.log)
      if (d.status !== 'running') {
        clearInterval(poll)
        if (d.status === 'done') onDone?.()
        else onError?.('Export failed')
      }
    }, 500)
  }
}

// ── Log ───────────────────────────────────────────────────────────────────
export class Logger {
  constructor(el) { this.el = el }
  append(msg, level='info') {
    const ts = new Date().toLocaleTimeString('en', {hour12:false})
    const div = document.createElement('div')
    div.className = level
    div.textContent = `[${ts}] ${msg}`
    this.el.appendChild(div)
    this.el.scrollTop = this.el.scrollHeight
  }
}
