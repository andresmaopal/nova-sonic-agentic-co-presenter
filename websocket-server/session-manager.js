/**
 * session-manager.js — NovaSonicSessionManager
 * ============================================
 *
 * One instance per browser WebSocket connection. Owns up to TWO
 * NovaSonicClient instances:
 *
 *   - Session A (presenter): full bidirectional, mic → model,
 *     model → speaker. Alive for the whole browser session.
 *
 *   - Session B (specialist): audio-OUT only, spawned on demand by
 *     Session A's `handoff_to_specialist` tool, torn down on terminator
 *     phrase / `end_session` tool / barge-in / stream error.
 *
 * Responsibilities (see ``design.md § 6``):
 *
 *   1. Multiplex speaker audio — at most ONE session's audioOutput
 *      reaches the browser at any moment.
 *   2. Always forward mic audio to Session A (Session B has no input).
 *   3. Dispatch every tool_use to POST /tool_call with a
 *      ``session_id`` tag + ``agent_id`` when session_id=B.
 *   4. React to Session A's `handoff_to_specialist` tool_result by
 *      opening Session B *after* Session A's handoff line finishes.
 *   5. React to Session B's `end_session` tool_result or its terminator
 *      phrase in textOutput by triggering handback.
 *   6. React to browser `barge_in_detected` while B is active by
 *      triggering handback (reason='barge_in').
 *   7. Run 8-minute renewal checks for both sessions independently.
 *
 * State machine:
 *
 *   IDLE → A_ACTIVE → HANDOFF_IN_PROGRESS → B_ACTIVE → A_ACTIVE → …
 *
 *                                      ┌── barge_in / end_session / terminator
 *                                      │   / B stream error / B timeout
 *                                      ▼
 *                                   handback()
 */

import { NovaSonicClient } from "./nova-sonic-client.js";

// ─────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────

export const STATE_IDLE = "IDLE";
export const STATE_A_ACTIVE = "A_ACTIVE";
export const STATE_HANDOFF_IN_PROGRESS = "HANDOFF_IN_PROGRESS";
export const STATE_B_ACTIVE = "B_ACTIVE";

export const SESSION_A = "A";
export const SESSION_B = "B";

const DEFAULT_TERMINATORS = [
  "reporte en pantalla",
  "report on screen",
  "listo, está en pantalla",
  "informe listo",
];

const HANDOFF_LINE_IDLE_DEBOUNCE_MS = 150;
const HANDOFF_LINE_IDLE_TIMEOUT_MS = 2500;
const GRACE_AFTER_END_SESSION_MS = 500;
/**
 * Grace window applied when a tool OTHER than ``end_session`` returns
 * ``trigger_handback: true``. Primary caller is ``render_report`` —
 * once the HTML report is on disk, the session manager treats the
 * successful render as an implicit end_session and schedules a
 * handback, but we wait longer than ``GRACE_AFTER_END_SESSION_MS``
 * so the specialist can finish the final narration phrase ("Reporte
 * en pantalla." / "Report on screen." etc) without being cut off.
 *
 * 3500 ms covers:
 *   - the ~500 ms tail of the Phase-4 narration ("Ensamblando el
 *     reporte"), which typically overlaps render_report,
 *   - the 1.5–2 s terminator phrase that normally precedes
 *     end_session on the happy path,
 *   - 500–1000 ms of margin so slow Sonnet tokens don't clip.
 *
 * Disarmed if the specialist DOES call end_session inside the
 * window — the 500 ms timer from end_session wins because it fires
 * sooner. Never fires after ``handback()`` ran for any other reason
 * (shutdown, barge-in, stall watchdog).
 *
 * See ``src/specialists/toolkits/shared.py::render_report`` for the
 * other half of the render-complete fast-path and the 2026-05-10
 * incident that motivated this change.
 */
const GRACE_AFTER_RENDER_COMPLETE_MS = 3500;
const RENEWAL_CHECK_INTERVAL_MS = 30000;

/**
 * Barge-in confirmation window. The browser's worklet fires a "speaking"
 * event every ~150 ms while the mic sees sustained energy, so real user
 * speech produces 3–5 hits per second. A single spike is almost always
 * noise (typing, chair squeak, post-AEC specialist echo).
 *
 * Policy: handback only if we see at least BARGE_IN_MIN_HITS within a
 * BARGE_IN_CONFIRM_WINDOW_MS rolling window. One hit → ignore.
 *
 * In big-room setups with the mic gated during Session B this policy is
 * belt-and-suspenders — the worklet is silenced at the source — but it
 * also protects us if the gate is ever bypassed or if Session A time
 * gets interrupted by a confused spike.
 *
 * See: (internal postmortem 2026-05-08) § 7 P0-#4.
 */
const BARGE_IN_CONFIRM_WINDOW_MS = 600;
const BARGE_IN_MIN_HITS = 3;

/**
 * After Session B opens, Carlos (or any specialist) is expected to emit
 * its first event (audio, text, or tool_use) within a few seconds — the
 * first tool_use for `fetch_data` normally lands in 1–2 s. If the model
 * goes silent for longer than SESSION_B_SILENT_WATCHDOG_MS we log a
 * loud diagnostic so the root cause (bad prompt, bad toolConfig,
 * missing end-of-turn signal, etc.) is obvious in logs/node.log — well
 * before Bedrock's 55 s stream timeout.
 *
 * Set to 0 to disable.
 */
const SESSION_B_SILENT_WATCHDOG_MS = 8000;

/**
 * After Session B starts producing events, it must advance through the
 * 6-tool pipeline (fetch_data → transform_data → generate_chart →
 * compose_summary → render_report → end_session). The slowest gap on
 * the happy path is compose_summary, historically ~3-5 s on Haiku and
 * up to ~10 s with a Sonnet fallback. If MORE than
 * SESSION_B_PIPELINE_STALL_MS elapses between one tool_use and the
 * next we assume the specialist has entered a narration-only loop
 * (e.g., after a FINALYSIS_ERROR it rambles instead of calling
 * end_session) and force a handback with reason="b_pipeline_stall".
 *
 * Why this matters: without this watchdog the only backstop is
 * Bedrock's own 55 s audio-idle timeout (InternalErrorCode=532),
 * which leaves the visor stuck for nearly a minute and the audience
 * staring at a frozen loader.
 *
 * Budget history:
 *   • 15 s  — set 2026-05-08 (jets-stuck-loader postmortem §5/§7 P1).
 *             Sized for single-symbol compose_summary 3-5 s with some
 *             headroom.
 *   • 25 s  — raised 2026-05-13 after live-demo TSLA-vs-NVDA run
 *             (compose_summary hit exactly 15 008 ms and got killed
 *             at the 15 000 ms boundary). Multi-series work from
 *             Change A/B adds legitimate load: (a) longer
 *             SUMMARY_SYSTEM, (b) ~3× richer context JSON
 *             (stats.series nested dict), (c) Sonnet fallback when
 *             Haiku drifts by ±1 bullet. Worst realistic case is
 *             Haiku ~8 s + Sonnet fallback ~8 s = 16 s; 25 s gives
 *             ~9 s real headroom and still flags genuine stalls
 *             well before Bedrock's 55 s idle timeout.
 *
 * Re-armed on every Session B tool_use event. Disarmed on handback /
 * shutdown. Set to 0 to disable.
 */
const SESSION_B_PIPELINE_STALL_MS = 25000;

/**
 * After a Session B tool returns a TERMINAL error (see
 * TERMINAL_B_ERROR_CODES below), the specialist's prompt instructs it
 * to say ONE short apology sentence and call end_session immediately.
 * In practice models sometimes narrate the error but forget
 * end_session, so we can't rely on the 6-tool happy path completing.
 *
 * SESSION_B_FAST_ERROR_MS is a SHORT backstop timer (much shorter than
 * SESSION_B_PIPELINE_STALL_MS) that fires soon after the terminal
 * error landed. It gives Carlos just enough time to narrate "Termino."
 * (typically <1 s on Nova Sonic) and then forces a handback with
 * reason="b_error", carrying the structured error context (code +
 * message + tool_name) into Nova's HANDBACK_NOTICE so Nova can tell
 * the presenter specifically what failed.
 *
 * Why this matters: before this watchdog, a BAD_ARGS error meant the
 * user waited 15 s (SESSION_B_PIPELINE_STALL_MS) with the mic gated
 * and the visor loader spinning before regaining control. With this
 * fast-error path, the mic unlocks and Nova takes over in ~3 s —
 * the time it takes Carlos's error sentence to finish playing.
 *
 * Armed by _dispatchToolUse() when tag=B + ok=false + code ∈
 * TERMINAL_B_ERROR_CODES. Disarmed if:
 *   - Carlos calls end_session (normal graceful path, let it flow)
 *   - Carlos recovers and calls another tool (shouldn't happen per
 *     the prompt, but defensive)
 *   - handback() fires for any reason (cleanup)
 *   - shutdown()
 *
 * Set to 0 to disable (falls back to pipeline-stall-only behavior).
 *
 * See (internal postmortem 2026-05-10).
 */
const SESSION_B_FAST_ERROR_MS = 3000;

/**
 * Error codes a Session B tool may return that are TERMINAL — there
 * is no recovery path inside this handoff, the specialist must
 * apologise briefly and yield. Matches the code set emitted by
 * src/specialists/toolkits/financial.py and src/specialists/toolkits/shared.py
 * for the known error modes.
 *
 * The session manager uses this set to recognize that the pipeline
 * WILL NOT progress further in this handoff, so it arms the fast-
 * error watchdog (SESSION_B_FAST_ERROR_MS) instead of waiting for
 * the full pipeline-stall watchdog to fire.
 *
 * NOT terminal (not in this set): codes like RATE_LIMITED on a
 * RETRYABLE tool where the specialist legitimately could call again.
 * Currently there are no such codes in the codebase — every listed
 * error ends the pipeline. But keeping this a narrow set (rather
 * than "any ok=false") means a future optional-retry code won't
 * accidentally trigger premature handback.
 */
const TERMINAL_B_ERROR_CODES = new Set([
  "BAD_ARGS",            // Carlos chose wrong tool inputs (400-class from Finalysis, or local validation)
  "FINALYSIS_ERROR",     // Finalysis service error (5xx, network)
  "SUMMARY_ERROR",       // Sonnet returned bad/no bullets
  "TRANSFORM_ERROR",     // Haiku failed to transform raw → chart shape
  "HANDLE_NOT_FOUND",    // Carlos passed a stale/invalid handle
  "EMPTY_TRANSFORM",     // Transform produced 0 rows (bad ticker / no data)
  "CHART_ERROR",         // AntV MCP failed to render the chart
  "RENDER_ERROR",        // report-render step failed (bad URL, bad template)
  "RATE_LIMITED",        // RPS limit on any external API
  "DISPATCH_ERROR",      // tool_call failed end-to-end (Python down, network)
  "CANCELLED",           // tool was cancelled (e.g. /cancel_session_tools)
  "INTERNAL_ERROR",      // generic python-side exception surfaced by the dispatcher
]);

/**
 * Staged rollout flag for the HANDBACK_BRIEF redesign (2026-05-11).
 *
 * When enabled, ``handback()`` sends Session A a structured digest
 * of Carlos's run (chart URL, 3-5 bullets, stats, customer, ticker,
 * window) IN ADDITION TO the legacy HANDBACK_NOTICE sentence-shaped
 * directive. Nova's prompt teaches her to parse the BRIEF and either
 * offer a narrated summary of the report or, on the failure branch,
 * propose a corrected re-handoff.
 *
 * Gated behind an env flag so the capability can land ahead of the
 * Nova prompt changes without user-visible effect, and can be
 * switched off instantly if a live demo surfaces an issue. The
 * capture path (``_lastBBrief`` in ``_dispatchToolUse``) is always
 * active because it's free — only the injection is gated.
 *
 * Set ``NOVA_HANDBACK_BRIEF=1`` (or any of 1/true/yes/on, case-
 * insensitive) to enable. Default off.
 */
const HANDBACK_BRIEF_ENABLED = /^(1|true|yes|on)$/i.test(
  String(process.env.NOVA_HANDBACK_BRIEF || "").trim(),
);

// ─────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────

/**
 * Case-insensitive substring match for terminator phrases.
 * @param {string} text
 * @param {string[]} terminators (lowercase)
 * @returns {string|null} the matched phrase or null
 */
export function matchTerminator(text, terminators) {
  if (!text) return null;
  const lower = String(text).toLowerCase();
  for (const phrase of terminators) {
    if (lower.includes(phrase)) return phrase;
  }
  return null;
}

/**
 * Map a Session B error code to a short natural-language hint the
 * session manager can paste into a HANDBACK_NOTICE for Nova. Each
 * hint is phrased as a DIRECTIVE ("di que …") so Nova's es-419
 * paraphrasing stays grounded and consistent across handoffs.
 *
 * Keeping this mapping on the Node side (not in the Python toolkit)
 * means the user-visible narration is one short hop from the code —
 * no network roundtrip, no template-render — which matters when
 * we're trying to bring Carlos→Nova handoff latency below 3 s.
 *
 * Codes not listed here fall through to a generic "error técnico"
 * sentence; keep this set aligned with TERMINAL_B_ERROR_CODES.
 *
 * @param {string} code
 * @returns {string} a sentence-sized directive for Nova (ends with ".")
 */
function _errorHintForNova(code) {
  switch (code) {
    case "BAD_ARGS":
      return "di que la consulta tenía parámetros inválidos (símbolo, fecha o ventana) y no se pudo traer los datos.";
    case "FINALYSIS_ERROR":
      return "di que Finalysis (la fuente de datos) no respondió bien.";
    case "SUMMARY_ERROR":
      return "di que el resumen ejecutivo no se pudo generar.";
    case "TRANSFORM_ERROR":
      return "di que los datos llegaron pero no se pudieron formatear para el gráfico.";
    case "HANDLE_NOT_FOUND":
      return "di que hubo un error interno entre pasos del análisis.";
    case "EMPTY_TRANSFORM":
      return "di que no había datos suficientes para ese ticker/periodo.";
    case "CHART_ERROR":
      return "di que el gráfico no se pudo generar.";
    case "RENDER_ERROR":
      return "di que el reporte no se pudo armar.";
    case "RATE_LIMITED":
      return "di que alcanzamos el límite de consultas por minuto y hay que esperar.";
    case "DISPATCH_ERROR":
      return "di que hubo un error técnico al llamar al analista.";
    case "CANCELLED":
      return "di que la solicitud se canceló antes de terminar.";
    case "INTERNAL_ERROR":
      return "di que hubo un error interno inesperado.";
    default:
      return "di brevemente que hubo un error y no se pudo completar el análisis.";
  }
}

/**
 * Escape a string for safe inclusion in a RegExp. Used when building
 * the assistant-name terminator so names like "Nova." or "C3PO" (with
 * regex special chars) don't break the pattern.
 */
export function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Look inside a tool_result envelope ({ result: {...} }) for signal flags.
 */
export function signalsHandoffReady(env) {
  return Boolean(env?.result?.handoff_ready);
}

export function triggersHandback(env) {
  return Boolean(env?.result?.trigger_handback);
}

/**
 * Turn a tool_use input object into a compact one-line preview suitable
 * for log output. We explicitly select the fields most useful for
 * triage (kind/indicator/symbol/dates for fetch_data, target/handle for
 * transform_data, agent_id/query for handoff_to_specialist) and fall
 * back to a size-capped JSON stringify for anything else.
 *
 * Logging the raw input verbatim would include Haiku transform prompts
 * and the full opaque-handle strings — noisy and not useful. This
 * preview is optimised for the "what did Carlos ask for?" question.
 *
 * Returns an empty string if input is falsy or the preview doesn't
 * yield anything informative; the caller skips emitting `input=` in
 * that case to avoid `input=` with no value.
 *
 * See (internal postmortem 2026-05-09) § RC-3.
 */
export function _previewToolInput(input) {
  if (!input || typeof input !== "object") return "";

  // Known-important keys per tool. Order matters — we render in this
  // order for visual consistency across log lines.
  const interestingKeys = [
    "agent_id",
    "kind",
    "indicator",
    "symbol",
    "target",
    "start_date",
    "end_date",
    "window",
    "slug",
    "customer_name",
    "query",
    "direction",
    "action",
    "reason",
  ];

  const picked = [];
  for (const key of interestingKeys) {
    if (!(key in input)) continue;
    const value = input[key];
    if (value === null || value === undefined || value === "") continue;
    let rendered;
    if (typeof value === "string") {
      // Trim long strings (queries, descriptions) to 80 chars.
      rendered = value.length > 80 ? `${value.slice(0, 77)}...` : value;
      rendered = JSON.stringify(rendered);
    } else {
      rendered = JSON.stringify(value);
    }
    picked.push(`${key}=${rendered}`);
  }

  if (picked.length === 0) {
    // Fall back to generic JSON stringify, capped at 200 chars.
    try {
      const s = JSON.stringify(input);
      return s.length > 200 ? `${s.slice(0, 197)}...` : s;
    } catch {
      return "";
    }
  }

  return `{${picked.join(",")}}`;
}

/**
 * Build Session B's initial user textInput — the one turn that seeds the
 * specialist with the presenter's query and display-name preference.
 */
export function buildSessionBInitialTextInput({ query, customer, todayIso }) {
  const d = todayIso || new Date().toISOString().slice(0, 10);
  return [
    `OBLIGATORIO: Habla UNA oración de Fase 0 EN VOZ ALTA antes de llamar fetch_data. Si no hablas primero, la audiencia no escuchará nada.`,
    `La consulta del presentador es: ${query}`,
    `Nombre a mostrar: ${customer || "(derivar del símbolo)"}`,
    `Fecha actual: ${d}`,
  ].join("\n");
}

// ─────────────────────────────────────────────────────────────────────
// HANDBACK_BRIEF helpers
// ─────────────────────────────────────────────────────────────────────

/**
 * Size limits for the HANDBACK_BRIEF payload. Keeping the whole
 * message under ~1.2 KB means Nova Sonic processes it as a single
 * short context injection (one turn, no model slowdown). Each field's
 * cap is generous enough to preserve meaning — bullets trimmed to
 * 200 chars each, descriptions to 200, chart URLs to 160 (long enough
 * for the AntV signed URLs we see in practice), and the final
 * rendered payload hard-capped at 1400 chars as a belt-and-braces
 * defence against a runaway field.
 */
const BRIEF_MAX_BULLET_CHARS = 200;
const BRIEF_MAX_DESCRIPTION_CHARS = 200;
const BRIEF_MAX_CUSTOMER_CHARS = 120;
const BRIEF_MAX_CHART_URL_CHARS = 160;
const BRIEF_MAX_CHART_TITLE_CHARS = 80;
const BRIEF_MAX_ATTEMPTED_INPUT_CHARS = 220;
const BRIEF_MAX_TOTAL_CHARS = 1400;
const BRIEF_MAX_BULLETS = 5;

/**
 * Shallow-copy + prune a Carlos tool input for the BRIEF's
 * ``attempted`` block. We keep only the fields that are actionable
 * for a corrected re-handoff (ticker, kind, indicator, window,
 * dates, extra_params) and drop anything the LLM shouldn't see
 * (handles, free-form narrative). Keys are preserved verbatim so
 * Nova's prompt can read them with predictable names.
 *
 * Exported (via ``@internal`` convention) only for unit tests.
 */
export function _cloneToolInputForBrief(input) {
  if (!input || typeof input !== "object") return {};
  const whitelist = [
    "kind", "indicator", "symbol", "start_date", "end_date",
    "window", "target", "agent_id", "query", "direction",
  ];
  const out = {};
  for (const k of whitelist) {
    const v = input[k];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "string") {
      out[k] = v.length > 80 ? v.slice(0, 77) + "..." : v;
    } else {
      out[k] = v;
    }
  }
  return out;
}

/**
 * Append the slice of the BBrief contributed by one successful
 * Session B tool. All assignments are pure in-memory string/number
 * copies — no I/O, no awaits. Mutates ``brief`` in place.
 *
 * Kept separate from ``_dispatchToolUse`` to keep the dispatcher
 * readable and so every tool's captured shape is visible at a glance.
 *
 * Exported only for unit tests.
 */
export function _capturePipelineSlice(brief, toolName, toolInput, result) {
  if (!brief) return;
  const r = result || {};
  const i = toolInput || {};
  switch (toolName) {
    case "fetch_data":
      brief.fetch = {
        ticker: i.symbol,
        kind: i.kind,
        indicator: i.indicator,
        window: i.window,
        start_date: i.start_date,
        end_date: i.end_date,
        count: r.count,
        summary: r.summary,
      };
      break;
    case "generate_chart":
      brief.chart = {
        url: r.chart_url,
        tool: r.tool_used,
        title: i.title,
        axis_x: i.axis_x_title,
        axis_y: i.axis_y_title,
      };
      break;
    case "compose_summary":
      brief.summary = {
        bullets: Array.isArray(r.bullets) ? r.bullets.slice(0) : null,
        stats: r.stats || null,
        customer_name: r.customer_name,
        description: r.description,
      };
      break;
    case "render_report":
      brief.report = {
        path: r.path,
        slug: r.slug,
        customer_name: r.customer_name,
        description: r.description,
        chart_url: r.chart_url,
        chart_title: r.chart_title,
        bullets: Array.isArray(r.bullets) ? r.bullets.slice(0) : null,
        report_date: r.report_date,
      };
      break;
    // end_session carries no new data — no-op on purpose.
    default:
      break;
  }
}

/** Cap a string to ``max`` chars, appending an ellipsis if truncated. */
function _cap(s, max) {
  if (!s) return "";
  const str = String(s);
  return str.length <= max ? str : str.slice(0, Math.max(0, max - 1)) + "…";
}

/**
 * Render the HANDBACK_BRIEF system-text payload for Session A.
 *
 * Two paths, selected by what the pipeline actually achieved:
 *
 *   path=success — used when Carlos produced a report AND the cancel
 *     response confirmed reached_render. Nova's prompt teaches her
 *     to either offer a narrated summary (paraphrasing 2–3 bullets
 *     in her own voice) or, if the room has moved on, stay silent.
 *     Uses the ``report`` slice when present (every field Nova needs
 *     to speak about the chart is echoed from render_report), falling
 *     back to the ``summary`` slice if render returned early or was
 *     replayed from cache.
 *
 *   path=failure — used on every other path. Carries Carlos's last
 *     terminal error code, the exact ``attempted`` input, and a
 *     short hint Nova can narrate. Nova's prompt reads the
 *     ``attempted`` block to decide whether to propose a corrected
 *     re-handoff.
 *
 * Returns ``null`` when there is nothing meaningful to say — e.g.
 * the handback fired on barge_in before any Session B tool ran.
 * Callers treat ``null`` as "don't inject a brief, fall back to the
 * legacy HANDBACK_NOTICE alone".
 *
 * Keeps the payload under BRIEF_MAX_TOTAL_CHARS via per-field caps
 * and a final guard at the bottom. The format is a plain
 * newline-delimited key=value block — no JSON, no Markdown — so
 * Nova Sonic parses it without any prompting tricks.
 *
 * Exported only for unit tests.
 */
export function _formatHandbackBrief({
  reason, reachedRender, lastBBrief, lastBError,
} = {}) {
  if (!lastBBrief) return null;
  const isSuccess = reachedRender === true
    && lastBBrief.report
    && lastBBrief.report.chart_url;

  const pipelineMs = lastBBrief.opened_at
    ? Math.max(0, Date.now() - lastBBrief.opened_at)
    : null;

  const lines = [];
  lines.push("HANDBACK_BRIEF v1");
  // 2026-05-12 anti-hallucination: ``status=REPORT_READY`` is the
  // MOST salient signal to Nova that the success path applies. Placed
  // above ``path=`` so it's the first semantic token her LLM parses.
  // Observed failure mode it fixes: Nova's LLM occasionally misread
  // ``reason=terminator`` (a graceful-completion marker in our jargon)
  // as a termination/abort signal and produced "the specialist had an
  // error" responses after a clean handback. See
  // 2026-05-12-nova-false-error-after-success postmortem.
  if (isSuccess) {
    lines.push("status=REPORT_READY");
  }
  lines.push(`path=${isSuccess ? "success" : "failure"}`);
  if (lastBBrief.agent_id) lines.push(`agent_id=${lastBBrief.agent_id}`);
  if (pipelineMs != null) lines.push(`pipeline_ms=${pipelineMs}`);
  // Rename ``terminator`` → ``completed`` on success so the field
  // reads naturally ("reason=completed" ≈ "finished cleanly") without
  // introducing the ambiguous "terminator" word that an LLM can
  // conflate with failure. Other reasons (b_error, b_timeout, etc.)
  // pass through unchanged — they're already unambiguously abnormal.
  if (reason) {
    const displayReason = isSuccess && reason === "terminator"
      ? "completed"
      : reason;
    lines.push(`reason=${displayReason}`);
  }
  // fresh_report=true is the structural signal to Nova that a NEW
  // report just landed on the visor in this handback. The prompt
  // rule says: when you see this, your VERY NEXT utterance MUST be
  // the one-sentence offer to narrate. It's a harder gate than the
  // legacy HANDBACK_NOTICE (a soft directive prone to ASK-ONCE guard
  // mis-fires and DIRECT SILENCE fallback swallowing the offer).
  // Added 2026-05-12 as part of the anti-hallucination fix: same
  // handback that populates ``current_report`` on the Python side
  // must flip this flag on the Node side, so both halves of the
  // grounding loop move together.
  if (isSuccess) {
    lines.push("fresh_report=true");
  }
  lines.push("");

  if (isSuccess) {
    const rep = lastBBrief.report || {};
    const sum = lastBBrief.summary || {};
    const fet = lastBBrief.fetch || {};
    if (fet.ticker) lines.push(`ticker=${fet.ticker}`);
    const spec = [
      fet.kind && `kind=${fet.kind}`,
      fet.indicator && `indicator=${fet.indicator}`,
      fet.window && `window=${fet.window}`,
    ].filter(Boolean).join(" ");
    if (spec) lines.push(spec);
    if (fet.start_date && fet.end_date) {
      const pts = fet.count ? ` (${fet.count} points)` : "";
      lines.push(`range=${fet.start_date}..${fet.end_date}${pts}`);
    }
    if (rep.customer_name) {
      lines.push(`customer=${_cap(rep.customer_name, BRIEF_MAX_CUSTOMER_CHARS)}`);
    }
    if (rep.description) {
      lines.push(`description=${_cap(rep.description, BRIEF_MAX_DESCRIPTION_CHARS)}`);
    }
    if (rep.chart_title) {
      lines.push(`chart_title=${_cap(rep.chart_title, BRIEF_MAX_CHART_TITLE_CHARS)}`);
    }
    if (rep.chart_url) {
      lines.push(`chart_url=${_cap(rep.chart_url, BRIEF_MAX_CHART_URL_CHARS)}`);
    }
    // stats block — one line, compact. Skip if no useful numbers.
    const stats = sum.stats || {};
    const statsBits = [
      stats.first_value != null && `first=${stats.first_value}`,
      stats.last_value != null && `last=${stats.last_value}`,
      stats.high != null && `high=${stats.high}`,
      stats.low != null && `low=${stats.low}`,
      stats.pct_change != null && `pct_change=${stats.pct_change}%`,
      stats.count != null && `points=${stats.count}`,
    ].filter(Boolean);
    if (statsBits.length) {
      lines.push("");
      lines.push("stats:");
      lines.push("  " + statsBits.join(" "));
    }
    // bullets — prefer the summary slice (already validated 3-5);
    // fall back to the report slice if compose_summary was cached
    // from a previous handoff or stats-only payload.
    const bullets = Array.isArray(sum.bullets) && sum.bullets.length
      ? sum.bullets
      : (Array.isArray(rep.bullets) ? rep.bullets : []);
    if (bullets.length) {
      lines.push("");
      lines.push("bullets:");
      bullets.slice(0, BRIEF_MAX_BULLETS).forEach((b, i) => {
        lines.push(`  ${i + 1}. ${_cap(b, BRIEF_MAX_BULLET_CHARS)}`);
      });
    }
  } else {
    // Failure branch. ``lastBError`` is the canonical source of
    // truth when present (captured inside _dispatchToolUse on any
    // terminal code). When the handback fired for a non-error reason
    // (barge_in, b_timeout) but the pipeline still hadn't reached
    // render, we fall back to whatever the brief captured in
    // ``attempted``.
    if (lastBError) {
      lines.push("failure:");
      lines.push(`  tool=${lastBError.tool_name}`);
      lines.push(`  code=${lastBError.code}`);
      if (lastBError.message) {
        lines.push(`  detail=${_cap(lastBError.message, BRIEF_MAX_DESCRIPTION_CHARS)}`);
      }
    } else {
      lines.push("failure:");
      lines.push(`  reason=${reason || "unknown"}`);
    }
    const att = lastBBrief.attempted;
    if (att) {
      lines.push("");
      lines.push("attempted:");
      lines.push(`  tool=${att.tool}`);
      const inputStr = _cap(
        JSON.stringify(att.input || {}),
        BRIEF_MAX_ATTEMPTED_INPUT_CHARS,
      );
      lines.push(`  input=${inputStr}`);
    }
  }

  let rendered = lines.join("\n");
  if (rendered.length > BRIEF_MAX_TOTAL_CHARS) {
    rendered = rendered.slice(0, BRIEF_MAX_TOTAL_CHARS - 1) + "…";
  }
  return rendered;
}

// ─────────────────────────────────────────────────────────────────────
// NovaSonicSessionManager
// ─────────────────────────────────────────────────────────────────────

/**
 * @typedef {object} SpecialistConfig
 * @property {string} voiceId        Nova Sonic voice for Session B.
 * @property {string} systemPrompt   Session B's full system prompt.
 * @property {Array}  toolDefs       Session B's toolConfiguration.tools[].
 * @property {string[]} terminators  Lowercase substring phrases.
 * @property {string} [locale]
 * @property {string} [displayName]
 */

export class NovaSonicSessionManager {
  /**
   * @param {object} opts
   * @param {import("ws").WebSocket} opts.ws        Browser WebSocket.
   * @param {string} opts.pythonUrl                 e.g. http://127.0.0.1:8000
   * @param {string} opts.region                    AWS region.
   * @param {string} opts.voiceIdA                  Session A voice.
   * @param {string} opts.systemPromptA             Session A system prompt.
   * @param {Array}  opts.toolDefsA                 Session A tool specs.
   * @param {string} [opts.modelId]                 Defaults to Nova Sonic v1.
   * @param {string} [opts.assistantName]           Session A's assistant
   *     name (e.g. "Nova"). If set, Session B will handback gracefully
   *     the moment it utters this name as a whole word — a clean yield
   *     signal when Carlos decides to return the floor. Word-bounded so
   *     it never false-matches inside "innovación", "supernova", etc.
   * @param {Record<string, SpecialistConfig>} [opts.specialists]
   *     Per-agent_id config used when opening Session B. Loaded by server.js
   *     at startup from GET /registry/{id}.
   * @param {(opts: object) => NovaSonicClient} [opts.clientFactory]
   *     Test hook — overrides the NovaSonicClient constructor.
   */
  constructor(opts) {
    this.ws = opts.ws;
    this.pythonUrl = opts.pythonUrl;
    this.region = opts.region;
    this.voiceIdA = opts.voiceIdA;
    this.systemPromptA = opts.systemPromptA;
    this.toolDefsA = opts.toolDefsA;
    this.modelId = opts.modelId || "amazon.nova-2-sonic-v1:0";
    this.specialists = opts.specialists || {};
    this._clientFactory = opts.clientFactory || ((o) => new NovaSonicClient(o));

    // Assistant name terminator: word-bounded regex so "Nova" matches
    // "Nova," / "Nova?" / " Nova." but NOT "innovación" / "supernova".
    // Disabled if opts.assistantName is falsy.
    this.assistantName = opts.assistantName || null;
    this._assistantNameRegex = this.assistantName
      ? new RegExp(`\\b${escapeRegExp(this.assistantName)}\\b`, "i")
      : null;

    /** @type {NovaSonicClient|null} */
    this.sessionA = null;
    /** @type {NovaSonicClient|null} */
    this.sessionB = null;

    /**
     * Guard against infinite auto-restart loops: we only try ONCE per
     * WebSocket lifetime. Reset on clean session_end / shutdown.
     * @type {boolean}
     */
    this._aRestartAttempted = false;

    /** @type {"A"|"B"|null} */
    this.activeSession = null;

    /** Gag flag — when true, Session A's audioOutput is dropped. */
    this.aSpeakerGagged = false;

    /** Monotonic counter for correlating logs across a handoff. */
    this._handoffCounter = 0;
    this._currentHandoffId = null;
    this._currentAgentId = null;
    this._bOpenedAt = null;
    this._openingB = false;

    this.state = STATE_IDLE;

    this._renewalTimer = null;
    this._sessionBWatchdogTimer = null;
    // Separate from the silent-watchdog above: the stall-watchdog fires
    // when B *has* started emitting events but then stops calling tools
    // (the "rambling specialist" failure mode). Re-armed after every B
    // tool_use; disarmed on handback / shutdown.
    this._sessionBStallWatchdogTimer = null;
    // Fast-error watchdog — a SHORT timer (SESSION_B_FAST_ERROR_MS)
    // armed when a Session B tool returns an ok=false + terminal code.
    // Gives Carlos a few seconds to say "termino." then force-handbacks
    // with structured error context so Nova can narrate what failed.
    // Disarmed on any of: end_session tool_use (graceful path wins),
    // another tool_use (specialist recovered, re-arm stall watchdog),
    // handback(), shutdown().
    this._sessionBFastErrorTimer = null;
    /**
     * Captured context from the last Session B tool that returned
     * ok=false + terminal code. Used by handback() to build a
     * HANDBACK_NOTICE with specific error details for Nova.
     * Shape: { code, message, tool_name, input_preview, at_ms } | null
     * Cleared on handoff open and on handback complete.
     * @type {{code:string,message:string,tool_name:string,input_preview:string,at_ms:number}|null}
     */
    this._lastBError = null;
    /**
     * Structured digest accumulated across Carlos's pipeline. Every
     * Session B tool that returns ok=true contributes a slice here
     * (see ``_dispatchToolUse`` — all captures are passive, zero
     * extra I/O). On handback, ``_formatHandbackBrief`` turns this
     * into a human-readable ``HANDBACK_BRIEF`` the session manager
     * injects into Session A alongside the existing
     * ``HANDBACK_NOTICE`` directive. Nova's prompt teaches her to
     * parse it and either offer a narrated summary of the report or
     * (on the failure branch) propose a corrected re-handoff.
     *
     * Shape (success, populated progressively):
     *
     *   {
     *     agent_id:    string,
     *     opened_at:   number,
     *     fetch?:      {ticker, kind, indicator, window, start_date,
     *                    end_date, count, summary},
     *     chart?:      {url, tool, title, axis_x, axis_y},
     *     summary?:    {bullets:string[], stats:object,
     *                    customer_name, description},
     *     report?:     {path, slug, customer_name, description,
     *                    chart_url, chart_title, bullets, report_date},
     *     attempted?:  {tool, input, at_ms}   // only on failure branch
     *   }
     *
     * Reset to ``null`` at the top of every new handoff (see
     * ``openSessionBAfterHandoffLine``), so a leftover digest from
     * a prior run can never colour the next one.
     *
     * @type {object|null}
     */
    this._lastBBrief = null;
    this._backgroundTasks = new Set();
    // Timestamp (ms) before which `barge_in_detected` events are ignored.
    // Set when a handback fires so multiple worklet VAD hits for the same
    // utterance don't each try to handback (race-free because everything
    // here runs on the Node event loop).
    this._bargeInCooldownUntil = 0;
    // Rolling list of recent barge_in_detected timestamps, used to
    // require sustained voice activity (≥ BARGE_IN_MIN_HITS within
    // BARGE_IN_CONFIRM_WINDOW_MS) before actually triggering handback.
    // Reset on each handback and on state transitions away from B.
    this._bargeInHits = [];

    // toolUseId dedup. Nova Sonic's SPECULATIVE + FINAL generation
    // stages occasionally emit the same tool_use twice per turn; the
    // second one arrives ~ms later with the same toolUseId. Without
    // this guard the second copy runs through _dispatchToolUse again,
    // adding an extra Python round-trip and an extra sendToolResult.
    // Map<toolUseId, firstSeenMs>; GC'd on size growth.
    this._seenToolUseIds = new Map();
    this._toolUseIdTtlMs = 30_000;
  }

  // ── lifecycle ─────────────────────────────────────────────

  /**
   * Open Session A. Call once per browser session after session_start.
   */
  async startSessionA() {
    if (this.sessionA) return;
    this.sessionA = this._clientFactory({
      region: this.region,
      voiceId: this.voiceIdA,
      modelId: this.modelId,
    });
    await this.sessionA.startSession(this.systemPromptA, this.toolDefsA);
    this.activeSession = SESSION_A;
    this.aSpeakerGagged = false;
    this.state = STATE_A_ACTIVE;
    this._armRenewalTimer();
    this._spawnBackground("proc-A", this._processSessionEvents(SESSION_A));
    console.log("[session-mgr] Session A opened (voice=%s)", this.voiceIdA);
  }

  /**
   * Handle a browser WS message (binary audio or JSON control).
   */
  async handleBrowserMessage(data, isBinary) {
    if (isBinary) {
      // Mic → Session A only. Session B is audio-OUT.
      if (this.sessionA?.isActive) {
        try {
          const base64Audio = Buffer.from(data).toString("base64");
          this.sessionA.sendAudioChunk(base64Audio);
        } catch (err) {
          // Would only happen if Session A were audio-OUT only (it's not).
          console.error("[session-mgr] sendAudioChunk to A failed:", err.message);
        }
      }
      return;
    }

    let msg;
    try { msg = JSON.parse(data.toString()); } catch { return; }

    switch (msg.type) {
      case "barge_in_detected":
        if (this.state === STATE_B_ACTIVE) {
          this._registerBargeInCandidate(msg);
        }
        break;

      case "session_end":
        console.log("[session-mgr] client requested session_end");
        await this.shutdown();
        break;

      default:
        // session_start is handled in server.js before the manager exists.
        break;
    }
  }

  /**
   * Graceful shutdown — close both Bedrock streams, cancel in-flight work.
   */
  async shutdown() {
    console.log("[session-mgr] shutdown()");
    if (this._renewalTimer) {
      clearInterval(this._renewalTimer);
      this._renewalTimer = null;
    }
    this._disarmSessionBWatchdog("shutdown");
    this._disarmSessionBStallWatchdog("shutdown");
    this._disarmSessionBFastErrorWatchdog("shutdown");

    // Fix 1D: self-healing release. If we shut down while a Session B
    // handoff was still in flight (user pressed Stop, WebSocket dropped,
    // page reload, Bedrock error mid-handoff), the rate-limiter
    // reservation on the Python side would otherwise stay counted
    // until the process restarts — permanently blocking every future
    // handoff with HANDOFF_IN_PROGRESS. Release it best-effort here
    // BEFORE we tear the streams down so the release lands even if
    // close races the final HTTP flush.
    if (
      (this.state === STATE_B_ACTIVE || this.state === STATE_HANDOFF_IN_PROGRESS)
      && this._currentAgentId
    ) {
      const agentId = this._currentAgentId;
      console.log(
        "[session-mgr] shutdown self-heal: releasing in-flight handoff agent=%s",
        agentId,
      );
      try { await this._releaseHandoff(agentId); }
      catch (e) {
        console.error(
          "[session-mgr] shutdown self-heal release failed: %s", e.message,
        );
      }
      this._currentAgentId = null;
    }

    if (this.sessionB) {
      try { await this.sessionB.endSession(); }
      catch (e) { console.error("[session-mgr] B.endSession:", e.message); }
      this.sessionB = null;
    }
    if (this.sessionA) {
      try { await this.sessionA.endSession(); }
      catch (e) { console.error("[session-mgr] A.endSession:", e.message); }
      this.sessionA = null;
    }
    this.activeSession = null;
    this.state = STATE_IDLE;
    await Promise.allSettled(Array.from(this._backgroundTasks));
    this._backgroundTasks.clear();
  }

  // ── event processing ─────────────────────────────────────

  async _processSessionEvents(tag) {
    const client = tag === SESSION_A ? this.sessionA : this.sessionB;
    if (!client) return;

    try {
      for await (const event of client.processResponses()) {
        if (!client.isActive) break;

        // First B event of any kind disarms the silent-watchdog.
        if (tag === SESSION_B && this._sessionBWatchdogTimer) {
          this._disarmSessionBWatchdog("first-event");
        }

        switch (event.type) {
          case "audio": {
            const isActiveSession = this.activeSession === tag;
            const isBlocked = (tag === SESSION_A && this.aSpeakerGagged);
            if (isActiveSession && !isBlocked && this.ws.readyState === this.ws.OPEN) {
              const audioBuffer = Buffer.from(event.data, "base64");
              this.ws.send(audioBuffer, { binary: true });
            }
            break;
          }
          case "tool_use": {
            this._spawnBackground(
              `tool-${tag}-${event.toolName}`,
              this._dispatchToolUse(tag, event, client),
            );
            break;
          }
          case "text":
            await this._handleTextEvent(tag, event);
            break;
          case "stream_error": {
            // Bedrock rejected/dropped the stream mid-session.
            // For Session A: the user shouldn't lose Nova from a
            // transient hiccup. Attempt ONE auto-restart while the
            // WebSocket is still healthy. This turns "Nova dies and
            // user must click Start again" into "Nova blips for 1s".
            // For Session B: existing handback path handles it.
            console.error(
              "[session-mgr] %s stream_error: %s (%s)",
              tag, event.message, event.errorName || "?",
            );
            if (tag === SESSION_A && !this._aRestartAttempted
                && this.ws.readyState === this.ws.OPEN) {
              this._aRestartAttempted = true;
              console.log(
                "[session-mgr] Session A abnormal death — attempting auto-restart",
              );
              // Signal the browser (optional UX hint).
              try {
                this.ws.send(JSON.stringify({
                  type: "nova_reconnecting",
                  reason: event.message || "stream_error",
                }));
              } catch (_) { /* best-effort */ }
              // Drop the dead client and re-open Session A. Must null
              // it first or startSessionA's idempotency check short-
              // circuits.
              try { await this.sessionA.endSession(); } catch (_) {}
              this.sessionA = null;
              try {
                await this.startSessionA();
                console.log("[session-mgr] Session A auto-restart succeeded");
                if (this.ws.readyState === this.ws.OPEN) {
                  try {
                    this.ws.send(JSON.stringify({ type: "nova_reconnected" }));
                  } catch (_) { /* best-effort */ }
                }
              } catch (reErr) {
                console.error(
                  "[session-mgr] Session A auto-restart FAILED: %s",
                  reErr.message,
                );
              }
            } else if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
              await this.handback({ reason: "b_stream_error" });
            }
            return;
          }
          case "session_end":
            console.log("[session-mgr] %s session_end received", tag);
            if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
              await this.handback({ reason: "b_stream_end" });
            }
            return;
        }
      }
    } catch (err) {
      console.error("[session-mgr] %s proc loop error: %s", tag, err.message);
      if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
        await this.handback({ reason: "b_stream_error" });
      }
    }
  }

  async _handleTextEvent(tag, event) {
    const text = event.text || "";

    // Nova Sonic's own barge-in signal for Session A (existing behavior).
    if (tag === SESSION_A && text.includes('{ "interrupted" : true }')) {
      if (this.ws.readyState === this.ws.OPEN) {
        this.ws.send(JSON.stringify({ type: "barge_in", source: "nova-a" }));
      }
      return;
    }

    // Short transcript preview for Session B. Without this, postmortems
    // can tell only that "B emitted text" from contentStart/End events;
    // they can't tell WHAT Carlos said. First ~140 chars is enough to
    // capture one Phase line, an error apology ("Finalysis no tiene
    // datos…"), or the terminator. See postmortem 2026-05-09 § RC-3.
    if (tag === SESSION_B && text) {
      const trimmed = text.trim();
      // Skip the Nova Sonic interrupted-marker shape and empty strings.
      if (trimmed && !trimmed.includes('"interrupted"')) {
        const preview = trimmed.length > 140
          ? `${trimmed.slice(0, 140).replace(/\s+/g, " ")}…`
          : trimmed.replace(/\s+/g, " ");
        console.log("[session-mgr] B textOutput: %s", preview);
      }
    }

    // Terminator phrase detection for Session B.
    if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
      const terminators = this._currentTerminators();
      const matched = matchTerminator(text, terminators);
      if (matched) {
        console.log("[session-mgr] terminator phrase matched: %s", matched);
        // Grace so B's audio for this line plays out before handback.
        setTimeout(() => {
          if (this.state === STATE_B_ACTIVE) {
            this.handback({ reason: "terminator", phrase: matched });
          }
        }, GRACE_AFTER_END_SESSION_MS);
        return;
      }

      // Assistant-name yield. If Carlos says the presenter's agent name
      // ("Nova") as a whole word, treat it as a graceful yield back to
      // Session A. Word-bounded so "innovación" / "supernova" don't
      // trigger. Classified as graceful (visor.done path, not
      // visor.aborted) so the report that has already rendered stays
      // on screen and the active-session badge flips cleanly.
      if (this._assistantNameRegex && this._assistantNameRegex.test(text)) {
        console.log(
          "[session-mgr] assistant-name yield matched: %s", this.assistantName,
        );
        setTimeout(() => {
          if (this.state === STATE_B_ACTIVE) {
            this.handback({
              reason: "assistant_name_hail",
              phrase: this.assistantName,
            });
          }
        }, GRACE_AFTER_END_SESSION_MS);
      }
    }
  }

  _currentTerminators() {
    const cfg = this._currentAgentId && this.specialists[this._currentAgentId];
    if (cfg && Array.isArray(cfg.terminators) && cfg.terminators.length > 0) {
      return cfg.terminators.map((p) => String(p).toLowerCase());
    }
    return DEFAULT_TERMINATORS;
  }

  // ── tool dispatch ────────────────────────────────────────

  async _dispatchToolUse(tag, event, client) {
    // Nova Sonic occasionally emits the same tool_use twice per turn
    // (SPECULATIVE vs FINAL generation stages), arriving within a few
    // ms of each other with the same toolUseId. Drop duplicates so
    // we don't POST /tool_call twice and send two tool results back.
    // See: (internal postmortem 2026-05-08) § N3.
    const now0 = Date.now();
    const seenAt = this._seenToolUseIds.get(event.toolUseId);
    if (seenAt && now0 - seenAt < this._toolUseIdTtlMs) {
      console.log(
        "[session-mgr] duplicate tool_use ignored tag=%s tool=%s toolUseId=%s (Δ=%dms)",
        tag, event.toolName, event.toolUseId, now0 - seenAt,
      );
      return;
    }
    this._seenToolUseIds.set(event.toolUseId, now0);
    // Opportunistic GC to bound the map.
    if (this._seenToolUseIds.size > 128) {
      for (const [k, t] of this._seenToolUseIds) {
        if (now0 - t > this._toolUseIdTtlMs) this._seenToolUseIds.delete(k);
      }
    }

    // Fix 1C: payload-level dedup for handoff_to_specialist.
    //
    // The SPECULATIVE and FINAL generation stages of the same turn
    // occasionally arrive with DIFFERENT toolUseIds — the dedup above
    // doesn't catch those, and both reach the Python backend in
    // parallel. Even though Python now has its own dedup (Fix 1B),
    // catching it at the Node layer avoids the extra HTTP round-trip
    // and keeps the happy path single-flighted end-to-end.
    //
    // Key: tool + agent_id + first-80-chars-of-query. 2-second TTL
    // covers the entire SPECULATIVE→FINAL gap Nova ever produces
    // (observed max: ~300 ms).
    if (event.toolName === "handoff_to_specialist") {
      const input = event.toolInput || {};
      const payloadKey = [
        event.toolName,
        String(input.agent_id || ""),
        String(input.query || "").slice(0, 80),
      ].join("\0");
      if (!this._seenPayloadKeys) this._seenPayloadKeys = new Map();
      const prevAt = this._seenPayloadKeys.get(payloadKey);
      if (prevAt && now0 - prevAt < 2000) {
        console.log(
          "[session-mgr] duplicate handoff payload ignored tag=%s agent=%s Δ=%dms",
          tag, input.agent_id, now0 - prevAt,
        );
        return;
      }
      this._seenPayloadKeys.set(payloadKey, now0);
      if (this._seenPayloadKeys.size > 64) {
        for (const [k, t] of this._seenPayloadKeys) {
          if (now0 - t > 5000) this._seenPayloadKeys.delete(k);
        }
      }
    }

    const startedAt = Date.now();
    const body = {
      session_id: tag,
      tool_name: event.toolName,
      tool_input: event.toolInput,
      tool_use_id: event.toolUseId,
    };
    if (tag === SESSION_B && this._currentAgentId) {
      body.agent_id = this._currentAgentId;
    }

    // 2026-05-10 FIX — re-arm the pipeline-stall watchdog at tool_use
    // START, NOT just at tool_done. Postmortem-2026-05-09-three-linked-
    // failures §2.2 identified this bug a year ago, the fix was
    // documented but never actually committed: the re-arm below the
    // fetch (around line 867) only fires AFTER the HTTP call returns.
    // When compose_summary runs for 14s (Sonnet is slow on long
    // contexts), the timer armed by the PREVIOUS tool_done expires
    // mid-fetch and cancels the in-flight Python call, producing the
    // "Generación interrumpida" symptom.
    //
    // Reproducer from logs/node.log 18:39-18:40 (AMZN vs MSFT run):
    //   tool_done generate_chart MSFT   → re-arm 15 s timer
    //   tool_use  compose_summary       → HTTP fetch starts (no re-arm)
    //   …14.3 s later, Sonnet still streaming…
    //   stall watchdog fires            → cancel_session_tools(B)
    //   compose_summary returns ok=false code=CANCELLED
    //
    // With this re-arm at START, compose_summary gets its own fresh
    // 15 s budget from the moment Carlos asks for it. The tool_done
    // re-arm below stays as a backstop against the "specialist
    // narrates after a tool completes and never calls another" failure
    // mode the watchdog was originally built for.
    if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
      this._armSessionBStallWatchdog(
        this._currentHandoffId, this._currentAgentId,
      );
    }

    let toolResult;
    try {
      const resp = await fetch(`${this.pythonUrl}/tool_call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      toolResult = await resp.json();
    } catch (err) {
      console.error(
        "[session-mgr] tool dispatch failed (%s %s): %s",
        tag, event.toolName, err.message,
      );
      toolResult = {
        result: { ok: false, code: "DISPATCH_ERROR", message: err.message },
      };
    }

    const duration = Date.now() - startedAt;
    // Extract the shape the result carries so every future postmortem
    // can answer "did that tool actually succeed?" from a single log
    // line (instead of reconstructing from timing + inference). See
    // (internal postmortem 2026-05-09) § RC-2.
    const resultEnvelope = toolResult?.result;
    const ok = resultEnvelope?.ok;
    const code = resultEnvelope?.code;
    const triggers = resultEnvelope?.trigger_handback === true;
    // Build a compact "ok=true" / "ok=false code=FINALYSIS_ERROR" tail
    // only when the fields are actually present; avoid "ok=undefined".
    // Also include a one-line preview of the input params — the
    // previous postmortem added ok/code but missed the natural
    // companion (what did Carlos *ask* for?). See
    // (internal postmortem 2026-05-09) § RC-3.
    const inputPreview = _previewToolInput(event.toolInput);
    const tail = [
      ok !== undefined ? `ok=${ok}` : null,
      code ? `code=${code}` : null,
      triggers ? "triggers_handback=true" : null,
      inputPreview ? `input=${inputPreview}` : null,
    ].filter(Boolean).join(" ");
    console.log(
      "[session-mgr] tool_done tag=%s tool=%s duration=%dms%s",
      tag, event.toolName, duration, tail ? ` ${tail}` : "",
    );

    // Re-arm the Session B pipeline-stall watchdog after every tool_use.
    // The happy path is 5 tool_uses spaced by ≤ 10 s (compose_summary is
    // the slowest). If the NEXT tool never arrives within
    // SESSION_B_PIPELINE_STALL_MS we force a handback — that's the
    // defense against "specialist narrates after FINALYSIS_ERROR without
    // calling end_session" (see (internal postmortem 2026-05-08)).
    if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
      this._armSessionBStallWatchdog(
        this._currentHandoffId, this._currentAgentId,
      );
    }

    // Session B terminal-error detection → fast-error watchdog + context capture.
    //
    // When a Session B tool returns ok=false with a code we know is
    // terminal (no recovery inside this handoff), we:
    //   (1) Stash { code, message, tool_name, input_preview } in
    //       this._lastBError so handback() can build a specific
    //       HANDBACK_NOTICE for Nova.
    //   (2) Arm the fast-error watchdog (SESSION_B_FAST_ERROR_MS, ~3s)
    //       as a backstop in case Carlos narrates the error but forgets
    //       end_session — brings mic-release latency from 15s → 3s.
    //   (3) If Carlos DOES call end_session promptly (the graceful
    //       path), the triggers_handback branch below runs, which
    //       disarms the fast-error timer — so we never race Carlos.
    //
    // On a SUCCESSFUL Session B tool, we DISARM the fast-error timer
    // (the specialist recovered — this happens when e.g. Carlos retries
    // a different tool; shouldn't happen per prompt but defensive).
    if (tag === SESSION_B && this.state === STATE_B_ACTIVE) {
      if (ok === false && code && TERMINAL_B_ERROR_CODES.has(code)) {
        // Capture compact error context for handback's HANDBACK_NOTICE.
        const msgText = resultEnvelope?.message || "";
        this._lastBError = {
          code,
          message: String(msgText).slice(0, 240),
          tool_name: event.toolName,
          input_preview: inputPreview,
          at_ms: Date.now(),
        };
        // Mirror into the BBrief so the HANDBACK_BRIEF failure
        // branch has the exact inputs Carlos tried. Kept separate
        // from ``_lastBError`` (which is the /NOTICE/ wire format)
        // because the BRIEF carries structured fields Nova can use
        // to propose a corrected re-handoff — the NOTICE is just a
        // sentence-shaped directive. Capturing both is cheap: same
        // event, same code path, each consumer reads the shape it
        // wants.
        this._lastBBrief = this._lastBBrief || {
          agent_id: this._currentAgentId,
          opened_at: this._bOpenedAt,
        };
        this._lastBBrief.attempted = {
          tool: event.toolName,
          input: _cloneToolInputForBrief(event.toolInput),
          at_ms: Date.now(),
        };
        console.log(
          "[session-mgr] [h%d] Session B terminal error captured: " +
          "tool=%s code=%s — arming fast-error watchdog (%d ms)",
          this._currentHandoffId, event.toolName, code,
          SESSION_B_FAST_ERROR_MS,
        );
        this._armSessionBFastErrorWatchdog(
          this._currentHandoffId, this._currentAgentId, code,
        );
      } else if (ok === true && this._sessionBFastErrorTimer) {
        // Specialist somehow recovered (not expected per prompt but
        // defensive). Disarm fast-error and let the normal flow continue.
        this._disarmSessionBFastErrorWatchdog("b_tool_recovered");
        this._lastBError = null;
      }

      // Success path — accumulate the digest slice for this tool.
      // All data already lives in either ``event.toolInput`` (what
      // Carlos asked for) or ``resultEnvelope`` (what came back), so
      // these assignments cost <100µs and add zero I/O. The object
      // is lazily constructed on first success so a handoff that
      // never produces a successful B tool leaves ``_lastBBrief``
      // as ``null``, which downstream code treats as "no brief".
      if (ok === true) {
        this._lastBBrief = this._lastBBrief || {
          agent_id: this._currentAgentId,
          opened_at: this._bOpenedAt,
        };
        _capturePipelineSlice(
          this._lastBBrief, event.toolName, event.toolInput, resultEnvelope,
        );
      }
    }

    // Legacy: forward slide-navigation events to the browser.
    if (tag === SESSION_A && event.toolName === "navigate_slide"
        && toolResult?.result && this.ws.readyState === this.ws.OPEN) {
      this.ws.send(JSON.stringify({
        type: "slide_change",
        slide_index: toolResult.result.slide_index,
        total_slides: toolResult.result.total_slides,
      }));
    }

    // Always deliver the result back to the model.
    try {
      client.sendToolResult(event.toolUseId, toolResult);
    } catch (err) {
      console.error("[session-mgr] sendToolResult failed: %s", err.message);
    }

    // Session A → handoff_to_specialist success → open Session B.
    if (tag === SESSION_A
        && event.toolName === "handoff_to_specialist"
        && signalsHandoffReady(toolResult)) {
      const {
        agent_id, query, customer, session_b_config,
      } = toolResult.result;
      this._spawnBackground(
        `open-B-${agent_id}`,
        this.openSessionBAfterHandoffLine({
          agentId: agent_id,
          query, customer,
          remoteConfig: session_b_config,
        }),
      );
    }

    // Session B → end_session or render_report → handback after grace.
    if (tag === SESSION_B && triggersHandback(toolResult)) {
      // Either the specialist called ``end_session`` explicitly
      // (graceful path wins) or ``render_report`` completed with
      // ``trigger_handback: true`` (the Python shared toolkit promotes
      // a successful render to an implicit end_session — the report
      // is already on screen so waiting for a separate end_session
      // turn would leave Nova idle for several seconds).
      //
      // In both cases: disarm the fast-error watchdog so we don't
      // double-trigger handback (the grace timer below is sufficient).
      // The _lastBError (if any) is preserved so handback's
      // HANDBACK_NOTICE branch still uses the specific b_error
      // message when the previous tool was a terminal error.
      if (this._sessionBFastErrorTimer) {
        this._disarmSessionBFastErrorWatchdog("end_session_graceful");
      }
      const isRenderComplete = event.toolName === "render_report";
      const graceMs = isRenderComplete
        ? GRACE_AFTER_RENDER_COMPLETE_MS
        : GRACE_AFTER_END_SESSION_MS;
      const reason = isRenderComplete ? "render_complete" : "end_session";
      // Guard against double-arming: if a render_complete timer was
      // already pending and we now get an explicit end_session, let
      // the sooner timer win. We don't clear; setTimeout with a
      // state-guard below is idempotent (only the first timer to fire
      // while STATE_B_ACTIVE does anything).
      setTimeout(() => {
        if (this.state === STATE_B_ACTIVE) {
          this.handback({ reason });
        }
      }, graceMs);
      if (isRenderComplete) {
        console.log(
          "[session-mgr] [h%d] render_report ok — scheduled render_complete " +
          "handback in %d ms (grace for final narration)",
          this._currentHandoffId, graceMs,
        );
      }
    }
  }

  // ── the tricky bit: open Session B after Session A's handoff line ──

  /**
   * Open Session B, but wait for Session A's handoff line to finish
   * playing first so the audience doesn't hear two voices overlap.
   *
   * @param {object} params
   * @param {string} params.agentId
   * @param {string} params.query
   * @param {string} [params.customer]
   * @param {object} [params.remoteConfig] Session B voice/terminators/locale
   *     returned by the handoff_to_specialist tool. Merged with the
   *     per-agent config loaded at startup.
   */
  async openSessionBAfterHandoffLine({ agentId, query, customer, remoteConfig }) {
    if (this._openingB) {
      console.warn("[session-mgr] openSessionB already in flight — ignoring");
      return;
    }
    if (this.state === STATE_B_ACTIVE) {
      console.warn("[session-mgr] openSessionB while B already active — ignoring");
      return;
    }
    this._openingB = true;
    this.state = STATE_HANDOFF_IN_PROGRESS;

    const handoffId = ++this._handoffCounter;
    this._currentHandoffId = handoffId;
    this._currentAgentId = agentId;
    // Clear any stale error context from the previous handoff — each
    // handoff starts with a clean slate so a leftover BAD_ARGS from
    // an earlier Tesla query can't accidentally colour Nova's next
    // HANDBACK_NOTICE if the new handoff returns without its own error.
    this._lastBError = null;
    // Same reasoning for the accumulated Session B digest — the
    // HANDBACK_BRIEF should reflect ONLY the handoff that just
    // concluded, never a mix of this run and the previous one.
    // ``_dispatchToolUse`` lazily initialises the object on the
    // first successful B tool (or on a failure's ``attempted``
    // capture), so leaving it null here is correct.
    this._lastBBrief = null;
    const startedAt = Date.now();

    try {
      const cfg = this._resolveSpecialistConfig(agentId, remoteConfig);
      if (!cfg) {
        throw new Error(`no config for specialist ${JSON.stringify(agentId)}`);
      }

      console.log(
        "[session-mgr] [h%d] opening Session B agent=%s voice=%s query=%s",
        handoffId, agentId, cfg.voiceId, String(query).slice(0, 60),
      );

      // (a) Build Session B's seed input while A is still speaking.
      const todayIso = new Date().toISOString().slice(0, 10);
      const initialTextInput = buildSessionBInitialTextInput({
        query, customer, todayIso,
      });

      // (b) Wait for Session A to go quiet before we flip the mux.
      if (this.sessionA?.isActive) {
        const idleStart = Date.now();
        await this.sessionA.waitForAudioIdle({
          debounceMs: HANDOFF_LINE_IDLE_DEBOUNCE_MS,
          timeoutMs: HANDOFF_LINE_IDLE_TIMEOUT_MS,
        });
        console.log(
          "[session-mgr] [h%d] A idle after %dms",
          handoffId, Date.now() - idleStart,
        );
      }

      // (c) Gag Session A's speaker route + open Session B.
      this.aSpeakerGagged = true;
      this.sessionB = this._clientFactory({
        region: this.region,
        voiceId: cfg.voiceId,
        modelId: this.modelId,
      });

      await this.sessionB.startSessionAudioOut({
        systemPrompt: cfg.systemPrompt,
        toolDefinitions: cfg.toolDefs,
        initialUserTextInput: initialTextInput,
      });

      // (d) Flip the mux. Browser speaker now plays Session B.
      this.activeSession = SESSION_B;
      this.state = STATE_B_ACTIVE;
      this._bOpenedAt = Date.now();

      // (e) Tell the browser (optional UI badge).
      if (this.ws.readyState === this.ws.OPEN) {
        this.ws.send(JSON.stringify({
          type: "active_session", who: "B",
          voice: cfg.voiceId,
          agent_id: agentId,
          display_name: cfg.displayName,
        }));
      }

      // (f) Kick off B's event loop.
      this._spawnBackground("proc-B", this._processSessionEvents(SESSION_B));

      // (g) Watchdog: if Session B emits nothing for SESSION_B_SILENT_WATCHDOG_MS
      // we log a loud diagnostic. Clearing happens in _processSessionEvents on
      // the first event of any kind (see "first B event" below).
      this._armSessionBWatchdog(handoffId, agentId);

      // (g.2) Pipeline-stall watchdog: if Session B opens but never
      // produces a tool_use within SESSION_B_PIPELINE_STALL_MS — or
      // stops producing tool_uses mid-pipeline — force a handback with
      // reason="b_pipeline_stall" so the visor flips to
      // "Generación interrumpida" instead of freezing until Bedrock's
      // 55 s audio-idle timeout fires. Re-armed in _dispatchToolUse
      // after every B tool_use; disarmed in handback() + shutdown().
      this._armSessionBStallWatchdog(handoffId, agentId);

      console.log(
        "[session-mgr] [h%d] Session B open in %dms",
        handoffId, Date.now() - startedAt,
      );
    } catch (err) {
      console.error(
        "[session-mgr] [h%d] openSessionB failed: %s", handoffId, err.message,
      );
      this.aSpeakerGagged = false;
      this.activeSession = SESSION_A;
      this.state = STATE_A_ACTIVE;
      try { await this.sessionB?.endSession(); } catch {}
      this.sessionB = null;
      this._currentAgentId = null;
      if (this.ws.readyState === this.ws.OPEN) {
        this.ws.send(JSON.stringify({
          type: "error",
          message: `No pude abrir al especialista (${agentId}) — intenta de nuevo en un momento.`,
        }));
      }
      if (this.sessionA?.isActive) {
        try {
          this.sessionA.sendSystemTextInput(
            `HANDOFF_FAILED: ${agentId} no pudo arrancar. ` +
            "Dile al presentador que fue un fallo técnico y que lo intente de nuevo."
          );
        } catch {}
      }
      await this._releaseHandoff(agentId);
    } finally {
      this._openingB = false;
    }
  }

  /**
   * Merge the in-memory specialist config (systemPrompt + toolDefs, loaded at
   * startup from Python) with any runtime values returned by the handoff tool
   * (terminators, locale, display_name). ``voiceId`` is deliberately forced
   * to match Session A's current voice (``this.voiceIdA``) so the audience
   * hears ONE consistent voice across the whole demo — the specialist
   * inherits whatever the user picked on the localhost:3000 dropdown at
   * ``session_start``.
   *
   * Design note (2026-05-13): earlier versions used a distinct ``carlos``
   * voice for Session B to create a "two voices collaborating" effect.
   * Feedback from live demos was that the voice hand-off felt
   * disjointed — the audience read it as "a second agent took over" when
   * the intended narrative was "same assistant, now doing domain work".
   * Unifying the voice makes the experience cohesive. If you ever want
   * the distinct-voice mode back, replace ``this.voiceIdA`` with
   * ``remoteConfig?.voice_id || local.voiceId`` here (pre-2026-05-13
   * behaviour).
   */
  _resolveSpecialistConfig(agentId, remoteConfig) {
    const local = this.specialists[agentId];
    if (!local) return null;
    return {
      voiceId: this.voiceIdA,
      systemPrompt: local.systemPrompt,
      toolDefs: local.toolDefs,
      terminators: Array.isArray(remoteConfig?.terminators)
        ? remoteConfig.terminators.map((p) => String(p).toLowerCase())
        : local.terminators,
      locale: remoteConfig?.locale || local.locale,
      displayName: remoteConfig?.display_name || local.displayName,
    };
  }

  // ── handback ─────────────────────────────────────────────

  // ── Session B silent-watchdog ─────────────────────────────
  /**
   * Start a timer that fires if Session B emits no event within
   * SESSION_B_SILENT_WATCHDOG_MS. The message is deliberately loud so
   * it's easy to spot in logs/node.log. Called after Session B opens.
   *
   * Cleared by `_disarmSessionBWatchdog` on the first B event, or on
   * handback / shutdown.
   */
  _armSessionBWatchdog(handoffId, agentId) {
    if (!SESSION_B_SILENT_WATCHDOG_MS) return;
    this._disarmSessionBWatchdog();  // just in case
    this._sessionBWatchdogTimer = setTimeout(() => {
      this._sessionBWatchdogTimer = null;
      // Only fire if we're still in B-active state and still on the
      // same handoff — avoid logging for stale timers.
      if (this.state !== STATE_B_ACTIVE) return;
      if (this._currentHandoffId !== handoffId) return;
      console.error(
        "\n[session-mgr] " +
        "===== SESSION B SILENT WATCHDOG =====\n" +
        "[session-mgr]   handoff=%d agent=%s\n" +
        "[session-mgr]   Session B opened %d ms ago and has emitted ZERO events.\n" +
        "[session-mgr]   Likely causes (in order of likelihood):\n" +
        "[session-mgr]     1. USER textInput in startSessionAudioOut() is not interactive:true.\n" +
        "[session-mgr]        (Bedrock will raise InternalErrorCode=532 after ~55 s.)\n" +
        "[session-mgr]     2. Empty system prompt: check /registry/%s → system_prompt_path file exists.\n" +
        "[session-mgr]     3. Malformed tool_defs: check the specialist's inputSchema.json strings.\n" +
        "[session-mgr]     4. Bedrock region/model access issue.\n" +
        "[session-mgr]   See: tests/nova-sonic-client.test.js for the regression net.\n" +
        "[session-mgr] =====================================\n",
        handoffId, agentId, SESSION_B_SILENT_WATCHDOG_MS, agentId,
      );
    }, SESSION_B_SILENT_WATCHDOG_MS);
  }

  _disarmSessionBWatchdog(reason = "disarmed") {
    if (this._sessionBWatchdogTimer) {
      clearTimeout(this._sessionBWatchdogTimer);
      this._sessionBWatchdogTimer = null;
      if (reason !== "disarmed") {
        // Only log on explicit non-default reasons to avoid noise.
        console.log("[session-mgr] Session B watchdog disarmed (%s)", reason);
      }
    }
  }

  // ── Session B pipeline-stall watchdog ─────────────────────
  /**
   * Arm (or re-arm) the pipeline-stall watchdog. Fires if Session B
   * does not emit another tool_use within SESSION_B_PIPELINE_STALL_MS.
   * On fire, forces ``handback({reason: "b_pipeline_stall"})`` so the
   * visor paints "Generación interrumpida" (via ABNORMAL_REASONS in
   * ``/cancel_session_tools``) instead of freezing on the stale phase.
   *
   * Called from:
   *   - openSessionBAfterHandoffLine() once B is open
   *   - _dispatchToolUse() after every Session B tool_use
   *
   * Disarmed from:
   *   - handback()   — graceful or abnormal, either way B is going away
   *   - shutdown()   — browser closed the WS
   */
  _armSessionBStallWatchdog(handoffId, agentId) {
    if (!SESSION_B_PIPELINE_STALL_MS) return;
    this._disarmSessionBStallWatchdog();
    this._sessionBStallWatchdogTimer = setTimeout(() => {
      this._sessionBStallWatchdogTimer = null;
      // Guard against stale timers after the state moved on.
      if (this.state !== STATE_B_ACTIVE) return;
      if (this._currentHandoffId !== handoffId) return;
      console.error(
        "[session-mgr] [h%d] Session B pipeline stalled — no tool_use " +
        "for %d ms (agent=%s). Forcing handback reason=b_pipeline_stall.",
        handoffId, SESSION_B_PIPELINE_STALL_MS, agentId,
      );
      this._spawnBackground(
        "handback-pipeline-stall",
        this.handback({ reason: "b_pipeline_stall" }),
      );
    }, SESSION_B_PIPELINE_STALL_MS);
  }

  _disarmSessionBStallWatchdog(reason = "disarmed") {
    if (this._sessionBStallWatchdogTimer) {
      clearTimeout(this._sessionBStallWatchdogTimer);
      this._sessionBStallWatchdogTimer = null;
      if (reason !== "disarmed") {
        console.log(
          "[session-mgr] Session B stall watchdog disarmed (%s)", reason,
        );
      }
    }
  }

  // ── Session B fast-error watchdog ─────────────────────────
  /**
   * Arm the fast-error watchdog for this handoff. Called from
   * _dispatchToolUse() when a Session B tool_done event reports a
   * terminal error (ok=false + code ∈ TERMINAL_B_ERROR_CODES).
   *
   * Fires after SESSION_B_FAST_ERROR_MS and force-handbacks with
   * reason="b_error", carrying the last error context so handback()
   * can build a specific HANDBACK_NOTICE for Nova.
   *
   * Idempotent — re-arming replaces the existing timer. Disarmed if
   * Carlos calls end_session (graceful path wins) or another tool
   * (specialist somehow recovered, back to the stall watchdog).
   *
   * @param {number} handoffId
   * @param {string} agentId
   * @param {string} errorCode only used for logging on fire
   */
  _armSessionBFastErrorWatchdog(handoffId, agentId, errorCode) {
    if (!SESSION_B_FAST_ERROR_MS) return;
    this._disarmSessionBFastErrorWatchdog();
    this._sessionBFastErrorTimer = setTimeout(() => {
      this._sessionBFastErrorTimer = null;
      // Guard against stale timers after state moved on.
      if (this.state !== STATE_B_ACTIVE) return;
      if (this._currentHandoffId !== handoffId) return;
      console.warn(
        "[session-mgr] [h%d] Session B fast-error trigger — specialist " +
        "tool returned %s and didn't call end_session within %d ms. " +
        "Forcing handback reason=b_error to surface the error to Nova.",
        handoffId, errorCode, SESSION_B_FAST_ERROR_MS,
      );
      this._spawnBackground(
        "handback-fast-error",
        this.handback({ reason: "b_error" }),
      );
    }, SESSION_B_FAST_ERROR_MS);
  }

  _disarmSessionBFastErrorWatchdog(reason = "disarmed") {
    if (this._sessionBFastErrorTimer) {
      clearTimeout(this._sessionBFastErrorTimer);
      this._sessionBFastErrorTimer = null;
      if (reason !== "disarmed") {
        console.log(
          "[session-mgr] Session B fast-error watchdog disarmed (%s)", reason,
        );
      }
    }
  }

  /**
   * Record a `barge_in_detected` candidate and trigger handback only
   * once we have at least BARGE_IN_MIN_HITS within a
   * BARGE_IN_CONFIRM_WINDOW_MS rolling window.
   *
   * The worklet fires every SPEAKING_MIN_INTERVAL_MS (150 ms) while the
   * mic sees sustained energy, so real user speech → ≥ 3 hits in 450 ms.
   * Single spikes (typing, chair squeak, post-AEC specialist echo) get
   * filtered out cheaply on the server without any round-trip to the
   * browser.
   *
   * Big-room note: this is belt-and-suspenders — during Session B the
   * mic gate in the browser already stops worklet signals at the source.
   * This method is the second layer of defense in case the gate is ever
   * bypassed (old browser tab, custom client, testing).
   *
   * @param {object} msg raw parsed JSON from the browser, may carry rms.
   */
  _registerBargeInCandidate(msg) {
    const now = Date.now();

    // Post-handback cooldown. While active, every incoming hit is
    // ignored and the hit list is cleared so the next utterance starts
    // fresh once the cooldown expires.
    if (this._bargeInCooldownUntil && now < this._bargeInCooldownUntil) {
      this._bargeInHits = [];
      return;
    }

    // Prune stale hits outside the confirmation window.
    this._bargeInHits = this._bargeInHits.filter(
      (t) => now - t < BARGE_IN_CONFIRM_WINDOW_MS,
    );
    this._bargeInHits.push(now);

    if (this._bargeInHits.length >= BARGE_IN_MIN_HITS) {
      const windowSpan = now - this._bargeInHits[0];
      const rms = typeof msg?.rms === "number" ? msg.rms.toFixed(3) : "?";
      console.log(
        "[session-mgr] barge-in confirmed (%d hits in %dms, last rms=%s) → handback",
        this._bargeInHits.length, windowSpan, rms,
      );
      this._bargeInCooldownUntil = now + 2000;
      this._bargeInHits = [];
      // Fire-and-forget: keeps this handler synchronous so subsequent
      // browser messages are processed without stalling the event loop.
      this._spawnBackground(
        "handback-barge-in",
        this.handback({ reason: "barge_in" }),
      );
    }
  }

  async handback({ reason, phrase } = {}) {
    if (this.state !== STATE_B_ACTIVE) return;
    // Reset barge-in bookkeeping so the next handoff starts clean.
    this._bargeInHits = [];
    this._disarmSessionBWatchdog("handback");
    this._disarmSessionBStallWatchdog("handback");
    this._disarmSessionBFastErrorWatchdog("handback");
    const handoffId = this._currentHandoffId;
    const sinceOpen = Date.now() - (this._bOpenedAt || Date.now());
    const agentId = this._currentAgentId;

    // Snapshot the last error BEFORE anything else modifies state,
    // so the HANDBACK_NOTICE branch below can build a specific
    // message for Nova. Cleared at the end of handback so the next
    // handoff starts fresh.
    const lastBError = this._lastBError;

    console.log(
      "[session-mgr] [h%d] handback reason=%s phrase=%s elapsed=%dms%s",
      handoffId, reason, phrase || "-", sinceOpen,
      lastBError ? ` lastBError=${lastBError.code}(${lastBError.tool_name})` : "",
    );

    // 0. Release the mic gate IMMEDIATELY. We broadcast
    //    active_session: who=A at the very start of handback (not at
    //    the end like before) so the browser un-gates the microphone
    //    within a few ms of the handback decision, instead of waiting
    //    for the full 200-400ms Python /cancel_session_tools roundtrip.
    //    The presenter regains speaking ability as soon as possible.
    //
    //    Safe: Session B's SPEAKER output is gagged by isAssistantPlaying
    //    (client-side) and the `barge_in` message below drains any queued
    //    audio. The mic gate is a separate concern (client → server
    //    input), and letting the presenter START speaking before the
    //    full teardown completes is explicitly desired here.
    if (this.ws.readyState === this.ws.OPEN) {
      this.ws.send(JSON.stringify({
        type: "active_session", who: "A", voice: this.voiceIdA,
      }));
    }

    // 1. Flush any queued Session B audio in the browser.
    if (this.ws.readyState === this.ws.OPEN) {
      this.ws.send(JSON.stringify({
        type: "barge_in", source: "handback", reason,
      }));
    }

    // 2. Tear down Session B.
    try { await this.sessionB?.endSession(); }
    catch (e) { console.error("[session-mgr] B.endSession: %s", e.message); }
    this.sessionB = null;

    // 3. Cancel any in-flight Python tool calls that belong to B,
    //    AND read back the pipeline-completion status so step 6 can
    //    tell Nova the truth about whether a report actually landed.
    //
    //    Response shape (session_id=B) — see src/api_server.py Fix A:
    //      {
    //        cancelled: string[],
    //        reached_render: boolean,   // render_report completed ok?
    //        path: "graceful"           // reached_render=true + graceful reason
    //            | "escalated-to-abnormal"  // graceful reason but no report
    //            | "abnormal",              // barge_in / b_stream_* / b_timeout
    //        effective: string,         // what the visor was told
    //        reason: string,            // what we passed in
    //      }
    //
    //    Before Fix B (2026-05-09) we ignored this response entirely
    //    and hardcoded "el reporte está en pantalla" in the
    //    HANDBACK_NOTICE for every graceful reason. When Carlos called
    //    end_session without rendering (e.g. Mexican stocks / Finalysis
    //    out of scope), Nova was misled into telling the presenter
    //    their report was ready — it wasn't, and the presenter would
    //    retry the same query in a loop. See
    //    (internal postmortem 2026-05-09).
    let cancelResult = null;
    try {
      const qs = new URLSearchParams({ session_id: "B" });
      if (reason) qs.set("reason", reason);
      const resp = await fetch(
        `${this.pythonUrl}/cancel_session_tools?${qs.toString()}`,
        { method: "POST" },
      );
      try { cancelResult = await resp.json(); } catch {
        // Response not JSON — older backend build? Fall back to
        // reason-only HANDBACK_NOTICE below.
        cancelResult = null;
      }
    } catch (e) {
      console.error("[session-mgr] cancel_session_tools: %s", e.message);
    }

    const reachedRender = cancelResult?.reached_render === true;
    const cancelPath = cancelResult?.path || null;
    console.log(
      "[session-mgr] [h%d] cancel response path=%s reached_render=%s effective=%s",
      handoffId, cancelPath || "-",
      cancelResult ? String(reachedRender) : "?",
      cancelResult?.effective || "-",
    );

    // 4. Release the rate limiter.
    await this._releaseHandoff(agentId);

    // 5. Restore mux.
    this.aSpeakerGagged = false;
    this.activeSession = SESSION_A;
    this.state = STATE_B_ACTIVE === this.state ? STATE_A_ACTIVE : STATE_A_ACTIVE;
    this.state = STATE_A_ACTIVE;
    this._currentAgentId = null;

    // 6. Nudge into Session A describing exactly what happened. Three
    // buckets, matching cancel_session_tools' classification:
    //
    //   GRACEFUL + report on screen (happy path)
    //     → Nova should close the loop and yield the floor.
    //   GRACEFUL + NO report (the 2026-05-09 retry-loop bug)
    //     → Nova must tell the presenter the specialist couldn't
    //       complete the request and offer to try something different.
    //       Without this, Nova tells the presenter the report is ready
    //       when it isn't, so they retry the same out-of-scope query.
    //   ABNORMAL (stream error / timeout / pipeline stall / barge-in)
    //     → Nova should briefly acknowledge the technical interruption
    //       and offer to retry.
    if (this.sessionA?.isActive) {
      // 6a. (Optional) Structured digest. When ``NOVA_HANDBACK_BRIEF``
      // is on AND this handoff captured anything meaningful, inject
      // the BRIEF FIRST — so by the time Nova processes the NOTICE
      // below, her context already contains the chart URL, bullets,
      // ticker, window, stats (success) or error + attempted inputs
      // (failure). Nova's prompt teaches her to prefer the BRIEF when
      // present; the NOTICE remains as a fallback directive for cases
      // where the BRIEF is empty (barge-in before any B tool ran).
      //
      // Sending two system text inputs is cheap (~1 KB each) and keeps
      // the feature flag reversible: if we turn it off, the session
      // manager behaves byte-for-byte like the pre-change version
      // (the NOTICE branch below is unchanged).
      if (HANDBACK_BRIEF_ENABLED && this._lastBBrief) {
        try {
          const brief = _formatHandbackBrief({
            reason,
            reachedRender,
            lastBBrief: this._lastBBrief,
            lastBError,
          });
          if (brief) {
            this.sessionA.sendSystemTextInput(brief);
            console.log(
              "[session-mgr] [h%d] HANDBACK_BRIEF injected (len=%d, path=%s)",
              handoffId, brief.length,
              brief.includes("path=success") ? "success" : "failure",
            );
          }
        } catch (e) {
          // A formatter bug must NEVER break handback. Swallow and
          // fall through to the NOTICE below. Logged loudly so a
          // regression is obvious in logs/node.log.
          console.error(
            "[session-mgr] [h%d] _formatHandbackBrief threw: %s",
            handoffId, e.message,
          );
        }
      }
      try {
        const isGraceful = reason === "terminator" || reason === "end_session";
        if (isGraceful && reachedRender) {
          // 2026-05-12 fix: the old notice ("Quédate en silencio hasta
          // que el presentador hable") contradicted the prompt's
          // OFRECE NARRAR default in the HANDBACK BRIEF section, which
          // tells Nova her default after a successful handback is to
          // ask in ONE short sentence whether to narrate the findings
          // or continue with slides. Because a live system textInput
          // has higher salience than a static prompt rule, Nova was
          // following the notice and staying silent until the
          // presenter called her out — and then asking the offer
          // question redundantly (once the user had already prompted).
          //
          // The new notice mirrors the prompt's OFRECE NARRAR clause
          // and adds an explicit ASK-ONCE guard so Nova never repeats
          // the offer within the same handback.
          this.sessionA.sendSystemTextInput(
            "HANDBACK_NOTICE: El especialista terminó y el reporte está " +
            "en pantalla. En UNA sola oración breve en el idioma del " +
            "presentador, ofrece la opción: ¿repaso los hallazgos o " +
            "continuamos con las diapositivas? Luego quédate en silencio " +
            "y espera su respuesta. IMPORTANTE: si el presentador " +
            "responde con cualquier petición de explicar, describir o " +
            "repasar el reporte (ej: 'explica el reporte', 'explain " +
            "the report', 'walk me through it', 'cuéntame del chart'), " +
            "eso equivale a 'sí' — llama read_current_report y narra. " +
            "NO repitas esta oferta dentro del " +
            "mismo handback — si el presentador ya respondió, actúa " +
            "sobre la respuesta; si ya la hiciste una vez y él retoma " +
            "otro tema, quédate en silencio."
          );
        } else if (isGraceful && cancelResult && !reachedRender) {
          // Fix B: this is the branch that used to lie to Nova.
          // If the LAST tool before end_session was a terminal error,
          // we have structured context — use it for a specific notice
          // instead of the generic 'fuera de alcance'. This is the
          // normal Carlos flow after an error (fetch_data → BAD_ARGS
          // → narrate → end_session).
          if (lastBError) {
            const { code, tool_name, message } = lastBError;
            const hint = _errorHintForNova(code);
            this.sessionA.sendSystemTextInput(
              `HANDBACK_NOTICE: B_FAILED code=${code} tool=${tool_name} ` +
              `detail="${message}". El especialista falló y cerró la sesión. ` +
              `NO digas que el reporte está en pantalla. En UNA oración ` +
              `breve al presentador: ${hint} Luego ofrece UNA alternativa ` +
              `(otro ticker/periodo/indicador). No vuelvas a delegar ` +
              `exactamente la misma consulta.`
            );
          } else {
            this.sessionA.sendSystemTextInput(
              "HANDBACK_NOTICE: El especialista cerró la sesión SIN generar " +
              "un reporte (quedó fuera de su alcance o los datos no estaban " +
              "disponibles). NO digas que el reporte está listo. Dile al " +
              "presentador, en una frase breve, que esa consulta no pudo " +
              "completarse y ofrece una alternativa (p. ej. otro ticker, " +
              "otro periodo, otro mercado dentro del alcance del especialista). " +
              "No vuelvas a delegar la misma consulta."
            );
          }
        } else if (isGraceful && !cancelResult) {
          // No response from Python (network error, old backend).
          // Safest default: treat as 'don't assume success'.
          this.sessionA.sendSystemTextInput(
            "HANDBACK_NOTICE: El especialista terminó, pero no pude " +
            "confirmar si el reporte quedó en pantalla. Pregúntale al " +
            "presentador si necesita que lo repitamos."
          );
        } else if (reason === "assistant_name_hail") {
          this.sessionA.sendSystemTextInput(
            "HANDBACK_NOTICE: El especialista te cedió la palabra al " +
            "mencionarte. Retoma la conducción; si hay un reporte en " +
            "pantalla, haz una transición corta ofreciéndolo al presentador."
          );
        } else if (reason === "b_error" && lastBError) {
          // Carlos returned a terminal error from a tool. We captured
          // the specific error context in _dispatchToolUse; now feed it
          // to Nova so she can tell the presenter what actually broke
          // (much more useful than a generic "error técnico"). The
          // sentence hints in this notice are explicit so Nova
          // produces a consistent user-visible explanation.
          const { code, tool_name, message } = lastBError;
          const hint = _errorHintForNova(code);
          this.sessionA.sendSystemTextInput(
            `HANDBACK_NOTICE: B_FAILED code=${code} tool=${tool_name} ` +
            `detail="${message}". El especialista falló antes de generar ` +
            `el reporte. NO digas que el reporte está en pantalla. En UNA ` +
            `oración breve en el idioma del presentador: ${hint} Luego ` +
            `ofrece UNA alternativa corta (otro ticker/periodo/indicador) ` +
            `o pregunta si intentamos de nuevo. No vuelvas a delegar ` +
            `exactamente la misma consulta.`
          );
        } else if (reason === "b_stream_error" || reason === "b_stream_end" ||
                   reason === "b_timeout" || reason === "b_pipeline_stall") {
          // Check if we have a lastBError for a more specific notice.
          // Pipeline stall is often the fallback path when b_error
          // didn't fire in time OR when Carlos narrates but doesn't
          // tool-call at all.
          if (lastBError) {
            const { code, tool_name, message } = lastBError;
            const hint = _errorHintForNova(code);
            this.sessionA.sendSystemTextInput(
              `HANDBACK_NOTICE: B_FAILED code=${code} tool=${tool_name} ` +
              `detail="${message}". Se interrumpió al especialista. NO digas ` +
              `que el reporte está en pantalla. En UNA oración breve al ` +
              `presentador: ${hint} Luego ofrece reintentar con otros ` +
              `parámetros.`
            );
          } else {
            this.sessionA.sendSystemTextInput(
              "HANDBACK_NOTICE: Se interrumpió al especialista por un error técnico. " +
              "Si el presentador pregunta, menciona brevemente el fallo y ofrécete a reintentar."
            );
          }
        }
        // reason === "barge_in" → user is speaking, don't inject anything.
      } catch (e) {
        console.error("[session-mgr] A.sendSystemTextInput: %s", e.message);
      }
    }

    // Cleanup — clear the captured error for the next handoff.
    this._lastBError = null;
  }

  async _releaseHandoff(agentId) {
    try {
      const q = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
      await fetch(`${this.pythonUrl}/internal/handoff_released${q}`, {
        method: "POST",
      });
    } catch (e) {
      console.warn("[session-mgr] handoff_released failed: %s", e.message);
    }
  }

  // ── renewal ──────────────────────────────────────────────

  _armRenewalTimer() {
    if (this._renewalTimer) clearInterval(this._renewalTimer);
    this._renewalTimer = setInterval(async () => {
      if (this.sessionA?.needsRenewal?.()) {
        try {
          await this.sessionA.renewSession();
          console.log("[session-mgr] Session A renewed");
        } catch (e) {
          console.error("[session-mgr] A renewal failed: %s", e.message);
        }
      }
      if (this.sessionB?.needsRenewal?.()) {
        console.warn(
          "[session-mgr] B near 8-min limit — forcing handback (b_timeout)",
        );
        await this.handback({ reason: "b_timeout" });
      }
    }, RENEWAL_CHECK_INTERVAL_MS);
  }

  // ── background task tracking ─────────────────────────────

  _spawnBackground(label, promise) {
    const wrapped = (async () => {
      try { await promise; }
      catch (e) { console.error("[session-mgr] bg %s threw: %s", label, e); }
      finally { this._backgroundTasks.delete(wrapped); }
    })();
    this._backgroundTasks.add(wrapped);
    return wrapped;
  }
}
