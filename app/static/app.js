/* MoBI-MSM participant interface.
 *
 * The server owns the flow: this script renders what it is handed, collects one
 * input at a time, and posts it back. It never knows the Stance, the assignment,
 * or what comes next.
 *
 * Two constraints shape the input handling:
 *   1. Keystroke integrity (Section 10) -- paste, drop, autofill, autocomplete
 *      and spellcheck-driven replacement are all blocked, because keydown/keyup
 *      is captured by a separate tool and must reflect real typing.
 *   2. Forward-only navigation (Section 6) -- there is no back affordance, and
 *      a submitted value can never be revisited.
 */
'use strict';

const els = {};
const state = {
  config: null,
  sessionId: null,
  input: null,          // the InputSpec currently being collected
  busy: false,          // a request is in flight; blocks double-submits
  loadedParticipant: null,
  userMsgSeq: 1,
  statusText: '',        // canonical status bar text, restored after a notice
  statusNoticeTimer: null,
  fontOk: false,
  unfocusedSince: null,  // wall-clock ms when the window last lost focus
};

document.addEventListener('DOMContentLoaded', init);

function init() {
  for (const id of [
    'start-screen', 'start-title', 'start-body', 'start-form', 'participant-id',
    'participant-error', 'participant-loaded', 'start-button',
    'task-screen', 'statusbar', 'status-block', 'transcript', 'transcript-scroll',
    'composer', 'numeric-form', 'numeric-input', 'numeric-submit', 'numeric-error',
    'numeric-low', 'numeric-high',
    'text-form', 'text-input', 'text-submit', 'text-error', 'word-count',
    'gate-form', 'gate-button', 'thinking', 'thinking-text',
    'fatal', 'fatal-text', 'fatal-retry',
    'end-screen', 'end-title', 'end-body', 'quit-button',
    'fixation', 'fixation-mark', 'setup-error', 'setup-error-body',
  ]) {
    els[id] = document.getElementById(id);
  }

  hardenInput(els['participant-id']);
  hardenInput(els['numeric-input']);
  hardenInput(els['text-input']);

  els['start-form'].addEventListener('submit', onStartSubmit);
  els['participant-id'].addEventListener('input', onParticipantIdInput);
  els['participant-id'].addEventListener('keydown', enterSubmits(els['start-form']));
  els['numeric-form'].addEventListener('submit', onNumericSubmit);
  els['numeric-input'].addEventListener('input', onNumericInput);
  els['numeric-input'].addEventListener('keydown', enterSubmits(els['numeric-form']));
  els['text-form'].addEventListener('submit', onTextSubmit);
  els['text-input'].addEventListener('input', onTextInput);
  els['text-input'].addEventListener('keydown', onTextKeydown);
  els['gate-form'].addEventListener('submit', onGateSubmit);
  els['quit-button'].addEventListener('click', onQuit);
  els['fatal-retry'].addEventListener('click', () => hide(els['fatal']));

  // Forward-only: neutralize the browser back gesture so a participant cannot
  // navigate out of a block. There is no in-app back affordance at all.
  history.pushState(null, '', location.href);
  window.addEventListener('popstate', () => history.pushState(null, '', location.href));
  window.addEventListener('beforeunload', (e) => {
    if (state.sessionId) { e.preventDefault(); e.returnValue = ''; }
  });

  // The composer grows (textarea auto-height, inline errors, scale anchors).
  // Re-pin the transcript so the newest message is never hidden behind it.
  if (window.ResizeObserver) {
    new ResizeObserver(() => scrollToBottom()).observe(els['composer']);
  }

  // Scroll changes the mapping from screen position to content, so they are
  // reported (trailing-throttled -- one marker per settled scroll, not per frame).
  let scrollTimer = null;
  els['transcript-scroll'].addEventListener('scroll', () => {
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      scrollTimer = null;
      sendEvent('transcript_scrolled', viewportState());
    }, 120);
  }, { passive: true });

  installFocusIntegrity();

  loadConfig();
}

/* Enter submits every field (Section 6). Handled explicitly rather than relying
 * on implicit form submission, which varies across browsers and kiosk shells. */
function enterSubmits(form) {
  return (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  };
}

/* ---------- input hardening ---------- */

function hardenInput(el) {
  if (!el) return;
  for (const evt of ['paste', 'drop', 'dragover', 'cut', 'copy']) {
    el.addEventListener(evt, (e) => { e.preventDefault(); reportBlockedInput(evt); });
  }
  // Catches paste routes that bypass the paste event (menus, IME, autofill).
  el.addEventListener('beforeinput', (e) => {
    if (e.inputType && /paste|drop/i.test(e.inputType)) {
      e.preventDefault();
      reportBlockedInput(e.inputType);
    }
  });
  el.addEventListener('contextmenu', (e) => e.preventDefault());
  el.setAttribute('autocomplete', 'off');
  el.setAttribute('autocorrect', 'off');
  el.setAttribute('autocapitalize', 'off');
  el.setAttribute('spellcheck', 'false');
  el.setAttribute('data-gramm', 'false');       // Grammarly and similar overlays
  el.setAttribute('data-1p-ignore', 'true');    // 1Password autofill
  el.setAttribute('data-lpignore', 'true');     // LastPass autofill
}

function reportBlockedInput(kind) {
  if (state.sessionId) sendEvent('validation_blocked', { reason: 'paste_blocked', kind });
}

/* ---------- API ---------- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    method: options.method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch (_) { /* non-JSON error page */ }
  if (!res.ok) {
    const err = new Error((payload && (payload.detail || payload.message)) || `Request failed (${res.status})`);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

function sendEvent(event, detail = {}) {
  if (!state.sessionId) return;
  // Fire-and-forget: a marker must never delay the participant's next action.
  fetch(`/api/session/${state.sessionId}/event`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event, detail }),
    keepalive: true,
  }).catch(() => {});
}

async function loadConfig() {
  try {
    state.config = await api('/api/config', { method: 'GET' });
  } catch (err) {
    // Nothing has started yet, so this belongs on the Start Screen where the
    // RA is looking, not in the task composer.
    show(els['participant-error'], 'Could not reach the task server. Ask the researcher for help.');
    els['start-button'].disabled = true;
    console.error('[mobi] config load failed', err);
    return;
  }
  // Every size in the stylesheet is a multiple of one degree of visual angle,
  // resolved here from the server's rig geometry. Applied before the font gate
  // so nothing is ever measured or painted at the fallback scale.
  applyRigScale();

  els['start-title'].textContent = state.config.copy.start_title;
  els['start-body'].textContent = state.config.copy.start_body;
  els['end-title'].textContent = state.config.copy.end_title;
  els['end-body'].textContent = state.config.copy.end_body;

  // The live word counter is kept but can be switched off from config. It is
  // made invisible rather than removed, so the composer's layout -- and every
  // AOI rectangle under it -- is the same whether or not it shows.
  if (!state.config.show_word_count) els['word-count'].style.visibility = 'hidden';

  // Section 12: nothing is shown until the pinned font has settled, so text is
  // never laid out with fallback metrics and then reflowed.
  const fontOk = await ensureFontReady();
  if (!fontOk && !state.config.allow_system_font) {
    els['setup-error-body'].textContent =
      `The interface font (${state.config.ui_font_family}) is not installed, so text would be ` +
      `laid out with the wrong metrics and the eye-tracking geometry would be invalid.\n\n` +
      `Place the font file at app/static/${state.config.ui_font_file} and reload. ` +
      `See "Font determinism" in the README.`;
    els['setup-error'].hidden = false;
    console.error('[mobi] pinned font unavailable; refusing to start a session');
    return;
  }
  if (!fontOk) {
    console.warn('[mobi] running on a SYSTEM FONT FALLBACK - development only, not valid for data collection');
  }
  state.fontOk = fontOk;

  els['start-screen'].hidden = false;
  els['participant-id'].focus();
}

/* One degree of visual angle, in CSS pixels, from the configured rig. */
function applyRigScale() {
  const ppd = state.config.rig && state.config.rig.px_per_degree;
  if (!ppd || !isFinite(ppd)) {
    console.error('[mobi] no px_per_degree from server; keeping stylesheet fallback');
    return;
  }
  document.documentElement.style.setProperty('--deg', `${ppd}px`);
}

/* Section 12: the interface pins its metrics to one embedded face, so the page
 * waits for that face rather than painting a fallback and reflowing after. */
async function ensureFontReady() {
  const family = state.config.ui_font_family;
  if (!document.fonts || !document.fonts.load) return false;
  try {
    await document.fonts.load(`1em "${family}"`);
    await document.fonts.ready;
    return document.fonts.check(`1em "${family}"`);
  } catch (err) {
    console.error('[mobi] font loading failed', err);
    return false;
  }
}

/* ---------- Start Screen ---------- */

function onParticipantIdInput() {
  // Any edit invalidates a previously loaded ID.
  state.loadedParticipant = null;
  els['start-button'].disabled = true;
  els['start-button'].textContent = 'Look up';
  els['start-button'].disabled = els['participant-id'].value.trim().length === 0;
  hide(els['participant-error']);
  hide(els['participant-loaded']);
  els['participant-id'].classList.remove('invalid');
}

async function onStartSubmit(e) {
  e.preventDefault();
  if (state.busy) return;
  const pid = els['participant-id'].value.trim();
  if (!pid) return;

  // First submit looks the ID up; the second starts the session (Section 4:
  // Start becomes available only once a valid, not-yet-run ID is loaded).
  if (state.loadedParticipant !== pid) {
    await lookupParticipant(pid);
    return;
  }
  await beginSession(pid);
}

async function lookupParticipant(pid) {
  state.busy = true;
  els['start-button'].disabled = true;
  try {
    await api('/api/participant/lookup', { body: { participant_id: pid } });
    state.loadedParticipant = pid;
    els['participant-id'].classList.remove('invalid');
    hide(els['participant-error']);
    show(els['participant-loaded'], `Participant ${pid} loaded and ready.`);
    els['start-button'].textContent = 'Start';
  } catch (err) {
    state.loadedParticipant = null;
    els['participant-id'].classList.add('invalid');
    hide(els['participant-loaded']);
    show(els['participant-error'], err.message);
    els['participant-id'].select();
  } finally {
    state.busy = false;
    els['start-button'].disabled = false;
  }
}

async function beginSession(pid) {
  state.busy = true;
  els['start-button'].disabled = true;
  try {
    const view = await api('/api/session', { body: { participant_id: pid } });
    state.sessionId = view.session_id;
    hide(els['start-screen']);
    els['task-screen'].hidden = false;
    sendEvent('start_screen_shown', {
      font: state.fontOk ? state.config.ui_font_family : 'SYSTEM-FALLBACK',
      font_ok: Boolean(state.fontOk),
    });
    checkPresentationMode();
    await applyView(view);
  } catch (err) {
    show(els['participant-error'], err.message);
    els['start-button'].disabled = false;
  } finally {
    state.busy = false;
  }
}

/* ---------- Rendering ---------- */

async function applyView(view) {
  setStatus(view.is_practice
    ? 'Practice'
    : `Block ${view.block_index} of ${view.progress.total_blocks}`);

  // Blocks are self-contained: opening one clears everything before it, so the
  // transcript can never be scrolled back into a previous block. The fixation
  // cross is held over the cleared screen before the new vignette renders.
  if (view.reset_transcript) {
    clearTranscript();
    await showFixationCross();
  }

  if (view.messages && view.messages.length) {
    appendMessages(view.messages);
  }

  if (view.done) {
    finishSession();
    return;
  }
  if (view.awaiting_llm) {
    runLLMTurn();
    return;
  }
  showInput(view.input);
}

function appendMessages(messages) {
  const ids = [];
  for (const m of messages) {
    els['transcript'].appendChild(buildMessage(m));
    ids.push(m.id);
  }
  scrollToBottom();
  // Report render time after the browser has actually painted, so the marker
  // reflects when the participant could see it.
  afterPaint(() => {
    sendEvent('messages_rendered', {
      message_ids: ids,
      kinds: messages.map((m) => m.kind),
      geometry: messageGeometry(ids),
      ...viewportState(),
    });
  });
}

function buildMessage(m) {
  const node = document.createElement('div');
  node.className = `msg msg-${m.kind}`;
  node.dataset.messageId = m.id;
  node.dataset.kind = m.kind;

  if (m.kind === 'vignette') {
    // The vignette, its instruction and its appraisal appear together, with
    // the appraisal scale's 0/100 anchor labels (Section 5, steps 1-2).
    const text = document.createElement('p');
    text.className = 'vignette-text';
    text.textContent = m.text;
    node.appendChild(text);

    if (m.instruction) {
      const i = document.createElement('p');
      i.className = 'vignette-instruction';
      i.textContent = m.instruction;
      node.appendChild(i);
    }

    if (m.question) {
      const q = document.createElement('p');
      q.className = 'vignette-appraisal';
      q.textContent = m.question;
      node.appendChild(q);
    }
    if (m.scale_low || m.scale_high) {
      const scale = document.createElement('div');
      scale.className = 'vignette-scale';
      const low = document.createElement('span');
      low.innerHTML = '<b>0</b> = ';
      low.append(m.scale_low || '');
      const high = document.createElement('span');
      high.innerHTML = '<b>100</b> = ';
      high.append(m.scale_high || '');
      scale.append(low, high);
      node.appendChild(scale);
    }
    return node;
  }

  node.textContent = m.text;
  return node;
}

/* ---------- AOI geometry (eye tracking) ---------- */

/* Run once the browser has painted, so a reported rectangle reflects what the
 * participant could actually see. requestAnimationFrame is suspended while the
 * window is hidden or minimized, so a timeout backstop guarantees the marker is
 * emitted either way -- instrumentation must never be silently lost. */
function afterPaint(fn) {
  let fired = false;
  const run = () => {
    if (fired) return;
    fired = true;
    fn();
  };
  requestAnimationFrame(() => requestAnimationFrame(run));
  setTimeout(run, 250);
}

/* Gaze arrives in screen coordinates, so an AOI is only recoverable if we know
 * where each element actually was at the moment it was on screen. These
 * rectangles are viewport-relative (the same frame as the screen recording),
 * and every report carries the scroll offset so content-relative position can
 * be reconstructed for anything the participant scrolled past. */
function rectOf(node) {
  const r = node.getBoundingClientRect();
  return {
    x: Math.round(r.x), y: Math.round(r.y),
    w: Math.round(r.width), h: Math.round(r.height),
  };
}

function messageGeometry(ids) {
  const out = [];
  for (const id of ids) {
    const node = els['transcript'].querySelector(`[data-message-id="${CSS.escape(id)}"]`);
    if (node) out.push({ id, kind: node.dataset.kind, ...rectOf(node) });
  }
  return out;
}

function viewportState() {
  return {
    scroll_top: Math.round(els['transcript-scroll'].scrollTop),
    scroll_height: Math.round(els['transcript-scroll'].scrollHeight),
    viewport_w: window.innerWidth,
    viewport_h: window.innerHeight,
    device_pixel_ratio: window.devicePixelRatio,
  };
}

function clearTranscript() {
  els['transcript'].replaceChildren();
  els['transcript-scroll'].scrollTop = 0;
}

function appendUserMessage(text, numeric) {
  const node = document.createElement('div');
  node.className = 'msg msg-user' + (numeric ? ' numeric' : '');
  // Participant answers are AOIs too, so they carry ids and are reported.
  const id = `u${state.userMsgSeq++}`;
  node.dataset.messageId = id;
  node.dataset.kind = numeric ? 'user-numeric' : 'user-text';
  node.textContent = text;
  els['transcript'].appendChild(node);
  scrollToBottom();
  afterPaint(() => {
    sendEvent('messages_rendered', {
      message_ids: [id],
      kinds: [node.dataset.kind],
      geometry: messageGeometry([id]),
      ...viewportState(),
    });
  });
}

function scrollToBottom() {
  const scroller = els['transcript-scroll'];
  const pin = () => { scroller.scrollTop = scroller.scrollHeight; };
  // Once after paint, and once more after the composer has settled to its new
  // height, so the newest message is never left behind the entry area.
  requestAnimationFrame(pin);
  setTimeout(pin, 90);
}

/* ---------- Inter-block fixation cross (Section 12) ---------- */

/* Held for a fixed interval before every block's vignette -- 13 times across a
 * session -- giving analysts a known, identical screen position to correct
 * calibration drift against. Non-interactive and not skippable: the overlay
 * covers the whole viewport and no input is on screen while it is up. */
function showFixationCross() {
  return new Promise((resolve) => {
    els['fixation'].hidden = false;

    let finished = false;
    const hold = state.config.fixation_cross_ms;

    // Timed from the frame the cross is actually PAINTED, not from the DOM
    // mutation, and ended on a frame boundary -- the hold is a visual interval,
    // so it should be measured and terminated in the same units the participant
    // experiences. setTimeout alone drifts by hundreds of ms under load.
    afterPaint(() => {
      const shownAt = performance.now();

      sendEvent('fixation_cross_shown', {
        geometry: { cross: rectOf(els['fixation-mark']) },
        ...viewportState(),
        // The cross displaces the transcript entirely, so there is no scroll
        // offset to reconcile against for this screen.
        scroll_top: 0,
        hold_ms: hold,
      });

      const finish = () => {
        if (finished) return;
        finished = true;
        // Hidden here rather than by the caller so that hiding the overlay and
        // appending the block's first messages land in the same task, and the
        // browser paints them together -- no frame of empty transcript.
        els['fixation'].hidden = true;

        // The hold is a timing reference, so the realised interval is recorded
        // and flagged when it drifts. Browsers throttle both rAF and timers in
        // a backgrounded or non-fullscreen window, which silently stretches the
        // hold; that must be visible in the record, not assumed away.
        const actual = Math.round(performance.now() - shownAt);
        const drift = actual - hold;
        const withinTolerance = Math.abs(drift) <= state.config.fixation_cross_tolerance_ms;
        sendEvent('fixation_cross_hidden', {
          nominal_hold_ms: hold,
          actual_hold_ms: actual,
          drift_ms: drift,
          within_tolerance: withinTolerance,
        });
        if (!withinTolerance) {
          console.warn(`[mobi] fixation cross held ${actual}ms (nominal ${hold}ms).`);
        }
        resolve();
      };

      const deadline = shownAt + hold;
      const tick = (now) => (now >= deadline ? finish() : requestAnimationFrame(tick));
      requestAnimationFrame(tick);

      // Backstop: rAF is suspended while the window is hidden, and the hold
      // must still end.
      setTimeout(finish, hold + 250);
    });
  });
}

/* ---------- Status bar ---------- */

function setStatus(text) {
  state.statusText = text;
  els['status-block'].textContent = text;
}

/* RA-facing only. Section 12 routes focus-loss notices through the existing
 * low-contrast chrome rather than a new UI surface, so the participant's screen
 * does not gain an AOI event. */
function flashStatusNotice(text, ms = 6000) {
  els['status-block'].textContent = text;
  if (state.statusNoticeTimer) clearTimeout(state.statusNoticeTimer);
  state.statusNoticeTimer = setTimeout(() => {
    state.statusNoticeTimer = null;
    els['status-block'].textContent = state.statusText;
  }, ms);
}

/* ---------- Window / focus integrity (Section 12) ---------- */

/* Logging only: never pauses the flow or blocks input. Any interval where gaze
 * cannot be trusted to correspond to on-screen content is flagged in the record
 * rather than left silently present in the gaze stream. */
function installFocusIntegrity() {
  const lost = (reason) => {
    if (state.unfocusedSince !== null) return;   // already flagged
    state.unfocusedSince = Date.now();
    sendEvent('window_focus_lost', { reason, ...viewportState() });
  };

  const regained = (reason) => {
    if (state.unfocusedSince === null) return;
    const unfocusedMs = Date.now() - state.unfocusedSince;
    state.unfocusedSince = null;
    sendEvent('window_focus_regained', { reason, unfocused_ms: unfocusedMs, ...viewportState() });
    flashStatusNotice(`Window lost focus for ${(unfocusedMs / 1000).toFixed(1)}s`);
  };

  window.addEventListener('blur', () => lost('blur'));
  window.addEventListener('focus', () => regained('focus'));
  document.addEventListener('visibilitychange', () => {
    sendEvent('visibility_changed', { visibility: document.visibilityState });
    if (document.visibilityState === 'hidden') lost('hidden');
    else regained('visible');
  });
}

/* The browser is expected to run fullscreen/kiosk. Toolbars cannot be detected
 * directly, but a window materially smaller than the screen means chrome is
 * showing -- worth recording, and worth telling the RA before a session. */
function checkPresentationMode() {
  const fullscreen =
    Boolean(document.fullscreenElement) ||
    (window.innerHeight >= window.screen.height - 2 && window.innerWidth >= window.screen.width - 2);

  // The rig's px/deg is computed from the monitor's NATIVE resolution. If the
  // OS or browser applies display scaling, the browser reports a different CSS
  // pixel count and every visual-angle figure is wrong -- silently. This is the
  // check that catches it before a session rather than after.
  const rig = state.config.rig || {};
  const expectedW = rig.screen_w_px;
  const expectedH = rig.screen_h_px;
  const scaleMismatch = Boolean(expectedW) &&
    (Math.abs(window.screen.width - expectedW) > 2 || Math.abs(window.screen.height - expectedH) > 2);

  sendEvent('presentation_mode_checked', {
    fullscreen,
    screen_w: window.screen.width,
    screen_h: window.screen.height,
    expected_screen_w: expectedW,
    expected_screen_h: expectedH,
    scale_mismatch: scaleMismatch,
    px_per_degree: rig.px_per_degree,
    viewing_distance_mm: rig.viewing_distance_mm,
    resolved_deg_px: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--deg')),
    ...viewportState(),
  });

  if (scaleMismatch) {
    const msg = `Display is ${window.screen.width}x${window.screen.height}, rig expects ` +
      `${expectedW}x${expectedH} - visual-angle sizing is off`;
    flashStatusNotice(msg, 15000);
    console.warn(`[mobi] ${msg}. Check OS display scaling, or update the rig in app/config.py.`);
  } else if (!fullscreen) {
    flashStatusNotice('Not fullscreen - browser chrome is visible', 10000);
    console.warn('[mobi] Window is not fullscreen; run the browser in kiosk mode before collecting data.');
  }
  return { fullscreen, scaleMismatch };
}

/* ---------- Input handling ---------- */

function showInput(spec) {
  hideAllEntries();
  state.input = spec;
  // The session is only ready for a submission once an input is on screen.
  // Releasing `busy` any earlier would let a submit fire against a step the
  // server has already moved past.
  state.busy = !spec;
  if (!spec) return;

  if (spec.type === 'numeric') {
    els['numeric-low'].textContent = spec.scale_low;
    els['numeric-high'].textContent = spec.scale_high;
    els['numeric-input'].value = '';
    els['numeric-input'].classList.remove('invalid');
    hide(els['numeric-error']);
    els['numeric-form'].hidden = false;
    els['numeric-input'].focus();
  } else if (spec.type === 'text') {
    els['text-input'].value = '';
    els['text-input'].classList.remove('invalid');
    els['text-input'].style.height = '';
    // The LLM's own reply asks for responses 2 and 3, so there is no prompt
    // message above the box. The placeholder is what tells the participant the
    // box is theirs if a reply ever fails to close on a question.
    els['text-input'].placeholder = spec.placeholder || 'Type your response...';
    hide(els['text-error']);
    updateWordCount();
    els['text-form'].hidden = false;
    els['text-input'].focus();
  } else if (spec.type === 'gate') {
    els['gate-button'].textContent = spec.button || 'Continue';
    els['gate-form'].hidden = false;
    els['gate-button'].focus();
  }
  // The entry area is a persistent AOI; its box moves as the composer grows.
  afterPaint(() => {
    const form = spec.type === 'numeric' ? els['numeric-form']
      : spec.type === 'text' ? els['text-form'] : els['gate-form'];
    const field = spec.type === 'numeric' ? els['numeric-input']
      : spec.type === 'text' ? els['text-input'] : els['gate-button'];
    sendEvent('input_focused', {
      step: spec.step,
      type: spec.type,
      geometry: { entry: rectOf(form), field: rectOf(field) },
      ...viewportState(),
    });
  });
}

function hideAllEntries() {
  // A hidden entry must never leave a stale spec behind for a submit to use.
  state.input = null;
  els['numeric-form'].hidden = true;
  els['text-form'].hidden = true;
  els['gate-form'].hidden = true;
  els['thinking'].hidden = true;
}

/* Numeric: whole numbers 0-100, blocked inline before submission (Section 6). */

function validateNumeric(raw) {
  const text = raw.trim();
  if (!text) return 'Enter a number.';
  if (!/^\d+$/.test(text)) return 'Enter a whole number between 0 and 100, with no decimal point.';
  const value = Number(text);
  const lo = state.config.score_min, hi = state.config.score_max;
  if (value < lo || value > hi) return `Enter a number between ${lo} and ${hi}.`;
  return null;
}

function onNumericInput() {
  const error = validateNumeric(els['numeric-input'].value);
  els['numeric-input'].classList.toggle('invalid', Boolean(error) && els['numeric-input'].value.trim() !== '');
  if (!error) hide(els['numeric-error']);
}

async function onNumericSubmit(e) {
  e.preventDefault();
  if (state.busy || !state.input) return;
  const raw = els['numeric-input'].value.trim();
  const error = validateNumeric(raw);
  if (error) {
    els['numeric-input'].classList.add('invalid');
    show(els['numeric-error'], error);
    sendEvent('validation_blocked', { step: state.input.step, reason: error });
    return;
  }
  appendUserMessage(raw, true);
  await submit(state.input.step, raw, els['numeric-error'], els['numeric-input']);
}

/* Free text: >= min_response_words whitespace-delimited words (currently 1, so
 * only an empty box is blocked); Enter submits, Shift+Enter newline. */

function wordCount(text) {
  return text.trim().split(/\s+/).filter(Boolean).length;
}

function updateWordCount() {
  const n = wordCount(els['text-input'].value);
  const min = state.config.min_response_words;
  els['word-count'].textContent = n >= min ? `${n} words` : `${n} of ${min} words minimum`;
  els['word-count'].className = 'word-count ' + (n >= min ? 'met' : 'short');
}

function onTextInput() {
  updateWordCount();
  const ta = els['text-input'];
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
  if (wordCount(ta.value) >= state.config.min_response_words) {
    ta.classList.remove('invalid');
    hide(els['text-error']);
  }
}

function onTextKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    els['text-form'].requestSubmit();
  }
}

async function onTextSubmit(e) {
  e.preventDefault();
  if (state.busy || !state.input) return;
  const raw = els['text-input'].value.trim();
  const n = wordCount(raw);
  const min = state.config.min_response_words;
  if (n < min) {
    els['text-input'].classList.add('invalid');
    show(els['text-error'], min <= 1
      ? 'Please type a response.'
      : `Please write at least ${min} words (${n} so far).`);
    sendEvent('validation_blocked', { step: state.input.step, reason: 'below_min_words', words: n });
    return;
  }
  appendUserMessage(raw, false);
  await submit(state.input.step, raw, els['text-error'], els['text-input']);
}

async function onGateSubmit(e) {
  e.preventDefault();
  if (state.busy || !state.input) return;
  await submit('gate', true, null, null);
}

async function submit(step, value, errorEl, inputEl) {
  const spec = state.input;
  state.busy = true;
  hideAllEntries();
  try {
    const view = await api(`/api/session/${state.sessionId}/submit`, { body: { step, value } });
    await applyView(view);   // releases `busy` once the next input is shown
  } catch (err) {
    // The server is the authority on validity; surface its message and let the
    // participant correct the value in place.
    if (err.status === 422 && errorEl && inputEl) {
      removeLastUserMessage();
      showInput(spec);
      inputEl.value = value;
      inputEl.classList.add('invalid');
      show(errorEl, err.message);
      if (inputEl === els['text-input']) updateWordCount();
    } else {
      showFatal(err, () => { showInput(spec); });
    }
  }
}

function removeLastUserMessage() {
  const nodes = els['transcript'].querySelectorAll('.msg-user');
  if (nodes.length) nodes[nodes.length - 1].remove();
}

/* ---------- LLM turn ---------- */

async function runLLMTurn() {
  state.busy = true;   // held until the reply arrives and the next input appears
  const shownAt = showThinking();
  try {
    const view = await api(`/api/session/${state.sessionId}/llm`);
    // Fixed hold: the reply appears exactly llm_thinking_hold_ms after the
    // indicator did, however fast the model was, so the wait is the same on
    // every trial. A reply that took longer appears on arrival instead, and
    // the marker says by how much it overran.
    const replyAfter = performance.now() - shownAt;
    const hold = state.config.llm_thinking_hold_ms;
    if (replyAfter < hold) await new Promise((r) => setTimeout(r, hold - replyAfter));
    clearThinking(shownAt, replyAfter);
    await applyView(view);
  } catch (err) {
    clearThinking(shownAt, null);
    showFatal(err, () => runLLMTurn());
  }
}

function showThinking() {
  hideAllEntries();
  els['thinking-text'].textContent = 'Thinking';
  els['thinking'].hidden = false;
  sendEvent('thinking_shown', { hold_ms: state.config.llm_thinking_hold_ms });
  return performance.now();
}

function clearThinking(shownAt, replyAfter) {
  els['thinking'].hidden = true;
  const shownMs = Math.round(performance.now() - shownAt);
  const hold = state.config.llm_thinking_hold_ms;
  sendEvent('thinking_hidden', {
    hold_ms: hold,
    actual_hold_ms: shownMs,
    reply_after_ms: replyAfter === null ? null : Math.round(replyAfter),
    // A reply that came back after the hold was shown late, so this trial's
    // wait was longer than the others'. null when the request failed.
    overran: replyAfter === null ? null : replyAfter > hold,
    failed: replyAfter === null,
  });
}

/* ---------- End Screen ---------- */

function finishSession() {
  hideAllEntries();
  els['task-screen'].hidden = true;
  els['end-screen'].hidden = false;
  sendEvent('end_screen_shown', {});
  state.sessionId = null;   // releases the beforeunload guard
  els['quit-button'].focus();
}

function onQuit() {
  els['quit-button'].disabled = true;
  els['quit-button'].textContent = 'You can close this window';
  window.close();  // only succeeds for a script-opened window; harmless otherwise
}

/* ---------- Errors ---------- */

/* Participants never see internal error text -- it would be meaningless to them
 * and could reveal flow internals. The detail goes to the marker log for the RA
 * and the analyst; the screen shows a plain, calm message. */
function showFatal(err, retry) {
  const detail = (err && err.message) || String(err);
  hideAllEntries();
  els['fatal-text'].textContent =
    'Something went wrong on our end. Please let the researcher know.';
  els['fatal-retry'].hidden = !retry;
  els['fatal-retry'].onclick = retry
    ? () => { hide(els['fatal']); retry(); }
    : () => hide(els['fatal']);
  els['fatal'].hidden = false;
  state.busy = true;   // nothing is submittable while the notice is up
  sendEvent('validation_blocked', { reason: 'request_failed', detail });
  console.error('[mobi]', detail, err);
}

function show(el, text) {
  if (text !== undefined) el.textContent = text;
  el.hidden = false;
}

function hide(el) { el.hidden = true; }
