#!/usr/bin/env node
/**
 * Finalysis Visor — Real-time report display server
 * ==================================================
 *
 * Watches ../reports/*.html and pushes updates to connected browsers
 * over Server-Sent Events (SSE). The agent can also push progress
 * events during generation so the loader reflects real work instead
 * of a cosmetic timer that only plays AFTER the report is ready.
 *
 * Event-driven progress flow
 * --------------------------
 *   1. Agent calls POST /api/start with an optional phases[] list
 *      → broadcasts `generating-started` → overlay appears instantly.
 *   2. Agent calls POST /api/phase { index, substep? } at each real
 *      step transition → broadcasts `phase-update` → overlay advances.
 *   3. Agent writes the HTML file to ../reports/<slug>.html
 *      → chokidar detects it → broadcasts `report-ready` → overlay
 *      collapses and iframe swaps in the new report immediately.
 *   4. (Optional) POST /api/done snaps all phases to complete just
 *      before the file write lands — useful if the final write is
 *      nearly instant after the last phase.
 *
 * If `report-ready` arrives without a prior `generating-started`
 * (e.g., someone drops a file in reports/ manually), the client falls
 * back to a short synthetic animation so the demo still looks polished.
 *
 * Endpoints:
 *   GET  /                → Visor HTML (dark theme, iframe + spinner overlay)
 *   GET  /events          → SSE stream (all progress events + keep-alive)
 *   GET  /api/latest      → JSON with filename of most recent report
 *   GET  /api/reports     → JSON list of all reports (newest first)
 *   GET  /reports/<file>  → Serves report HTML files (static)
 *   POST /api/start       → body {phases?: Array<string|{label,substeps?}>}
 *                           broadcasts `generating-started`
 *   POST /api/phase       → body {index?: number, label?: string,
 *                                 substep?: string}
 *                           broadcasts `phase-update`
 *   POST /api/done        → broadcasts `generating-done`
 *
 * Config via env:
 *   PORT                  → default 3333
 *   REPORTS_DIR           → default ../reports (relative to this file)
 */

import express from "express";
import chokidar from "chokidar";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = parseInt(process.env.PORT || "3333", 10);
const REPORTS_DIR = path.resolve(
  __dirname,
  process.env.REPORTS_DIR || "../reports"
);

// Files to exclude from the visor (template, hidden files)
const EXCLUDED = new Set(["template.html"]);

// ─────────────────────────────────────────────────────────────
// SSE client registry
// ─────────────────────────────────────────────────────────────
const clients = new Set();

function broadcast(event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of clients) {
    try {
      res.write(payload);
    } catch {
      // client gone — cleanup happens on 'close'
    }
  }
}

// Keep-alive ping every 25s so proxies don't close the connection
setInterval(() => {
  for (const res of clients) {
    try {
      res.write(`: keep-alive ${Date.now()}\n\n`);
    } catch {}
  }
}, 25_000);

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────
function listReports() {
  if (!fs.existsSync(REPORTS_DIR)) return [];
  return fs
    .readdirSync(REPORTS_DIR)
    .filter(
      (f) =>
        f.endsWith(".html") &&
        !f.startsWith(".") &&
        !EXCLUDED.has(f)
    )
    .map((f) => {
      const full = path.join(REPORTS_DIR, f);
      return { name: f, mtime: fs.statSync(full).mtimeMs };
    })
    .sort((a, b) => b.mtime - a.mtime);
}

function latestReport() {
  const reports = listReports();
  return reports[0] || null;
}

// ─────────────────────────────────────────────────────────────
// File watcher
// ─────────────────────────────────────────────────────────────
const watcher = chokidar.watch(path.join(REPORTS_DIR, "*.html"), {
  ignoreInitial: true,
  awaitWriteFinish: {
    stabilityThreshold: 250,
    pollInterval: 50,
  },
});

function onFileEvent(filePath) {
  const name = path.basename(filePath);
  if (EXCLUDED.has(name) || name.startsWith(".")) return;
  console.log(`[visor] report ready: ${name}`);
  broadcast("report-ready", {
    name,
    mtime: Date.now(),
    url: `/reports/${encodeURIComponent(name)}`,
  });
}

watcher.on("add", onFileEvent);
watcher.on("change", onFileEvent);
watcher.on("error", (err) => console.error("[visor] watcher error:", err));

// ─────────────────────────────────────────────────────────────
// Express app
// ─────────────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: "256kb" }));

// Static: serve /reports/* directly from the reports folder
app.use(
  "/reports",
  express.static(REPORTS_DIR, {
    extensions: ["html"],
    setHeaders(res) {
      res.setHeader("Cache-Control", "no-store");
    },
  })
);

// API: latest report
app.get("/api/latest", (_req, res) => {
  const latest = latestReport();
  if (!latest) return res.json({ name: null, url: null });
  res.json({
    name: latest.name,
    url: `/reports/${encodeURIComponent(latest.name)}`,
    mtime: latest.mtime,
  });
});

// API: list all reports
app.get("/api/reports", (_req, res) => {
  res.json(listReports());
});

// API: agent hook — generation has started
app.post("/api/start", (req, res) => {
  const phases = Array.isArray(req.body?.phases) ? req.body.phases : null;
  const meta = (req.body && typeof req.body === "object") ? req.body.meta || null : null;
  console.log(`[visor] generating-started${phases ? ` (${phases.length} phases)` : ""}`);
  broadcast("generating-started", { phases, meta, t: Date.now() });
  res.json({ ok: true });
});

// API: agent hook — advance to a specific phase (and optionally a substep)
app.post("/api/phase", (req, res) => {
  const b = req.body || {};
  const out = {
    index: typeof b.index === "number" ? b.index : null,
    label: typeof b.label === "string" ? b.label : null,
    substep: typeof b.substep === "string" ? b.substep : null,
    status: typeof b.status === "string" ? b.status : "active",
    t: Date.now(),
  };
  console.log(
    `[visor] phase-update idx=${out.index ?? "?"} ${
      out.label ? `"${out.label}" ` : ""
    }${out.substep ? `· ${out.substep}` : ""}`
  );
  broadcast("phase-update", out);
  res.json({ ok: true });
});

// API: agent hook — all phases complete (optional; file-write will also
// trigger the swap). Lets the UI snap to "done" state a beat before swap.
app.post("/api/done", (_req, res) => {
  console.log(`[visor] generating-done`);
  broadcast("generating-done", { t: Date.now() });
  res.json({ ok: true });
});

// API: agent hook — generation was interrupted (barge-in, cancel, error)
// BEFORE a report landed. Distinct from /api/done, which implies success.
// The client uses this to show a transient "generación interrumpida"
// empty-state instead of the cold-boot welcome copy, so the presenter
// who just asked for a chart doesn't see "Esperando el primer reporte…"
// and think nothing happened.
app.post("/api/aborted", (req, res) => {
  const reason = (req.body && typeof req.body.reason === "string")
    ? req.body.reason
    : null;
  console.log(`[visor] generating-aborted${reason ? ` (${reason})` : ""}`);
  broadcast("generating-aborted", { reason, t: Date.now() });
  res.json({ ok: true });
});

// SSE endpoint
app.get("/events", (req, res) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  res.write("retry: 3000\n\n");
  res.write(`event: connected\ndata: ${JSON.stringify({ t: Date.now() })}\n\n`);

  clients.add(res);
  console.log(`[visor] client connected (total: ${clients.size})`);

  req.on("close", () => {
    clients.delete(res);
    console.log(`[visor] client disconnected (total: ${clients.size})`);
  });
});

// Main visor page
app.get("/", (_req, res) => {
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.send(VISOR_HTML);
});

// ─────────────────────────────────────────────────────────────
// Visor HTML (inline — no build step, no extra files)
// ─────────────────────────────────────────────────────────────
const VISOR_HTML = /* html */ `<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Finalysis Visor — Reportes en Tiempo Real</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
<style>
:root{
  --bg:#ffffff;
  --surface:#ffffff;
  --surface-2:#ffffff;
  --ink:#0b1f33;
  --ink-muted:#4a5b6e;
  --ink-faint:#94a3b3;
  --accent:#0a4f8a;
  --accent-soft:#1e88e5;
  --accent-glow:rgba(10,79,138,.14);
  --success:#2e8b57;
  --success-soft:#5cb98a;
  --rule:rgba(11,31,51,.08);
  --shadow-sm:0 1px 2px rgba(11,31,51,.04);
  --shadow-md:0 4px 16px rgba(11,31,51,.06);
  --shadow-lg:0 10px 40px rgba(11,31,51,.08);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{
  height:100%;
  background:var(--bg);
  color:var(--ink);
  font-family:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-weight:400;
  overflow:hidden;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
#report-frame{
  position:fixed;
  inset:0;
  width:100%;
  height:100%;
  border:0;
  background:var(--bg);
  transition:opacity .45s ease;
  opacity:1;
}
#report-frame.hidden{opacity:0;pointer-events:none}

.empty-state{
  position:fixed;
  inset:0;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  gap:16px;
  text-align:center;
  padding:24px;
}
.empty-state h1{
  font-family:"Instrument Serif",Georgia,serif;
  font-weight:400;
  font-size:clamp(36px,5vw,64px);
  letter-spacing:-.01em;
  color:var(--ink);
}
.empty-state p{
  color:var(--ink-muted);
  font-size:clamp(14px,1.4vw,18px);
  max-width:560px;
  line-height:1.5;
}
.empty-state.hidden{display:none}

#overlay{
  position:fixed;
  inset:0;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  gap:32px;
  background:#ffffff;
  z-index:100;
  opacity:0;
  pointer-events:none;
  transition:opacity .35s ease;
}
#overlay.visible{opacity:1;pointer-events:auto}

.spinner-wrap{
  /* Spinner removed 2026-05-13. The centered phase-pill stack is now
     the single visual cue for pipeline progress — the overlay looks
     calmer and the audience's eye stays on the labels instead of
     ping-ponging between a rotating element and the pills. Kept the
     CSS class as an empty no-op in case future diffs want to put
     something back here; the DOM element itself is gone. */
}
/* The active-pill icon (see .phase.active .dot below) still uses this
   rotation — do NOT remove the keyframes, removing them broke the
   active-pill indicator during an earlier attempt. */
@keyframes spin{to{transform:rotate(360deg)}}

/* Phase bars upsized ~40% across padding + font so the verbose
   substep line stays legible from the back of a conference room. */
.phases{
  display:flex;
  flex-direction:column;
  gap:14px;
  min-width:420px;
  max-width:min(720px,92vw);
}
.phase{
  display:flex;
  align-items:center;
  gap:18px;
  padding:17px 26px;
  border-radius:999px;
  background:#ffffff;
  border:1px solid var(--rule);
  font-size:clamp(18px,1.7vw,22px);
  color:var(--ink-faint);
  transition:all .4s ease;
  letter-spacing:.02em;
  flex-wrap:wrap;
  font-weight:500;
  box-shadow:var(--shadow-sm);
}
.phase.done{
  color:var(--ink-muted);
  border-color:rgba(46,139,87,.28);
  background:rgba(46,139,87,.06);
}
.phase.active{
  color:var(--ink);
  border-color:var(--accent);
  background:rgba(10,79,138,.06);
  box-shadow:0 6px 24px rgba(10,79,138,.14),0 1px 2px rgba(11,31,51,.04);
  transform:translateX(5px);
}
.phase .icon{
  width:24px;
  height:24px;
  display:flex;
  align-items:center;
  justify-content:center;
  flex-shrink:0;
  position:relative;
}
.phase .dot{
  width:11px;
  height:11px;
  border-radius:50%;
  background:var(--ink-faint);
  opacity:.45;
}
.phase.done .dot{
  background:var(--success);
  opacity:1;
}
.phase.active .dot{display:none}
.phase.active .icon::before{
  content:"";
  position:absolute;
  width:19px;
  height:19px;
  border-radius:50%;
  border:2px solid var(--accent);
  border-right-color:transparent;
  border-bottom-color:transparent;
  animation:spin .8s linear infinite;
}

.phase-substep{
  display:none;
  flex-basis:100%;
  margin-top:10px;
  margin-left:42px;
  padding-left:14px;
  border-left:2px solid rgba(10,79,138,.35);
  font-size:clamp(15px,1.4vw,18px);
  color:var(--ink);
  letter-spacing:.02em;
  font-weight:500;
  font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  opacity:0;
  animation:substepFade .45s ease forwards;
  word-break:break-word;
}
.phase.active .phase-substep{display:block}
.phase-substep::before{
  content:"› ";
  color:var(--accent);
  margin-right:3px;
  font-weight:600;
}
@keyframes substepFade{
  from{opacity:0;transform:translateY(-2px)}
  to{opacity:.9;transform:translateY(0)}
}

.badge{
  position:fixed;
  top:18px;
  /* 2026-05-19 — moved from right to left at user request, then
     bumped left from 18px to 4vw on the same day so the badge
     aligns horizontally with the report title ("Reporte Financiero")
     inside the iframe. The slide template uses
     'padding: 3vh 4vw' (reports/templates/financial.html § .slide),
     and the deck is body-centered with 'justify-content:center'
     and 'max-width:177.78vh' (16:9). On a 16:9-shaped window the
     deck fills the full width so the title's left edge is at exactly
     4vw — matching here gives pixel-perfect alignment. On windows
     that are wider than 16:9, the deck letterboxes horizontally so
     the title shifts inward by ½ × (visor_width - 16:9_capped_width);
     the badge stays at 4vw, which puts it 1-2vw to the left of the
     title in extreme aspect ratios. That mismatch is acceptable —
     fixing it perfectly would require JS measurement and a resize
     observer, which is overkill for a status badge.

     CRITICAL: this whole block lives inside the VISOR_HTML template
     literal (started at line ~252 with backticks). Do NOT use
     double-backtick markdown-emphasis like 'foo' here — JS reads
     two backticks as "end the outer template, then start a tagged-
     template call" and the visor crashes at runtime with
     "... is not a function" (node --check passes, runtime fails).
     Use single quotes for code emphasis inside this string. */
  left:4vw;
  padding:8px 14px;
  border-radius:999px;
  background:#ffffff;
  border:1px solid var(--rule);
  font-size:11px;
  letter-spacing:.12em;
  text-transform:uppercase;
  color:var(--ink-muted);
  z-index:50;
  display:flex;
  align-items:center;
  gap:8px;
  font-weight:600;
  user-select:none;
  opacity:.9;
  transition:opacity .2s;
  box-shadow:var(--shadow-sm);
}
.badge:hover{opacity:1}
.badge .status-dot{
  width:7px;
  height:7px;
  border-radius:50%;
  background:var(--success);
  box-shadow:0 0 6px var(--success);
  /* 2026-05-19 — slow "live" breathing animation. The shadow halo
     expands and the dot scales up by ~18% on the way out, then
     contracts back. 2.5s/cycle is just slower than a typical TV-news
     LIVE indicator (≈1.5s) so the visor feels calm rather than
     anxious; ease-in-out softens the inflection points so it looks
     organic instead of metronomic. transform: scale is GPU-composited
     so this costs effectively nothing even on integrated graphics.
     The keyframe colours use rgba(46,139,87,…) — the unpacked form
     of var(--success) (#2e8b57) — because CSS variables can't carry
     an alpha multiplier directly. */
  animation:status-dot-breathe 2.5s ease-in-out infinite;
  transform-origin:center center;
}
@keyframes status-dot-breathe{
  0%,100%{
    transform:scale(1);
    box-shadow:0 0 5px rgba(46,139,87,.55);
  }
  50%{
    transform:scale(1.18);
    box-shadow:0 0 14px rgba(46,139,87,.95);
  }
}
.badge .status-dot.reconnecting{
  background:#dc6b4f;
  box-shadow:0 0 6px #dc6b4f;
  /* No breathing while reconnecting — the colour change is the
     signal, and a static dot reads as "something's wrong" more
     clearly than a (still-pulsing) different colour. */
  animation:none;
  transform:scale(1);
}
@media (prefers-reduced-motion:reduce){
  /* Accessibility: vestibular-disorder users (and anyone with the
     OS-level "Reduce motion" toggle on) get a static dot. The
     glow halo from the static box-shadow still differentiates
     "live" from a plain green dot. */
  .badge .status-dot{animation:none;transform:scale(1);}
}
.badge #status-label{font-size:33px;line-height:1}
</style>
</head>
<body>
<iframe id="report-frame" src="about:blank" title="Reporte"></iframe>

<div class="empty-state hidden" id="empty">
  <h1 id="empty-title">Visor Finalysis</h1>
  <p id="empty-body">Esperando el primer reporte. Pídele al agente que genere un análisis y aparecerá aquí automáticamente.</p>
</div>

<div class="badge" id="badge">
  <span class="status-dot" id="status-dot"></span>
  <span id="status-label">En vivo</span>
</div>

<div id="overlay">
  <div class="phases" id="phases"></div>
</div>

<script>
(function(){
  const frame = document.getElementById('report-frame');
  const overlay = document.getElementById('overlay');
  const empty = document.getElementById('empty');
  const phasesEl = document.getElementById('phases');
  const statusDot = document.getElementById('status-dot');
  const statusLabel = document.getElementById('status-label');

  // Default phases shown when the agent doesn't send a custom list.
  // Each entry can be a string OR {label, substeps?: string[]}. Substeps are
  // optional "granular progress" text that the agent can override at runtime
  // via POST /api/phase {index, substep}.
  const DEFAULT_PHASES = [
    { label: 'Consultando Finalysis API' },
    { label: 'Transformando series temporales' },
    { label: 'Seleccionando y construyendo gráfica' },
    { label: 'Componiendo resumen ejecutivo (Sonnet)' },
    { label: 'Auditando resultados con Agente revisor' },
    { label: 'Ensamblando reporte' }
  ];

  // When the overlay is armed by a prior generating-started, report-ready
  // swaps in the report after this short fade (lets the last phase animation
  // settle). When there's NO prior start (e.g. manual file drop), we play a
  // short synthetic animation for this total duration before swapping.
  const FADE_AFTER_DONE_MS = 500;
  const FALLBACK_MIN_OVERLAY_MS = 1800;

  // ── State
  // phaseState is non-null while an overlay session is active. When null,
  // the overlay is dismissed and no progress events are tracked.
  let phaseState = null;            // {phases, activeIdx, activeSubstep}
  let fallbackTimer = null;         // auto-complete timer for manual drops
  let pendingReport = null;         // report queued during fallback animation

  function normalizePhase(p){
    if (typeof p === 'string') return { label: p, substeps: null };
    return {
      label: String(p && p.label || ''),
      substeps: Array.isArray(p && p.substeps) && p.substeps.length ? p.substeps : null
    };
  }

  function renderPhases(phases, activeIdx, activeSubstep){
    phasesEl.innerHTML = '';
    phases.forEach((phase, i) => {
      const el = document.createElement('div');
      let cls = 'phase';
      if (i < activeIdx) cls += ' done';
      else if (i === activeIdx) cls += ' active';
      el.className = cls;
      el.innerHTML = '<span class="icon"><span class="dot"></span></span>'
                   + '<span class="phase-label">' + phase.label + '</span>';
      if (i === activeIdx && activeSubstep){
        const sub = document.createElement('span');
        sub.className = 'phase-substep';
        sub.textContent = activeSubstep;
        el.appendChild(sub);
      }
      phasesEl.appendChild(el);
    });
  }

  function armOverlay(rawPhases){
    const list = (rawPhases && rawPhases.length ? rawPhases : DEFAULT_PHASES).map(normalizePhase);
    phaseState = {
      phases: list,
      activeIdx: 0,
      activeSubstep: list[0] && list[0].substeps ? list[0].substeps[0] : null
    };
    overlay.classList.add('visible');
    renderPhases(phaseState.phases, phaseState.activeIdx, phaseState.activeSubstep);
  }

  function applyPhaseUpdate(data){
    if (!phaseState) return;
    let changed = false;
    if (typeof data.index === 'number' && data.index >= 0 && data.index < phaseState.phases.length){
      if (data.index !== phaseState.activeIdx){
        phaseState.activeIdx = data.index;
        phaseState.activeSubstep = null;
        changed = true;
      }
    } else if (typeof data.label === 'string' && data.label){
      const found = phaseState.phases.findIndex(p => p.label === data.label);
      if (found >= 0 && found !== phaseState.activeIdx){
        phaseState.activeIdx = found;
        phaseState.activeSubstep = null;
        changed = true;
      }
    }
    if (typeof data.substep === 'string'){
      phaseState.activeSubstep = data.substep;
      changed = true;
    }
    if (changed){
      renderPhases(phaseState.phases, phaseState.activeIdx, phaseState.activeSubstep);
    }
  }

  function markAllDone(){
    if (!phaseState) return;
    renderPhases(phaseState.phases, phaseState.phases.length, null);
  }

  function hideOverlay(){
    overlay.classList.remove('visible');
    phaseState = null;
    if (fallbackTimer){ clearTimeout(fallbackTimer); fallbackTimer = null; }
  }

  function loadReport(url, name){
    empty.classList.add('hidden');
    const bustedUrl = url + (url.includes('?') ? '&' : '?') + '_t=' + Date.now();
    frame.classList.add('hidden');
    setTimeout(() => {
      frame.src = bustedUrl;
      frame.onload = () => frame.classList.remove('hidden');
    }, 250);
  }

  function setConnectionStatus(connected){
    if (connected){
      statusDot.classList.remove('reconnecting');
      statusLabel.textContent = 'En vivo';
    } else {
      statusDot.classList.add('reconnecting');
      statusLabel.textContent = 'Reconectando…';
    }
  }

  async function loadLatest(){
    // Manual-reload path (triggered by the "R" shortcut below). On first
    // page load we intentionally do NOT call this — the visor always starts
    // on the welcome view, and new reports arrive via the SSE 'report-ready'
    // event. This keeps stale charts from a prior session from appearing
    // when the visor tab is re-opened at the start of a demo.
    try {
      const res = await fetch('/api/latest', {cache: 'no-store'});
      const data = await res.json();
      if (data.url){
        loadReport(data.url, data.name);
      } else {
        empty.classList.remove('hidden');
      }
    } catch (err) {
      console.error('Failed to fetch latest report:', err);
      empty.classList.remove('hidden');
    }
  }

  function showWelcome(variant){
    // variant: 'welcome' (default, cold boot) or 'aborted' (a generation
    // attempt was just cancelled before a report was produced). The two
    // variants share styling but have different copy — 'aborted' tells
    // the presenter their request did NOT silently disappear.
    const title = document.getElementById('empty-title');
    const body = document.getElementById('empty-body');
    if (variant === 'aborted'){
      title.textContent = 'Generación interrumpida';
      body.textContent = 'La última solicitud se canceló antes de terminar. Pídele al agente un nuevo reporte cuando estés listo.';
    } else {
      title.textContent = 'Visor Finalysis';
      body.textContent = 'Esperando el primer reporte. Pídele al agente que genere un análisis y aparecerá aquí automáticamente.';
    }
    empty.classList.remove('hidden');
    frame.classList.add('hidden');
  }

  // Timer that reverts the 'aborted' empty state back to the default
  // welcome copy after a while, so a long demo idle doesn't leave a
  // stale "generación interrumpida" message on the screen forever.
  let abortedRevertTimer = null;
  const ABORTED_REVERT_MS = 45_000;

  function connectSSE(){
    const es = new EventSource('/events');

    es.addEventListener('connected', () => setConnectionStatus(true));
    es.onopen = () => setConnectionStatus(true);
    es.onerror = () => setConnectionStatus(false);

    // Agent signals the start of a generation session → overlay appears NOW.
    es.addEventListener('generating-started', (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data); } catch {}
      if (fallbackTimer){ clearTimeout(fallbackTimer); fallbackTimer = null; }
      if (abortedRevertTimer){ clearTimeout(abortedRevertTimer); abortedRevertTimer = null; }
      pendingReport = null;
      // Reset the post-report guard so THIS run's report-ready is
      // treated as fresh. Without this, a session with two
      // back-to-back handoffs would keep the flag from the first
      // one and mis-handle the second run's generating-done events.
      reportReadySeen = false;
      armOverlay(data.phases);
    });

    // Agent signals real progress → overlay advances (no timer drift).
    es.addEventListener('phase-update', (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data); } catch {}
      applyPhaseUpdate(data);
    });

    // Agent signals all work is done. Two possible callers:
    //  1. Happy path: render_report() finishes and fires visor.done()
    //     milliseconds before the file-watcher sees the new report.html.
    //     → markAllDone() for instant UI feedback; report-ready then
    //       swaps in the iframe and hides the overlay.
    //  2. Cancellation / barge-in: /cancel_session_tools fires visor.done()
    //     to tell us Session B is over. No report-ready will follow.
    //     → we must hide the overlay ourselves or it freezes on the last
    //       phase (e.g. "Ensamblando reporte HTML") forever.
    //
    // Strategy: always arm a dismiss timer. If report-ready fires before
    // the timer expires, it cancels the timer and runs its own swap path.
    // Once report-ready HAS fired, subsequent generating-done events are
    // no-ops — they can only come from duplicate /api/done calls (e.g.,
    // shared.py render_report AND api_server.py cancel_session_tools both
    // invoking visor.done() on the happy path) and must not re-arm the
    // dismiss timer, which would race with the already-scheduled iframe
    // swap and can leave the visor reverted to the welcome copy. See
    // 2026-05-13 incident in docs/.
    let dismissTimer = null;
    let reportReadySeen = false;
    es.addEventListener('generating-done', () => {
      markAllDone();
      // If a report has already landed (or its swap is scheduled),
      // leave the overlay logic alone. The report-ready handler
      // owns the dismiss + swap sequence.
      if (reportReadySeen) return;
      if (dismissTimer) clearTimeout(dismissTimer);
      dismissTimer = setTimeout(() => {
        dismissTimer = null;
        // If a report swap started in the meantime, phaseState is already
        // null — this is a safe no-op.
        if (phaseState) hideOverlay();
      }, FADE_AFTER_DONE_MS + 200);
    });

    // Session B was cancelled (barge-in, timeout, /cancel_session_tools)
    // BEFORE a report landed. Dismiss the overlay quickly and swap the
    // empty-state copy from the cold-boot welcome to an 'aborted' variant
    // that tells the presenter explicitly: "your last request was cut
    // short, no report was produced." Without this, users who just asked
    // for a chart see the generic "Esperando el primer reporte…" copy
    // and reasonably assume nothing happened.
    es.addEventListener('generating-aborted', () => {
      if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }
      if (abortedRevertTimer){ clearTimeout(abortedRevertTimer); abortedRevertTimer = null; }
      // Hide the overlay immediately — there's no reason to linger on
      // the last phase when we know the pipeline won't complete.
      if (phaseState) hideOverlay();
      // If no report has been loaded yet, swap the empty-state copy.
      // If a report is already visible, leave it — a prior success
      // shouldn't be replaced by an abort message for a later request.
      if (frame.classList.contains('hidden') || frame.src === 'about:blank' || !frame.src){
        showWelcome('aborted');
      }
      abortedRevertTimer = setTimeout(() => {
        abortedRevertTimer = null;
        // Revert only if still showing the aborted variant (iframe still blank).
        if (frame.classList.contains('hidden') || frame.src === 'about:blank' || !frame.src){
          showWelcome('welcome');
        }
      }, ABORTED_REVERT_MS);
    });

    // File watcher detected a new report → swap in the iframe.
    es.addEventListener('report-ready', (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data); } catch {}

      // Mark that a report has landed. Any subsequent
      // generating-done event (e.g. the redundant one from
      // cancel_session_tools after render_report already fired
      // visor.done()) becomes a no-op so it can't race with the
      // iframe swap below. See 2026-05-13 incident.
      reportReadySeen = true;

      // Cancel any generating-done dismiss timer — we'll handle the
      // overlay ourselves below (happy-path success).
      if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }

      if (phaseState){
        // We were armed — mark everything done and swap in after the fade.
        markAllDone();
        setTimeout(() => {
          hideOverlay();
          loadReport(data.url, data.name);
        }, FADE_AFTER_DONE_MS);
      } else {
        // Manual drop / legacy path — play a short fallback animation.
        pendingReport = data;
        armOverlay(null);
        markAllDone();
        fallbackTimer = setTimeout(() => {
          const r = pendingReport;
          pendingReport = null;
          hideOverlay();
          if (r) loadReport(r.url, r.name);
        }, FALLBACK_MIN_OVERLAY_MS);
      }
    });
  }

  showWelcome();
  connectSSE();

  // R = reload latest manually (useful during development)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'r' || e.key === 'R') loadLatest();
  });
})();
</script>
</body>
</html>`;

// ─────────────────────────────────────────────────────────────
// Startup
// ─────────────────────────────────────────────────────────────
if (!fs.existsSync(REPORTS_DIR)) {
  fs.mkdirSync(REPORTS_DIR, { recursive: true });
}

app.listen(PORT, () => {
  const latest = latestReport();
  console.log(`
┌─────────────────────────────────────────────────────┐
│  Finalysis Visor                                     │
│  http://localhost:${PORT}                                │
│  Watching: ${REPORTS_DIR.padEnd(40, " ").slice(0, 40)} │
│  Reports:  ${String(listReports().length).padEnd(40, " ").slice(0, 40)} │
│  Latest:   ${String(latest?.name || "—").padEnd(40, " ").slice(0, 40)} │
└─────────────────────────────────────────────────────┘
`);
});

process.on("SIGINT", () => {
  console.log("\n[visor] shutting down…");
  watcher.close();
  process.exit(0);
});
