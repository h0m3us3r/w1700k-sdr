// SPDX-License-Identifier: MIT
// W1700K spectrum web UI: spectrum, waterfall, stream status and the receiver's controls.
//
// The server sends spectrum frames already fitted to this view (8-bit steps
// of 0.5 dB) and a status message a few times a second; the page asks for
// the range and resolution it shows, acknowledges every frame (which paces
// the server on a slow link) and reconnects by itself when the link drops.
// Receiver settings go to the server as `set` and `run` messages; its
// controller applies them and the status reports what the receiver does.
import {Renderer, NO_DATA, PALETTES, CHANNEL_COLORS, paletteCss} from './render.js';

const $ = (id) => document.getElementById(id);
const clamp = (x, low, high) => Math.min(high, Math.max(low, x));

// ---- preferences (kept in this browser) --------------------------------------------------------

const DEFAULTS = {
    top: -20, range: 80, contrast: 0,       // dBFS at the top of the spectrum, dB shown, waterfall shift
    spectrumAttack: 0.9, spectrumDecay: 0.7, // SDR#'s defaults
    waterfallAttack: 1.0, waterfallDecay: 0.9,
    palette: 'sdrsharp', fill: true, peakHold: false,
    split: 0.42, maxFps: 0, maxKbps: 0, maxBins: -1,  // -1: automatic resolution
    bothLayout: 'overlaid',  // both channels of a two-channel stream: 'overlaid' or 'side' by side
    sidebar: window.innerWidth > 900,
    open: {receiver: true, fft: true, display: false, stream: true, link: false},
    // The analog filter follows the sample rate; asked of the receiver again on every connection.
    filterFollow: true,
    // The FFT settings chosen in this browser (size, window, rate, averaging, peak, dc), asked of
    // the server again on every connection; one not chosen here is the server's.
    fft: {},
};
const prefs = (() => {
    try {
        const saved = JSON.parse(localStorage.getItem('w1700k-spectrum') || '{}');
        return {...DEFAULTS, ...saved, open: {...DEFAULTS.open, ...(saved.open || {})}, fft: {...(saved.fft || {})}};
    } catch {
        return {...DEFAULTS, open: {...DEFAULTS.open}, fft: {}};
    }
})();
let saveTimer = 0;
function savePrefs() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
        try {
            localStorage.setItem('w1700k-spectrum', JSON.stringify(prefs));
        } catch { /* private mode: preferences last for this visit */ }
    }, 300);
}

// ---- state ---------------------------------------------------------------------------------------

let status = null;          // last status message
let hello = null;           // what the server offers: FFT windows, bands, filters, gain steps
let windows = [];           // FFT windows the server offers: {name, bandwidth (bins)}
let maxBins = 4096;         // most bins the server sends in a frame
const spectrum = {key: '', start: 0, stop: 0, bins: 0, target: null, display: null, peak: null, settled: true};
const waterfall = {key: '', row: null};
// One trace and one waterfall a track: track 0 is what is shown of one signal; with both channels of
// a two-channel stream shown, track 0 is channel 0 and track 1 channel 1.
const spectra = [spectrum, {key: '', start: 0, stop: 0, bins: 0, target: null, display: null, peak: null, settled: true}];
const waterfalls = [waterfall, {key: '', row: null}];
const view = {offset: 0, span: 0};     // relative to the LO: centre offset and width, Hz
const pointer = {x: -1, y: -1, inside: false};
let marker = null;                     // Hz
let pendingLo = null, pendingLoAt = 0;
const dirty = {plot: true, overlay: true};
const stats = {frames: 0, shown: 0, merged: 0, bytes: 0, renders: 0, draws: [], age: 0, renderFps: 0, dataFps: 0};
window.w1700k = {stats, prefs, get status() { return status; }, get link() { return link.state; },
                 get playout() { return playout; }};  // for inspection

const canvases = $('canvases');
let renderer;
try {
    renderer = new Renderer($('gl'), $('overlay'));
} catch (e) {
    showMessage(e.message);
    throw e;
}
$('gl').addEventListener('webglcontextlost', (e) => e.preventDefault());
$('gl').addEventListener('webglcontextrestored', () => {
    renderer.setup();
    for (const w of waterfalls) w.key = '';
    dirty.plot = true;
});

// ---- connection -------------------------------------------------------------------------------------

const link = {ws: null, state: 'connecting', retries: 0, retryAt: 0, timer: 0, last: 0, rtt: null,
              clockOffset: 0, replaced: false};

function connect() {
    clearTimeout(link.timer);
    link.replaced = false;
    link.state = 'connecting';
    $('take-over').hidden = true;
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.binaryType = 'arraybuffer';
    link.ws = ws;
    ws.onopen = () => {
        link.state = 'live';
        link.retries = 0;
        link.last = performance.now();
        sendView();
        ping();
        showLink();
    };
    ws.onmessage = (event) => {
        link.last = performance.now();
        if (typeof event.data === 'string') {
            stats.bytes += event.data.length;
            onMessage(JSON.parse(event.data));
        } else {
            stats.bytes += event.data.byteLength;
            onFrame(event.data);
        }
    };
    ws.onclose = (event) => {
        if (link.ws !== ws) return;
        link.ws = null;
        if (event.code === 4001) {
            link.state = 'replaced';
            link.replaced = true;
            showLink();
        } else {
            reconnectLater();
        }
    };
}

function reconnectLater() {
    const delay = Math.min(8000, 500 * 2 ** link.retries) * (0.75 + 0.5 * Math.random());
    link.retries = Math.min(link.retries + 1, 6);
    link.state = 'reconnecting';
    link.retryAt = performance.now() + delay;
    link.timer = setTimeout(connect, delay);
    showLink();
}

// A link that has gone quiet is dead even if the socket has not noticed.
function abandon() {
    const ws = link.ws;
    if (!ws) return;
    ws.onopen = ws.onmessage = ws.onclose = null;
    try { ws.close(); } catch { /* already gone */ }
    link.ws = null;
    reconnectLater();
}

function send(message) {
    if (!link.ws || link.ws.readyState !== WebSocket.OPEN) return false;
    link.ws.send(JSON.stringify(message));
    return true;
}

function ping() { send({t: 'ping', c: performance.now()}); }

setInterval(() => {
    if (link.state === 'live') {
        if (performance.now() - link.last > 5000) abandon();
        else ping();
    }
    showLink();
}, 2000);
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && link.state === 'reconnecting') connect();
    sendView();  // a hidden page asks for fewer frames
});
window.addEventListener('online', () => { if (link.state === 'reconnecting') connect(); });
$('take-over').addEventListener('click', connect);

function onMessage(m) {
    if (m.t === 'status') onStatus(m);
    else if (m.t === 'hello') onHello(m);
    else if (m.t === 'notice') onNotice(m);
    else if (m.t === 'pong') {
        const now = performance.now();
        link.rtt = now - m.c;
        link.clockOffset = m.s - (performance.timeOrigin + (now + m.c) / 2);
    }
}

// ---- frames -----------------------------------------------------------------------------------------

// Frames stand for equal steps of sample time, but they arrive unevenly: the host that receives
// the stream and computes them is busy, so some come tens of milliseconds late and the next few
// right behind them. Shown as they arrive, the waterfall would stall and then jump. So each frame
// is shown at its own place on the frames' clock (the sample time of those before it), a fixed
// delay behind the earliest that frames have come: the spread of their arrival times over the last
// seconds, and a margin. A frame later than any before raises the delay at once (the picture has
// paused for it anyway); the delay falls back slowly, the frames then shown a tenth faster.
const playout = {queue: [], media: 0, shift: 0, target: 0, delay: 0, second: -1, lo: [], hi: [], ran: 0, key: ''};
const PLAYOUT_MIN = 30, PLAYOUT_MAX = 400, PLAYOUT_MARGIN = 25, PLAYOUT_WINDOW = 10;  // ms, ms, ms, s

function onFrame(buffer) {
    const d = new DataView(buffer);
    if (d.getUint8(0) !== 1) return;
    const peakMode = d.getUint8(1) & 1, bins = d.getUint16(2, true), sequence = d.getUint32(4, true);
    // Bit 1 of the flags: the channel the frame is of (0 or 1 of a two-channel stream). One channel
    // is drawn at a time for now; a view of both takes its frames apart by this.
    const channel = (d.getUint8(1) >> 1) & 1;
    send({t: 'ack', seq: sequence});
    const start = d.getFloat64(8, true), stop = d.getFloat64(16, true);
    const base = d.getFloat32(24, true), step = d.getFloat32(28, true);
    const merged = d.getUint32(32, true), time = d.getFloat64(40, true);
    const codes = new Uint8Array(buffer, 48, bins);
    const db = new Float32Array(bins);
    for (let i = 0; i < bins; i++) db[i] = base + codes[i] * step;
    stats.frames++;
    stats.merged += merged - 1;

    // With both channels shown the frames come in pairs over the same samples, channel 0's first:
    // each goes to its own track, and the pair is one step of the display's clock.
    const both = bothShown(), ms = (Math.max(1, merged) * 1000) / (status ? status.fft.rate : 100);  // the sample time it covers
    queueFrame({db, bins, start, stop, time, channel, track: both ? channel : 0,
                key: `${bins}:${start}:${stop}:${peakMode}:${both ? 'both' : channel}`,
                ms: both && channel === 0 ? 0 : ms},
               performance.now(), document.hidden);
}

function queueFrame(frame, now, hidden) {
    const p = playout;
    const restart = () => {
        while (p.queue.length) showFrame(p.queue.shift());
        Object.assign(p, {media: 0, shift: 0, target: 0, delay: 0, second: -1, lo: [], hi: []});
    };
    // Shown at once: on a page that is not being animated (hidden, or a window the browser has
    // stopped animating without saying so), where a queue would only grow; and for a new view or
    // tuning, so that zooming, panning and retuning answer as fast as the link does (what waited
    // belongs to the old view).
    if (hidden || now - p.ran > 250 || frame.key !== p.key) {
        restart();
        p.key = frame.key;
        showFrame(frame);
        return;
    }
    let offset = now - p.media;  // when it came, against the frames' own clock
    // Far later than the delay can cover: frames are missing in between (the stream stopped, or
    // the server skipped some), and the clock starts again here.
    if (p.lo.length && offset - Math.min(...p.lo) > PLAYOUT_MAX + 100) {
        restart();
        offset = now;
    }
    const second = Math.floor(now / 1000);
    if (second !== p.second) {  // the spread is kept per second, over the last PLAYOUT_WINDOW of them
        p.second = second;
        p.lo.unshift(Infinity);
        p.hi.unshift(-Infinity);
        p.lo.length = p.hi.length = Math.min(p.lo.length, PLAYOUT_WINDOW);
    }
    p.lo[0] = Math.min(p.lo[0], offset);
    p.hi[0] = Math.max(p.hi[0], offset);
    const earliest = Math.min(...p.lo);
    p.delay = clamp(Math.max(...p.hi) - earliest + PLAYOUT_MARGIN, PLAYOUT_MIN, PLAYOUT_MAX);
    p.target = earliest + p.delay;
    if (p.target > p.shift) p.shift = p.target;
    frame.at = p.media;
    p.media += frame.ms;
    p.queue.push(frame);
}

// Shows the queued frames that are due; called for every animation frame, `elapsed` ms after the last.
function playFrames(now, elapsed) {
    const p = playout;
    p.ran = now;
    if (p.shift > p.target) p.shift = Math.max(p.target, p.shift - 0.1 * elapsed);
    while (p.queue.length && p.queue[0].at + p.shift <= now) showFrame(p.queue.shift());
}

function showFrame({db, bins, start, stop, time, key, track = 0}) {
    stats.shown++;
    const sp = spectra[track], fall = waterfalls[track];
    if (key !== sp.key) {
        Object.assign(sp, {key, start, stop, bins, display: db.slice(), peak: db.slice()});
    } else if (prefs.peakHold) {
        for (let i = 0; i < bins; i++) sp.peak[i] = Math.max(sp.peak[i], db[i]);
    }
    sp.target = db;
    sp.settled = false;

    // SDR#'s waterfall smoothing, applied from row to row.
    let row = db;
    if (fall.key === key) {
        row = new Float32Array(bins);
        const previous = fall.row, up = prefs.waterfallAttack, down = prefs.waterfallDecay;
        for (let i = 0; i < bins; i++) {
            const delta = db[i] - previous[i];
            row[i] = previous[i] + (delta > 0 ? up : down) * delta;
        }
    }
    fall.key = key;
    fall.row = row;
    renderer.pushRow(row, start, stop, time, track);

    stats.age = performance.timeOrigin + performance.now() + link.clockOffset - time;
    dirty.plot = true;
}

// ---- one signal, or both channels ---------------------------------------------------------------------

// Both channels of a two-channel stream are shown: the server sends a frame of each over the same
// samples, channel 0's first.
function bothShown() { return status?.receiver?.channels === 2 && status?.fft?.channel === 2; }
// 'single': one trace and one waterfall. 'overlaid': both traces on one graph, the waterfall's
// upper half channel 0 and its lower half channel 1. 'side': channel 0's plots in the left half,
// channel 1's in the right, each over the whole view.
function drawMode() { return !bothShown() ? 'single' : prefs.bothLayout === 'side' ? 'side' : 'overlaid'; }
function paneCount() { return drawMode() === 'side' ? 2 : 1; }
function paneWidth() { return canvases.clientWidth / paneCount(); }  // CSS pixels
// The pane a pointer at x is in, and x within it.
function paneAt(x) {
    const w = paneWidth(), n = clamp(Math.floor(x / w), 0, paneCount() - 1);
    return {pane: n, x: x - n * w};
}
let shownMode = 'single';
// A change of what is drawn: the tracks hold other signals than they will, and the frames wanted
// are a pane wide.
function modeChanged() {
    const mode = drawMode();
    if (mode === shownMode) return;
    shownMode = mode;
    for (const sp of spectra) Object.assign(sp, {key: '', target: null, display: null, peak: null, settled: true});
    for (const w of waterfalls) w.key = '';
    renderer.clearWaterfall();
    playout.key = '';
    sendView();
    dirty.plot = dirty.overlay = true;
}

// ---- view ----------------------------------------------------------------------------------------------

function fullSpan() {
    const r = status && status.receiver;
    return r ? {lo: r.lo, rate: r.sample_rate} : null;
}

function minimumSpan() {
    const f = fullSpan(), size = status ? status.fft.size : 4096;
    return f ? Math.max(1000, (f.rate / size) * 8) : 1000;
}

// The visible range in Hz.
function visible() {
    const f = fullSpan();
    if (!f) return spectrum.bins ? {start: spectrum.start, stop: spectrum.stop} : {start: 2.4e9, stop: 2.48e9};
    const span = view.span || f.rate;
    const centre = f.lo + view.offset;
    return {start: centre - span / 2, stop: centre + span / 2};
}

function setView(offset, span) {
    const f = fullSpan();
    if (!f) return;
    span = clamp(span, minimumSpan(), f.rate);
    offset = clamp(offset, -(f.rate - span) / 2, (f.rate - span) / 2);
    if (offset === view.offset && span === view.span) return;
    view.offset = offset;
    view.span = span;
    const zoom = Math.log(f.rate / span) / Math.log(f.rate / minimumSpan());
    $('zoom').value = String(Math.round(1000 * (isFinite(zoom) ? zoom : 0)));
    dirty.plot = dirty.overlay = true;
    sendViewSoon();
}

let viewTimer = 0;
function sendViewSoon() {
    if (!viewTimer) viewTimer = setTimeout(() => { viewTimer = 0; sendView(); }, 90);
}

// Asks for the visible range plus a margin to pan into, at about one bin
// per screen pixel.
function sendView() {
    const f = fullSpan(), v = visible(), width = renderer.paneWidth(drawMode());  // a pane's device pixels
    let start = 0, stop = 0, bins = width;
    if (f) {
        const margin = (v.stop - v.start) * (view.span && view.span < f.rate ? 0.25 : 0);
        start = Math.max(f.lo - f.rate / 2, v.start - margin);
        stop = Math.min(f.lo + f.rate / 2, v.stop + margin);
        bins = Math.round((width * (stop - start)) / (v.stop - v.start));
    }
    const limit = Math.min(renderer.textureWidth, maxBins, prefs.maxBins > 0 ? prefs.maxBins : Infinity);
    send({t: 'view', start, stop, bins: clamp(bins, 16, limit), auto: prefs.maxBins < 0,
          fps: document.hidden ? 2 : prefs.maxFps, kbps: prefs.maxKbps});
}

function frequencyAt(x) {  // x in CSS pixels; every pane shows the whole view
    const v = visible();
    return v.start + (paneAt(x).x / paneWidth()) * (v.stop - v.start);
}

function zoomAround(x, factor) {
    const f = fullSpan();
    if (!f) return;
    const v = visible(), hz = frequencyAt(x), span = clamp((v.stop - v.start) * factor, minimumSpan(), f.rate);
    const start = hz - (paneAt(x).x / paneWidth()) * span;
    setView(start + span / 2 - f.lo, span);
}

function pan(pixels) {
    const v = visible();
    setView(view.offset - (pixels / paneWidth()) * (v.stop - v.start), v.stop - v.start);
}

// Plot layout in CSS pixels.
function layout() {
    const height = canvases.clientHeight, axis = 20;
    const spectrumHeight = Math.round(clamp(prefs.split, 0.12, 0.88) * height);
    return {spectrum: spectrumHeight, axis, waterfall: Math.max(0, height - spectrumHeight - axis)};
}

// ---- pointer and keyboard ------------------------------------------------------------------------

const touches = new Map();
let drag = null;

// Pointer position in the plot, in CSS pixels, whatever element is under it.
function local(e) {
    const box = canvases.getBoundingClientRect();
    return {x: e.clientX - box.left, y: e.clientY - box.top};
}
// The message box and the splitter handle their own pointer events.
const onPlot = (e) => !e.target.closest('#message, #splitter');

canvases.addEventListener('wheel', (e) => {
    if (!onPlot(e)) return;
    e.preventDefault();
    const delta = e.deltaY * (e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1);
    zoomAround(local(e).x, Math.exp(delta * 0.0015));
}, {passive: false});

canvases.addEventListener('pointerdown', (e) => {
    if (!onPlot(e) || e.button > 0) return;
    canvases.setPointerCapture(e.pointerId);
    const p = local(e);
    touches.set(e.pointerId, p);
    drag = touches.size === 1 ? {...p, moved: false, time: performance.now()} : null;
});
canvases.addEventListener('pointermove', (e) => {
    const p = local(e);
    pointer.x = p.x;
    pointer.y = p.y;
    pointer.inside = true;
    dirty.overlay = true;
    const touch = touches.get(e.pointerId);
    if (!touch) return;
    if (touches.size === 2) {
        const [a, b] = [...touches.values()];
        const before = Math.abs(a.x - b.x);
        touch.x = p.x;
        touch.y = p.y;
        const [c, d] = [...touches.values()];
        const after = Math.abs(c.x - d.x);
        if (before > 20 && after > 20) zoomAround((c.x + d.x) / 2, before / after);
        return;
    }
    const dx = p.x - touch.x;
    touch.x = p.x;
    touch.y = p.y;
    if (drag && (drag.moved || Math.hypot(p.x - drag.x, p.y - drag.y) > 4)) {
        drag.moved = true;
        pan(dx);
    }
});
function endPointer(e) {
    if (!touches.has(e.pointerId)) return;
    touches.delete(e.pointerId);
    if (drag && !drag.moved && touches.size === 0 && performance.now() - drag.time < 400) {
        marker = frequencyAt(drag.x);
        dirty.overlay = true;
    }
    drag = null;
}
canvases.addEventListener('pointerup', endPointer);
canvases.addEventListener('pointercancel', endPointer);
canvases.addEventListener('pointerleave', () => {
    pointer.inside = false;
    dirty.overlay = true;
});
canvases.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    marker = null;
    dirty.overlay = true;
});
canvases.addEventListener('dblclick', (e) => {
    if (!onPlot(e)) return;
    tune(Math.round(frequencyAt(local(e).x) / 1000) * 1000, true);
});

$('splitter').addEventListener('pointerdown', (e) => {
    e.stopPropagation();
    const target = $('splitter');
    target.setPointerCapture(e.pointerId);
    const move = (m) => {
        const top = canvases.getBoundingClientRect().top;
        prefs.split = clamp((m.clientY - top - 10) / canvases.clientHeight, 0.12, 0.88);
        placeSplitter();
        dirty.plot = dirty.overlay = true;
        savePrefs();
    };
    const up = () => {
        target.removeEventListener('pointermove', move);
        target.removeEventListener('pointerup', up);
        sendView();
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
});

function placeSplitter() {
    const l = layout();
    $('splitter').style.top = `${l.spectrum + l.axis / 2}px`;
}

document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, select, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
    const middle = canvases.clientWidth / 2;
    if (e.key === '+' || e.key === '=') zoomAround(middle, 1 / 1.5);
    else if (e.key === '-') zoomAround(middle, 1.5);
    else if (e.key === '0') setView(0, Infinity);
    else if (e.key === 'ArrowLeft') pan(canvases.clientWidth / 8);
    else if (e.key === 'ArrowRight') pan(-canvases.clientWidth / 8);
    else if (e.key === 'Escape') { marker = null; dirty.overlay = true; }
    else return;
    e.preventDefault();
});

new ResizeObserver(() => {
    // Beyond two device pixels per CSS pixel the picture looks the same and
    // costs more than twice as much to draw.
    renderer.resize(canvases.clientWidth, canvases.clientHeight, Math.min(2, window.devicePixelRatio || 1));
    placeSplitter();
    dirty.plot = dirty.overlay = true;
    sendViewSoon();
}).observe(canvases);

// ---- drawing ----------------------------------------------------------------------------------------

let traceColumns = new Float32Array(0), peakColumns = new Float32Array(0);
let traceColumns1 = new Float32Array(0), peakColumns1 = new Float32Array(0);   // track 1's

// For each device-pixel column: the maximum of the bins under it, or the
// value interpolated between bins when zoomed in beyond them.
function columns(values, out, sp = spectrum) {
    const w = out.length, v = visible(), bins = sp.bins;
    const scale = bins / (sp.stop - sp.start), step = (v.stop - v.start) / w;
    for (let c = 0; c < w; c++) {
        const b0 = (v.start + c * step - sp.start) * scale, b1 = b0 + step * scale;
        if (b1 <= 0 || b0 >= bins) {
            out[c] = NO_DATA;
        } else if (b1 - b0 >= 1) {
            let peak = -Infinity;
            const last = Math.min(bins, Math.ceil(b1));
            for (let i = Math.max(0, Math.floor(b0)); i < last; i++) peak = Math.max(peak, values[i]);
            out[c] = peak;
        } else {
            const x = clamp((b0 + b1) / 2 - 0.5, 0, bins - 1), i = Math.floor(x), f = x - i;
            out[c] = i + 1 < bins ? values[i] * (1 - f) + values[i + 1] * f : values[i];
        }
    }
}

let lastTime = performance.now(), renderMark = performance.now(), renderCount = 0, dataMark = 0, overlayAt = 0;
function animate(now) {
    requestAnimationFrame(animate);
    const dt = Math.min(0.25, (now - lastTime) / 1000);
    lastTime = now;
    playFrames(now, 1000 * dt);

    // SDR#'s spectrum attack and decay are per FFT frame; applied here in
    // proportion to the time passed, the trace glides at the display rate.
    for (const sp of spectra) {
        if (!sp.target || sp.settled) continue;
        const frames = dt * (status ? status.fft.rate : 40);
        const up = 1 - Math.pow(1 - prefs.spectrumAttack, frames), down = 1 - Math.pow(1 - prefs.spectrumDecay, frames);
        const d = sp.display, t = sp.target;
        let moving = false;
        for (let i = 0; i < d.length; i++) {
            const delta = t[i] - d[i];
            d[i] += (delta > 0 ? up : down) * delta;
            if (delta > 0.05 || delta < -0.05) moving = true;
        }
        sp.settled = !moving;
        dirty.plot = true;
    }

    if (dirty.plot) {
        const started = performance.now(), l = layout();
        const mode = drawMode(), width = renderer.paneWidth(mode);
        if (traceColumns.length !== width) {
            traceColumns = new Float32Array(width);
            peakColumns = new Float32Array(width);
            traceColumns1 = new Float32Array(width);
            peakColumns1 = new Float32Array(width);
        }
        const tracks = [[spectra[0], traceColumns, peakColumns, 0], [spectra[1], traceColumns1, peakColumns1, 1]];
        for (const [sp, trace, peak, n] of mode === 'single' ? tracks.slice(0, 1) : tracks) {
            if (sp.display) {
                columns(sp.display, trace, sp);
                if (prefs.peakHold) columns(sp.peak, peak, sp);
            } else {
                trace.fill(NO_DATA);
            }
            renderer.setColumns(trace, prefs.peakHold ? peak : null, n);
        }
        renderer.draw(l, visible(), {top: prefs.top, bottom: prefs.top - prefs.range, contrast: prefs.contrast / 100,
                                     fill: prefs.fill, peak: prefs.peakHold, mode});
        dirty.plot = false;
        // The overlay shows the trace's level only at the pointer and the marker: with neither it
        // stays as it is, and with one a few readings a second are enough. Redrawing it with
        // every frame made the browser upload a second full-size canvas each time.
        if ((pointer.inside || marker !== null) && now - overlayAt > 100) dirty.overlay = true;
        renderCount++;
        stats.draws.push(performance.now() - started);
        if (stats.draws.length > 240) stats.draws.shift();
    }
    if (dirty.overlay) {
        drawOverlay();
        dirty.overlay = false;
        overlayAt = now;
    }
    if (now - renderMark >= 1000) {
        stats.renderFps = (renderCount * 1000) / (now - renderMark);
        stats.dataFps = ((stats.frames - dataMark) * 1000) / (now - renderMark);
        dataMark = stats.frames;
        renderCount = 0;
        renderMark = now;
    }
}
requestAnimationFrame(animate);

function niceStep(minimum) {
    const power = Math.pow(10, Math.floor(Math.log10(minimum)));
    for (const m of [1, 2, 5, 10]) if (m * power >= minimum) return m * power;
    return 10 * power;
}

function formatHz(hz, step) {
    const decimals = clamp(Math.ceil(-Math.log10(step / 1e6)), 0, 6);
    return (hz / 1e6).toFixed(decimals);
}

const channelCss = (n) => `rgb(${CHANNEL_COLORS[n].map((c) => Math.round(255 * c)).join(', ')})`;

// The grid, labels, marker and pointer above the plots. With both channels side by side there are
// two panes, each over the whole view: the marker and the pointer are drawn in both, at the same
// frequency, each with its own channel's level.
function drawOverlay() {
    const ctx = renderer.context2d, r = renderer.ratio || 1;
    const total = canvases.clientWidth, h = canvases.clientHeight, l = layout(), v = visible();
    const mode = drawMode(), count = paneCount(), w = total / count;
    const at = pointer.inside ? paneAt(pointer.x) : null;
    ctx.setTransform(r, 0, 0, r, 0, 0);
    ctx.clearRect(0, 0, total, h);
    for (let pane = 0; pane < count; pane++) {
        ctx.save();
        ctx.translate(pane * w, 0);
        ctx.beginPath();
        ctx.rect(0, 0, w, h);
        ctx.clip();
        drawPane(ctx, r, pane, w, h, l, v, mode, at);
        ctx.restore();
    }
    if (count > 1) {  // between the panes
        ctx.fillStyle = '#0d1116';
        ctx.fillRect(Math.round(w) - 1, 0, 2, h);
    }
}

function drawPane(ctx, r, pane, w, h, l, v, mode, at) {
    const x = (hz) => ((hz - v.start) / (v.stop - v.start)) * w;
    const top = prefs.top, bottom = prefs.top - prefs.range;
    const y = (db) => ((top - db) / (top - bottom)) * l.spectrum;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';

    // Level grid and labels.
    const dbStep = niceStep((prefs.range * 26) / Math.max(1, l.spectrum));
    ctx.strokeStyle = 'rgba(160, 180, 200, 0.13)';
    ctx.fillStyle = 'rgba(200, 210, 220, 0.75)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let db = Math.ceil(bottom / dbStep) * dbStep; db <= top; db += dbStep) {
        const yy = Math.round(y(db)) + 0.5;
        ctx.moveTo(0, yy);
        ctx.lineTo(w, yy);
    }
    ctx.stroke();
    ctx.textAlign = 'left';
    for (let db = Math.ceil(bottom / dbStep) * dbStep; db <= top; db += dbStep) {
        const yy = y(db);
        if (yy > 8 && yy < l.spectrum - 6) ctx.fillText(`${db}`, 4, yy);
    }

    // Frequency grid, and labels in the strip between the plots.
    const hzStep = niceStep(((v.stop - v.start) * 90) / Math.max(1, w));
    ctx.beginPath();
    for (let f = Math.ceil(v.start / hzStep) * hzStep; f <= v.stop; f += hzStep) {
        const xx = Math.round(x(f)) + 0.5;
        ctx.moveTo(xx, 0);
        ctx.lineTo(xx, l.spectrum);
    }
    ctx.stroke();
    ctx.fillStyle = '#0d1116';
    ctx.fillRect(0, l.spectrum, w, l.axis);
    ctx.fillStyle = 'rgba(200, 210, 220, 0.85)';
    ctx.textAlign = 'center';
    for (let f = Math.ceil(v.start / hzStep) * hzStep; f <= v.stop; f += hzStep) {
        const xx = x(f);
        if (xx > 24 && xx < w - 24) ctx.fillText(formatHz(f, hzStep), xx, l.spectrum + l.axis / 2);
    }

    // The LO (and the DC artefact that sits on it).
    const f = fullSpan();
    if (f) {
        const xx = Math.round(x(f.lo)) + 0.5;
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.18)';
        ctx.setLineDash([3, 4]);
        ctx.beginPath();
        ctx.moveTo(xx, 0);
        ctx.lineTo(xx, l.spectrum);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // Which channel is where. Overlaid: both traces on this graph, the waterfall's upper half
    // channel 0 and its lower half channel 1. Side by side: this pane is one channel's.
    const fallTop = l.spectrum + l.axis, lowerFall = Math.floor(l.waterfall / 2), upperFall = l.waterfall - lowerFall;
    const tag = (text, xx, yy, color, align = 'right') => {
        ctx.font = '11px system-ui, sans-serif';
        const width = ctx.measureText(text).width + 10, left = align === 'right' ? xx - width : xx;
        ctx.fillStyle = 'rgba(10, 13, 17, 0.75)';
        ctx.fillRect(left, yy - 9, width, 18);
        ctx.fillStyle = color;
        ctx.textAlign = 'left';
        ctx.fillText(text, left + 5, yy);
    };
    if (mode === 'side') {
        tag(`Channel ${pane}`, w - 6, 14, channelCss(pane));
    } else if (mode === 'overlaid') {
        tag('Channel 1', w - 6, 14, channelCss(1));
        tag('Channel 0', w - 6 - 70, 14, channelCss(0));
        if (lowerFall > 24) {
            tag('Channel 0', 4, fallTop + 12, channelCss(0), 'left');
            tag('Channel 1', 4, fallTop + upperFall + 12, channelCss(1), 'left');
            ctx.fillStyle = 'rgba(13, 17, 22, 0.9)';
            ctx.fillRect(0, fallTop + upperFall - 1, w, 2);
        }
    }

    // The traces whose level is read at a point of this pane, and the waterfall row under a point.
    const here = mode === 'side' ? [pane] : mode === 'overlaid' ? [0, 1] : [0];
    const levelAt = (px, track) => {
        const values = track ? traceColumns1 : traceColumns, c = Math.round(px * r);
        return c >= 0 && c < values.length && values[c] > NO_DATA ? values[c] : null;
    };
    const levels = (px) => {
        const found = here.map((track) => [track, levelAt(px, track)]).filter(([, level]) => level !== null);
        if (!found.length) return '';
        if (here.length === 1) return `  ${found[0][1].toFixed(1)} dBFS`;
        return '  ' + found.map(([track, level]) => `ch ${track}: ${level.toFixed(1)}`).join('  ') + ' dBFS';
    };
    const rowTimeAt = (py) => {
        const into = py - fallTop;
        if (mode === 'overlaid') return into < upperFall ? renderer.rowTime(Math.floor(into), 0) : renderer.rowTime(Math.floor(into - upperFall), 1);
        return renderer.rowTime(Math.floor(into), mode === 'side' ? pane : 0);
    };
    const label = (text, xx, yy, color) => {
        ctx.font = '12px system-ui, sans-serif';
        const width = ctx.measureText(text).width + 12, left = clamp(xx + 10, 2, w - width - 2);
        ctx.fillStyle = 'rgba(10, 13, 17, 0.88)';
        ctx.fillRect(left, yy - 10, width, 20);
        ctx.fillStyle = color;
        ctx.textAlign = 'left';
        ctx.fillText(text, left + 6, yy);
    };
    const digits = clamp(Math.ceil(-Math.log10((v.stop - v.start) / w / 1e6)), 0, 6);

    if (marker !== null) {
        const xx = Math.round(x(marker)) + 0.5;
        if (xx >= 0 && xx <= w) {
            ctx.strokeStyle = 'rgba(255, 190, 70, 0.9)';
            ctx.beginPath();
            ctx.moveTo(xx, 0);
            ctx.lineTo(xx, h);
            ctx.stroke();
            label(`${(marker / 1e6).toFixed(digits)} MHz${levels(xx)}`, xx, 34, '#ffc766');
        }
    }
    if (at && !drag?.moved) {
        const xx = Math.round(at.x) + 0.5;
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
        ctx.beginPath();
        ctx.moveTo(xx, 0);
        ctx.lineTo(xx, h);
        ctx.stroke();
        let text = `${(frequencyAt(pointer.x) / 1e6).toFixed(digits)} MHz`;
        if (pointer.y < l.spectrum) {
            text += levels(at.x);
            if (marker !== null) text += `  Δ ${((frequencyAt(pointer.x) - marker) / 1e3).toFixed(1)} kHz`;
        } else if (pointer.y > fallTop) {
            const t = rowTimeAt(pointer.y);
            if (t) text += `  ${((Date.now() + link.clockOffset - t) / 1000).toFixed(1)} s ago`;
        }
        label(text, at.x, clamp(pointer.y - 18, 12, h - 12), '#e8eef5');
    }
}

function showMessage(text, takeOver = false) {
    $('message').hidden = !text;
    $('message-text').textContent = text || '';
    $('take-over').hidden = !takeOver;
}

let toastCount = 0;
function toast(text, kind = '') {
    const element = document.createElement('div');
    element.className = `toast ${kind}`;
    element.textContent = text;
    $('toasts').append(element);
    if (++toastCount > 4) $('toasts').firstElementChild?.remove();
    setTimeout(() => { element.remove(); toastCount--; }, 6000);
    return element;
}

// ---- status ----------------------------------------------------------------------------------------

const COUNTERS = [  // label, key, is an error
    ['Slices', 'slices', false],
    ['Packed (28-bit)', 'packed', false],
    ['Lost', 'lost', true],
    ['Skipped (server behind)', 'skipped', false],
    ['Late', 'late', true],
    ['Doubled', 'duplicates', true],
    ['Malformed', 'bad', true],
    ['Socket drops', 'socket_drops', true],
    ['Firmware events', 'events', false],
    ['Partial slices', 'pieces', true],
];
const ERRORS = COUNTERS.filter((c) => c[2]).map((c) => c[1]);
const previousCounters = {};
let ringPeak = 0;
const setStats = (id, rows) => {
    $(id).replaceChildren(...rows.flatMap(([k, value]) => {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = k;
        dd.textContent = value;
        return [dt, dd];
    }));
};

function onHello(m) {
    // A new connection: what the last one said may no longer hold.
    status = null;
    hello = m;
    windows = m.windows;
    maxBins = m.max_bins;
    $('fft-window').replaceChildren(...windows.map((w) => new Option(w.name.replace(/-/g, ' '), w.name)));
    $('band').replaceChildren(...(m.bands || []).map((b) => new Option(b.name, String(b.band))));
    $('lna').max = String(m.max_lna ?? 4);
    $('lpf').max = String(m.max_lpf ?? 9);
    draft = null;
    fftRestored = false;
    followRestored = false;
}

// A refused request arrives twice: as the controller's own log line, then as the request's failure
// ("configure failed: " and the same words). One pop-up says it, in the fuller wording.
let lastNotice = null;
function onNotice(m) {
    const level = m.level || 'error', now = performance.now();
    const repeats = lastNotice && now - lastNotice.at < 5000 && lastNotice.element.isConnected &&
        (m.text === lastNotice.text || m.text.endsWith(`: ${lastNotice.text}`));
    if (repeats) lastNotice.element.textContent = m.text;
    else lastNotice = {element: toast(m.text, level === 'info' ? '' : 'bad'), text: m.text, at: now};
    if (level !== 'info' && pendingLo !== null) {  // a refused tune: show the LO the receiver has
        pendingLo = null;
        showFrequency();
    }
}

function onStatus(m) {
    // Sections that have not changed since the last status are left out;
    // one that is no longer known comes as null.
    const previous = status;
    const s = status = {...(status || {}), ...m};
    const r = s.receiver, before = previous && previous.receiver;
    if (r && (!before || before.lo !== r.lo || before.sample_rate !== r.sample_rate)) {
        // A new LO keeps the same frequencies in view, as far as the new span
        // reaches; a view of the whole span stays whole at a new rate.
        if (before) view.offset += before.lo - r.lo;
        if (!view.span || (before && view.span >= before.sample_rate)) view.span = r.sample_rate;
        setView(view.offset, view.span);
        dirty.plot = dirty.overlay = true;
        sendView();
    }
    if (!previous || previous.fft.size !== s.fft.size) setView(view.offset, view.span);
    restoreFft(s.fft);

    // Top bar. With a controller the run button shows what the receiver does; without one, what arrives.
    const on = managed() ? streaming() : s.running;
    $('run').textContent = on ? '■' : '▶';
    $('run').classList.toggle('on', on);
    $('run').setAttribute('aria-label', on ? 'Stop streaming' : 'Start streaming');
    const states = {running: ['streaming', 'good'], waiting: ['no stream', 'warn']};
    const [stateText, stateClass] = states[s.state] || [s.state, ''];
    setPill('pill-state', stateText, stateClass);
    showControl(s.controller);
    if (pendingLo !== null && r && (Math.abs(r.lo - pendingLo) < 1000 || performance.now() - pendingLoAt > 8000))
        pendingLo = null;
    showFrequency();
    if (r) {
        const radio = radioState()?.radio;
        const half = radio && radio.loaded && radio.capture_rate && radio.capture_rate !== radio.sample_rate;
        // What the stream carries: the samples' depth and how they are packed for the link.
        const stream = radioState()?.stream, packing = stream?.running ? PACKING[stream.packing] : null;
        $('span').textContent = `${r.sample_rate / 1e6} Msps` + (half ? ` (${rateLabel(radio)})` : '') +
            (radio && radio.loaded ? ` · ${RF_NAMES[radio.rf_band] || ''}` : '') +
            (packing ? ` · ${packing.label}${stream.packing_manual ? ' (manual)' : ''}` : '');
        const gbit = stream?.link_mbps ? ` for the ${stream.link_mbps / 1000} Gbit/s link` : '';
        $('span').title = packing
            ? `${packing.detail} ${stream.packing_manual ? 'Set by hand.' : `Chosen automatically${gbit}.`}` : '';
    }

    if (!link.replaced) showMessage(s.state === 'waiting' ? s.message : '');

    showReceiver(s);
    showFft(s);
    showStream(s);
    showLinkStats(s.link);
    enableControls();
}

function setPill(id, text, kind) {
    const pill = $(id);
    pill.textContent = text;
    pill.className = `pill ${kind}`;
}

function showLink() {
    const now = performance.now();
    if (link.state === 'live') setPill('pill-link', `live${link.rtt === null ? '' : ` · ${Math.round(link.rtt)} ms`}`, 'good');
    else if (link.state === 'reconnecting') setPill('pill-link', `reconnecting in ${Math.max(0, Math.ceil((link.retryAt - now) / 1000))} s`, 'warn');
    else if (link.state === 'replaced') setPill('pill-link', 'in use elsewhere', 'bad');
    else setPill('pill-link', 'connecting', 'warn');
    if (link.replaced) showMessage('Another browser is using this receiver now.', true);
    if (link.state !== 'live') {  // unknown until the server is heard again
        setPill('pill-state', 'unknown', '');
        setPill('pill-control', 'unknown', '');
    }
    enableControls();
}

function showStream(s) {
    const t = s.stream, sp = s.spectrum;
    if (t) {
        const capacity = t.ring_slices, used = sp.backlog, fraction = used / capacity;
        ringPeak = Math.max(ringPeak, used);
        const mibOf = (slices) => (slices * 8192) / 1048576;
        $('ring-fill').style.width = `${(100 * fraction).toFixed(2)}%`;
        $('ring-peak').style.left = `calc(${((100 * ringPeak) / capacity).toFixed(2)}% - 1px)`;
        $('ring-mini').style.width = `${Math.max(2, 100 * fraction).toFixed(1)}%`;
        $('ring-mini').style.background = fraction > 0.5 ? 'var(--bad)' : fraction > 0.1 ? 'var(--warn)' : 'var(--good)';
        $('ring-mini-text').textContent = `ring ${mibOf(used).toFixed(mibOf(used) < 10 ? 1 : 0)} MiB`;
        setStats('ring-stats', [
            ['Waiting', `${mibOf(used).toFixed(1)} MiB (${(100 * fraction).toFixed(2)} %)`],
            ['Capacity', `${mibOf(capacity).toFixed(0)} MiB, ${((capacity * 2048) / s.receiver.sample_rate).toFixed(2)} s`],
            ['Peak', `${mibOf(ringPeak).toFixed(1)} MiB`],
            ['FFT workers', `${sp.workers}, ${sp.queued} frames queued`],
            ['FFT plan', sp.plan === 'measured' ? 'measured' : 'estimated, the measured one being found'],
        ]);
        const errors = ERRORS.reduce((sum, k) => sum + (t[k] || 0), 0);
        setPill('pill-errors', errors ? `${errors} errors` : 'no errors', errors ? 'bad' : 'good');
        const rows = [];
        for (const [name, key, isError] of COUNTERS) {
            const tr = document.createElement('tr');
            for (const [i, text] of [name, t[key].toLocaleString('en-US')].entries()) {
                const td = document.createElement('td');
                td.textContent = text;
                if (i && isError && t[key] > 0) td.className = 'bad';
                if (i && isError && previousCounters[key] !== undefined && t[key] > previousCounters[key])
                    td.classList.add('flash');
                tr.append(td);
            }
            rows.push(tr);
        }
        $('counters').replaceChildren(...rows);
        for (const k of ERRORS) previousCounters[k] = t[k];
        setStats('rate-stats', [
            ['Network', `${((t.byte_rate * 8) / 1e9).toFixed(2)} Gbit/s, ${(t.datagram_rate / 1e3).toFixed(1)} k datagrams/s`],
            ['Samples', `${(t.sample_rate / 1e6).toFixed(2)} Msps`],
            ['Packed', `${(100 * t.packed_share).toFixed(1)} % of slices (28-bit)`],
            ['Run', `#${t.run}, ${t.seconds.toFixed(0)} s`],
            ['Spectrum', `${sp.fps.toFixed(1)} frames/s, ${(100 * sp.coverage).toFixed(1)} % of samples` +
                (sp.retuned ? `; ${sp.retuned.toLocaleString('en-US')} frames dropped at retunes` : '')],
        ]);
    }
    const c = s.controller;
    setStats('devices', [
        ['Stream', `UDP ${s.devices.source}`],
        ['Control', s.devices.controller ?? 'none (passive viewer)'],
        ...(c && c.mode === 'managed' ? [['Controller', c.alive ? `running, pid ${c.pid}` : 'not running'],
                                         ['Restarts', String(c.restarts)]] : []),
    ]);
}

function showLinkStats(l) {
    const rows = [
        ['State', link.state],
        ['Round trip', link.rtt === null ? '—' : `${link.rtt.toFixed(0)} ms`],
        ['Data age', stats.frames ? `${Math.max(0, stats.age).toFixed(0)} ms` : '—'],
        ['Smoothing', `shown ${playout.delay.toFixed(0)} ms behind, ${playout.queue.length} frames waiting`],
        ['Frames', `${stats.dataFps.toFixed(1)}/s received, ${(l ? l.merged : 0).toFixed(1)}/s merged`],
        ['Drawing', `${stats.renderFps.toFixed(0)} fps, ${percentile(stats.draws, 0.95).toFixed(2)} ms p95`],
    ];
    if (l) {
        rows.push(['Link', `${l.kbps.toFixed(0)} kbit/s used${l.compressed ? ', compressed' : ''}`]);
        rows.push(['Frames delivered', `${l.delivered.toFixed(0)} kbit/s; empty-link round trip ${(1000 * l.rtt_min).toFixed(0)} ms`]);
        rows.push(['In flight', `${l.in_flight} frames, ${(l.in_flight_bytes / 1024).toFixed(1)} of ${(l.window / 1024).toFixed(1)} KiB`]);
        if (l.bins_cap) rows.push(['Resolution', `${l.bins_cap} bins (reduced for the link)`]);
    }
    setStats('link-stats', rows);
}

function percentile(values, p) {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
}

// ---- frequency display --------------------------------------------------------------------------

const frequency = $('frequency');
const DIGITS = 10;  // Hz, up to 9.999 999 999 GHz
const digitElements = [];
for (let i = 0; i < DIGITS; i++) {
    if (i && (DIGITS - i) % 3 === 0) {
        const dot = document.createElement('span');
        dot.className = 'dot';
        dot.textContent = '.';
        frequency.append(dot);
    }
    const d = document.createElement('span');
    d.className = 'digit';
    d.dataset.place = String(DIGITS - 1 - i);
    frequency.append(d);
    digitElements.push(d);
}

function currentLo() {
    return pendingLo ?? (status && status.receiver ? status.receiver.lo : null);
}

function showFrequency() {
    const lo = currentLo(), text = lo === null ? '' : String(Math.round(lo)).padStart(DIGITS, '0');
    let leading = true;
    digitElements.forEach((d, i) => {
        d.textContent = text ? text[i] : '-';
        leading = leading && text[i] === '0';
        d.classList.toggle('lead', leading);
    });
    frequency.classList.toggle('pending', pendingLo !== null);
    if (document.activeElement !== $('lo') && lo !== null) $('lo').value = (lo / 1e6).toFixed(6);
}

let tuneTimer = 0;
// Retunes the LO, within the band's range. `centre`: also bring that frequency to the middle of the view.
function tune(hz, centre = false) {
    if (!receiverReady()) {
        toast(managed() ? 'The receiver is not available yet.' : 'This server only shows the stream: it cannot tune.', 'bad');
        return;
    }
    if (busy()) {
        toast(`The receiver is busy (${control().busy}).`, 'bad');
        return;
    }
    if (!streaming()) {
        toast('Start the stream first: the stream itself retunes the LO.', 'bad');
        return;
    }
    const range = radioState().rf?.lo_range_hz;
    if (!range) {
        toast('The LO range is not known yet: configure the radio first.', 'bad');
        return;
    }
    hz = Math.round(hz);
    if (hz < range[0] || hz > range[1]) {
        toast(`${(hz / 1e6).toFixed(3)} MHz is outside this band's LO range, ` +
              `${(range[0] / 1e6).toFixed(0)}–${(range[1] / 1e6).toFixed(0)} MHz.`, 'bad');
        return;
    }
    pendingLo = hz;
    pendingLoAt = performance.now();
    const f = fullSpan();
    if (centre && f) setView(hz - f.lo, view.span || f.rate);
    showFrequency();
    clearTimeout(tuneTimer);
    tuneTimer = setTimeout(() => {
        if (!send({t: 'set', lo: hz})) toast('Not connected.', 'bad');
    }, centre ? 0 : 350);
}

function stepDigit(place, direction) {
    const lo = currentLo();
    if (lo === null) return;
    const unit = 10 ** place;
    tune(Math.round(lo / unit) * unit + direction * unit);
}

frequency.addEventListener('wheel', (e) => {
    const d = e.target.closest('.digit');
    if (!d) return;
    e.preventDefault();
    stepDigit(Number(d.dataset.place), e.deltaY < 0 ? 1 : -1);
}, {passive: false});
frequency.addEventListener('click', (e) => {
    const d = e.target.closest('.digit');
    if (!d) return;
    const box = d.getBoundingClientRect();
    stepDigit(Number(d.dataset.place), e.clientY < box.top + box.height / 2 ? 1 : -1);
});
// Typing: Enter, or a digit, while the display has focus.
function startEntry(initial) {
    const entry = $('frequency-entry'), lo = currentLo();
    entry.value = initial ?? (lo === null ? '' : (lo / 1e6).toFixed(6));
    frequency.hidden = true;
    entry.hidden = false;
    entry.focus();
    if (initial === undefined) entry.select();
}
frequency.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault();
        stepDigit(6, e.key === 'ArrowUp' ? 1 : -1);
    } else if (e.key === 'Enter') {
        e.preventDefault();
        startEntry();
    } else if (/^[0-9.]$/.test(e.key)) {
        e.preventDefault();
        startEntry(e.key);
    }
});

// "5250", "5250.5M", "6.1G", "5250000k": MHz unless a unit says otherwise.
function parseFrequency(text) {
    const m = /^\s*([0-9]*\.?[0-9]+)\s*([kMG]?)(hz)?\s*$/i.exec(text);
    if (!m) return null;
    const unit = {k: 1e3, m: 1e6, g: 1e9, '': 1e6}[m[2].toLowerCase()];
    return Number(m[1]) * unit;
}

function finishEntry(apply) {
    const entry = $('frequency-entry');
    if (entry.hidden) return;
    const hz = parseFrequency(entry.value);
    entry.hidden = true;
    frequency.hidden = false;
    if (apply && hz !== null) tune(hz);
    else if (apply) toast('Enter a frequency such as 5250 or 6.105G.', 'bad');
}
$('frequency-entry').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') finishEntry(true);
    else if (e.key === 'Escape') finishEntry(false);
});
$('frequency-entry').addEventListener('blur', () => finishEntry(false));
$('lo').addEventListener('change', () => {
    const hz = parseFrequency($('lo').value);
    if (hz === null) toast('Enter a frequency such as 5250 or 6.105G.', 'bad');
    else tune(hz);
});

// ---- controls -----------------------------------------------------------------------------------------

const touched = new Map();  // control -> time of the user's last change

// Shows a value in a control unless the user is working it.
function showValue(input, value) {
    const at = touched.get(input);  // a control never touched is not being worked
    if (document.activeElement === input || (at !== undefined && performance.now() - at < 1500)) return;
    if (input.type === 'checkbox') input.checked = value;
    else input.value = String(value);
    const output = document.querySelector(`output[for="${input.id}"]`);
    if (output) output.textContent = input.value;
}

function showSegmented(id, value) {
    for (const b of $(id).querySelectorAll('button')) b.classList.toggle('on', b.dataset.v === String(value));
}

function bindSlider(id, onValue) {
    const input = $(id), output = document.querySelector(`output[for="${id}"]`);
    const update = (immediate) => {
        touched.set(input, performance.now());
        if (output) output.textContent = input.value;
        onValue(Number(input.value), immediate);
    };
    input.addEventListener('input', () => update(false));
    input.addEventListener('change', () => update(true));
}

// ---- receiver ---------------------------------------------------------------------------------

const RF_NAMES = {'2g': '2.4 GHz', '5g': '5 GHz', '6g': '6 GHz'};
// The controller's packings. raw, half and full carry the radio's 14-bit I and Q bit for bit; the
// numbered ones are requantised on the unit, each block of 2048 samples with a scale of its own.
const reduced = (bits, each, below) => ({
    label: `${each}, reduced (${bits} b/sample)`,
    detail: `I and Q requantised on the unit to ${each} each, every 2048 samples with their own scale: not ` +
        `lossless, the quantisation noise lies about ${below} dB below each block's peak.`});
const PACKING = {
    split: {label: '7-bit, reduced (14 b/sample)',
            detail: 'I and Q requantised to 7 bits each, every other block of 2048 samples by the radio itself ' +
                'under a shift that follows the signal, the ones between on the unit with their own scale: not ' +
                'lossless, the quantisation noise lies about 41 to 47 dB below the blocks\' peaks.'},
    16: reduced(16, '8-bit', 53),
    14: reduced(14, '7-bit', 47),
    8: reduced(8, '4-bit', 28),
    7: reduced(7, '11-level', 25),
    raw: {label: '14-bit, raw (32 b/sample)', detail: '14-bit I and Q as captured, 32 bits per sample on the link.'},
    half: {label: '14-bit, half packed (30 b/sample)',
           detail: '14-bit I and Q, lossless: every other chunk packed to 28 bits per sample, 30 on average.'},
    full: {label: '14-bit, packed (28 b/sample)',
           detail: '14-bit I and Q, lossless: every chunk packed to 28 bits per sample.'},
};

function control() { return status && status.controller; }  // the server's controller section
function managed() { return control()?.mode === 'managed'; }
function radioState() { return control()?.state || null; }    // the controller's state, once known
function streaming() { return !!radioState()?.stream?.running; }
function busy() { return !!control()?.busy; }
function receiverReady() {
    const c = control();
    return link.state === 'live' && managed() && c.alive && !!c.state;
}

// Receiver settings; a dragged slider sends at most every 250 ms (the server keeps only the latest).
const settingTimers = {};
function setReceiver(name, fields, immediate) {
    clearTimeout(settingTimers[name]?.timer);
    const due = settingTimers[name]?.due || 0, now = performance.now();
    const go = () => {
        settingTimers[name] = {due: performance.now() + 250};
        if (!send({t: 'set', ...fields})) toast('Not connected.', 'bad');
    };
    if (immediate || now >= due) go();
    else settingTimers[name] = {due, timer: setTimeout(go, due - now)};
}

// The band and sample rate being edited, kept until the radio has them. (The LO is set on its own.)
let draft = null;

// The server's rates: {label, sample_rate, capture_rate, bands}; 640/2 is band 2's only.
function rateInfo(label) { return hello?.rates?.find((r) => r.label === label); }
function rateLabel(c) {
    const capture = c.capture_rate || c.sample_rate;
    return hello?.rates?.find((r) => r.sample_rate === c.sample_rate && r.capture_rate === capture)?.label ||
        String(c.sample_rate / 1e6);
}
function rateOnBand(label, band) { return !!rateInfo(label)?.bands.includes(band); }

function formConfig() {
    const on = $('rate').querySelector('button.on'), r = on ? rateInfo(on.dataset.v) : null;
    return {band: Number($('band').value), sample_rate: r ? r.sample_rate : null, capture_rate: r ? r.capture_rate : null};
}

function radioConfig() {
    const r = radioState()?.radio;
    return r && r.loaded ? {band: r.band, sample_rate: r.sample_rate, capture_rate: r.capture_rate || r.sample_rate} : null;
}

function applied(want, have) {
    return !!have && want.band === have.band && want.sample_rate === have.sample_rate &&
        want.capture_rate === have.capture_rate;
}

function bandInfo(index) { return hello?.bands?.find((b) => b.band === index); }

function editConfig() {
    const on = $('rate').querySelector('button.on'), band = Number($('band').value);
    if (on && !rateOnBand(on.dataset.v, band)) showSegmented('rate', '320');   // 640/2 is band 2's only
    draft = formConfig();
    showConfigNote();
    enableControls();
}

function showConfigNote() {
    const c = draft || radioConfig();
    if (!c) {
        $('configure-note').textContent = 'The radio is not configured yet.';
        return;
    }
    const band = bandInfo(c.band);
    let text = `${band ? band.name : `Band ${c.band}`} at ${c.sample_rate / 1e6} Msps` +
        (c.capture_rate && c.capture_rate !== c.sample_rate
            ? `, every other sample of ${c.capture_rate / 1e6}: flat to about ±155 MHz, signals 160–320 MHz away fold in.`
            : '.');
    if (draft && !applied(draft, radioConfig())) text += ' Not applied yet.';
    $('configure-note').textContent = text;
}

function showGainOutputs() {
    $('lna').nextElementSibling.textContent = `${$('lna').value}: ≈${6 * Number($('lna').value)} dB`;
    $('lpf').nextElementSibling.textContent = `${$('lpf').value}: ≈${2 * Number($('lpf').value)} dB`;
}

function showControl(c) {
    const pill = $('pill-control');
    if (!c || c.mode !== 'managed') {
        setPill('pill-control', 'view only', '');
        pill.title = 'This server only shows the stream (--no-controller).';
    } else if (!c.alive) {
        setPill('pill-control', 'controller down', 'bad');
        pill.title = c.error || 'The controller is not running.';
    } else if (c.busy) {
        const p = c.progress, fraction = p && p.fraction !== null ? ` ${Math.round(100 * p.fraction)} %` : '';
        setPill('pill-control', `${c.busy}${fraction}`, 'warn');
        pill.title = p && p.text ? p.text : c.busy;
    } else if (!c.state) {
        setPill('pill-control', 'controller starting', 'warn');
        pill.title = c.command;
    } else if (c.state.error) {
        setPill('pill-control', 'receiver error', 'bad');
        pill.title = c.state.error;
    } else {
        setPill('pill-control', 'ready', 'good');
        pill.title = `Controlled by ${c.command}`;
    }
}

function showReceiver(s) {
    const r = s.receiver, c = s.controller, st = c?.state;
    if (r) {
        let note = `${(r.lo / 1e6).toFixed(3)} MHz, ${r.sample_rate / 1e6} Msps: ` +
            `${((r.lo - r.sample_rate / 2) / 1e6).toFixed(0)}–${((r.lo + r.sample_rate / 2) / 1e6).toFixed(0)} MHz.`;
        const range = st?.rf?.lo_range_hz;
        if (range) note += ` LO range ${(range[0] / 1e6).toFixed(0)}–${(range[1] / 1e6).toFixed(0)} MHz.`;
        $('lo-note').textContent = note;
    }

    $('busy').hidden = !c?.busy;
    if (c?.busy) {
        const p = c.progress, known = p && p.fraction !== null;
        $('busy-text').textContent = c.busy[0].toUpperCase() + c.busy.slice(1) + (p && p.text ? `: ${p.text}` : '') +
            (known ? ` (${Math.round(100 * p.fraction)} %)` : '') + '…';
        $('busy-fill').classList.toggle('unknown', !known);
        $('busy-fill').style.width = known ? `${(100 * p.fraction).toFixed(1)}%` : '';
    }

    if (st) {
        if (draft && applied(draft, radioConfig())) draft = null;
        const config = radioConfig();
        if (!draft && config) {
            showValue($('band'), config.band);
            showSegmented('rate', rateLabel(config));
        }
        const rf = st.rf || {};
        showSegmented('filter', rf.filter_mhz);
        showValue($('filter-follow'), !!rf.filter_follows_rate);
        restoreFollow(rf);
        showValue($('flatten'), !!r?.flatten);
        showValue($('agc'), !!rf.agc);
        if (rf.lna !== null && rf.lna !== undefined) showValue($('lna'), rf.lna);
        if (rf.lpf !== null && rf.lpf !== undefined) showValue($('lpf'), rf.lpf);
        showGainOutputs();
    }
    showConfigNote();
    // A running stream shows what it is; a stopped one, what the next start asks for.
    const two = r?.channels === 2;
    if (streaming()) showValue($('two-channels'), two);
    $('two-channels').disabled = streaming();
    $('channels-note').textContent = two ? 'The stream is of two channels: chains 0 and 1, one gain on both.'
        : streaming() && $('two-channels').checked ? '' : !streaming() && $('two-channels').checked ? 'Applies from the next start.' : '';
    $('fft-channel-row').hidden = !two;
    $('flatten-note').textContent = !r?.flatten ? ''
        : r.flatten_active ? 'The stream is corrected: the decimation\'s roll-off is divided out of the samples.'
        : r.flatten_available ? 'Applies from the next slices.'
        : 'Waits for a filter as wide as the sample rate (40, 80, 160 or 320; not 640/2).';

    const note = $('receiver-note'), parts = [];
    let bad = false;
    if (!c || c.mode !== 'managed') {
        parts.push('View only: the server runs with --no-controller, so the receiver is controlled elsewhere.');
    } else if (!c.alive) {
        parts.push(`The controller is not running: ${c.error || 'it is restarted shortly'}.`);
        bad = true;
    } else if (st?.error) {
        parts.push(st.error);
        bad = true;
    }
    note.textContent = parts.join(' ');
    note.classList.toggle('bad', bad);
}

function enableControls() {
    const live = link.state === 'live', idle = receiverReady() && !busy();
    // The LO, gain and filter are firmware commands: whenever the radio is loaded, streaming or not.
    const rf = !!radioState()?.radio?.loaded;
    for (const e of $('panel-receiver').querySelectorAll('input, button, select')) e.disabled = !idle;
    if (idle) {
        $('lo').disabled = !rf;
        for (const b of $('filter').querySelectorAll('button')) b.disabled = !rf;
        $('filter-follow').disabled = !rf;
        $('agc').disabled = !rf;
        $('lna').disabled = $('lpf').disabled = !rf || $('agc').checked;
        const band = Number($('band').value);
        for (const b of $('rate').querySelectorAll('button')) b.disabled = !rateOnBand(b.dataset.v, band);
        const changed = !!draft && !applied(draft, radioConfig());
        $('configure').disabled = !changed;
        $('configure').classList.toggle('changed', changed);
    }
    for (const e of $('panel-fft').querySelectorAll('input, button, select')) e.disabled = !live;
    $('run').disabled = !idle;
    frequency.classList.toggle('locked', !(idle && rf));
}

$('run').addEventListener('click', () => {
    // Two channels are asked for with the start; a stream that runs keeps what it was started with.
    if (!send({t: 'run', on: !streaming(), channels: $('two-channels').checked ? 2 : 1})) toast('Not connected.', 'bad');
});
$('band').addEventListener('change', editConfig);
for (const b of $('rate').querySelectorAll('button'))
    b.addEventListener('click', () => {
        showSegmented('rate', b.dataset.v);
        editConfig();
    });
$('configure').addEventListener('click', () => {
    const c = formConfig();
    if (!c.sample_rate) {
        toast('Choose a sample rate.', 'bad');
        return;
    }
    draft = c;
    if (!send({t: 'set', ...c})) toast('Not connected.', 'bad');
});
// A width chosen by hand ends the filters' following the sample rate (the receiver's rule, and
// what this browser then remembers); the checkbox starts it again, on every band.
function rememberFollow(on) {
    prefs.filterFollow = on;
    savePrefs();
}
for (const b of $('filter').querySelectorAll('button'))
    b.addEventListener('click', () => {
        rememberFollow(false);
        setReceiver('filter', {filter: Number(b.dataset.v)}, true);
    });
$('filter-follow').addEventListener('change', () => {
    touched.set($('filter-follow'), performance.now());
    rememberFollow($('filter-follow').checked);
    setReceiver('filter_follow', {filter_follow: $('filter-follow').checked}, true);
});
// Once per connection, when the receiver is there to ask: what this browser remembers. A
// receiver loaded afresh follows the rate, and another browser or the command line may have
// left it otherwise.
let followRestored = false;
function restoreFollow(rf) {
    if (followRestored || !receiverReady() || busy() || !radioState()?.radio?.loaded) return;
    followRestored = true;
    if (typeof rf.filter_follows_rate === 'boolean' && rf.filter_follows_rate !== prefs.filterFollow)
        send({t: 'set', filter_follow: prefs.filterFollow});
}
function sendGain(immediate) {
    if ($('agc').checked) setReceiver('gain', {agc: true}, true);
    else setReceiver('gain', {agc: false, lna: Number($('lna').value), lpf: Number($('lpf').value)}, immediate);
}
$('flatten').addEventListener('change', () => {
    touched.set($('flatten'), performance.now());
    setReceiver('flatten', {flatten: $('flatten').checked}, true);
});
$('agc').addEventListener('change', () => {
    touched.set($('agc'), performance.now());
    sendGain(true);
    enableControls();
});
bindSlider('lna', (_, immediate) => {
    showGainOutputs();
    sendGain(immediate);
});
bindSlider('lpf', (_, immediate) => {
    showGainOutputs();
    sendGain(immediate);
});

// FFT settings belong to the server (every frame is computed once).
const AVERAGING = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 0];
const SIZES = Array.from({length: 10}, (_, k) => 512 * 2 ** k);
$('fft-size').replaceChildren(...SIZES.map((n) => new Option(n.toLocaleString('en-US'), String(n))));
// Only the fields that changed: the server keeps the rest, so quick
// successive changes cannot undo each other. What is chosen here is remembered
// in this browser, and asked of the server again whenever the page connects:
// the server starts with its own defaults, and another browser may have set others.
function sendFft(changes) {
    Object.assign(prefs.fft, changes);
    savePrefs();
    if (!send({t: 'fft', ...changes})) toast('Not connected.', 'bad');
}

let fftRestored = false;  // this connection has been asked for the remembered settings
function restoreFft(current) {
    if (fftRestored) return;
    fftRestored = true;
    const usable = {  // what a newer or older server may no longer offer is forgotten, not asked for
        size: (v) => SIZES.includes(v),
        window: (v) => windows.some((w) => w.name === v),
        rate: (v) => typeof v === 'number' && v >= 1 && v <= 100,
        averaging: (v) => AVERAGING.includes(v),
        peak: (v) => typeof v === 'boolean',
        dc: (v) => typeof v === 'boolean',
        channel: (v) => v === 0 || v === 1 || v === 2,
    };
    const changes = {};
    for (const [key, value] of Object.entries(prefs.fft)) {
        if (!usable[key]?.(value)) delete prefs.fft[key];
        else if (current[key] !== value) changes[key] = value;
    }
    if (Object.keys(changes).length) send({t: 'fft', ...changes});
}

// The server's own settings again (it names them in its hello), and nothing remembered here.
const FFT_DEFAULTS = {size: 262144, window: 'blackman-harris-4', rate: 100, averaging: 0, peak: true, dc: false};
$('fft-defaults').addEventListener('click', () => {
    prefs.fft = {};
    savePrefs();
    for (const id of ['fft-size', 'fft-window', 'fft-rate', 'fft-averaging', 'fft-dc']) touched.delete($(id));
    if (!send({t: 'fft', ...(hello?.fft_defaults || FFT_DEFAULTS)})) toast('Not connected.', 'bad');
});
$('fft-size').addEventListener('change', () => sendFft({size: Number($('fft-size').value)}));
$('fft-window').addEventListener('change', () => sendFft({window: $('fft-window').value}));
bindSlider('fft-rate', (v, immediate) => { if (immediate) sendFft({rate: v}); });
bindSlider('fft-averaging', (v, immediate) => {
    $('fft-averaging').nextElementSibling.textContent = AVERAGING[v] || 'all';
    if (immediate) sendFft({averaging: AVERAGING[v]});
});
for (const b of $('fft-channel').querySelectorAll('button'))
    b.addEventListener('click', () => sendFft({channel: Number(b.dataset.v)}));
for (const b of $('both-layout').querySelectorAll('button'))
    b.addEventListener('click', () => {
        prefs.bothLayout = b.dataset.v;
        savePrefs();
        showSegmented('both-layout', prefs.bothLayout);
        modeChanged();
    });
for (const b of $('fft-detector').querySelectorAll('button'))
    b.addEventListener('click', () => sendFft({peak: b.dataset.v === 'peak'}));
$('fft-dc').addEventListener('change', () => sendFft({dc: $('fft-dc').checked}));

function showFft(s) {
    const f = s.fft;
    showValue($('fft-size'), f.size);
    if (windows.length) showValue($('fft-window'), f.window);
    showValue($('fft-rate'), f.rate);
    const index = AVERAGING.indexOf(f.averaging);
    showValue($('fft-averaging'), index < 0 ? 0 : index);
    if (document.activeElement !== $('fft-averaging')) $('fft-averaging').nextElementSibling.textContent = f.averaging || 'all';
    showSegmented('fft-detector', f.peak ? 'peak' : 'average');
    showValue($('fft-dc'), f.dc);
    showSegmented('fft-channel', f.channel || 0);
    showSegmented('both-layout', prefs.bothLayout);
    $('both-layout-row').hidden = !bothShown();
    modeChanged();
    const rate = s.receiver ? s.receiver.sample_rate : 80e6, bin = rate / f.size;
    const rbw = bin * (windows.find((w) => w.name === f.window)?.bandwidth || 1);
    $('fft-note').textContent = `Bins ${(bin / 1e3).toFixed(2)} kHz apart, resolution bandwidth ${(rbw / 1e3).toFixed(2)} kHz. ` +
        `${s.spectrum.fps.toFixed(1)} frames/s from ${(100 * s.spectrum.coverage).toFixed(s.spectrum.coverage < 0.1 ? 2 : 0)} % of the samples` +
        (s.spectrum.pairs_dropped > 0 ? `; ${(s.spectrum.pairs_dropped / 1e6).toFixed(1)} Msps skipped (busy).` : '.');
}

// Display preferences (this browser only).
const preferences = new Map();  // key -> show its value in its control, and apply it
function bindPreference(id, key, apply) {
    const input = $(id);
    const read = () => (input.type === 'checkbox' ? input.checked
                        : typeof DEFAULTS[key] === 'number' ? Number(input.value) : input.value);
    const output = document.querySelector(`output[for="${id}"]`);
    const show = () => {
        if (input.type === 'checkbox') input.checked = prefs[key];
        else input.value = String(prefs[key]);
        if (output) output.textContent = input.value;
    };
    show();
    preferences.set(key, () => {
        show();
        apply?.();
    });
    input.addEventListener(input.type === 'range' ? 'input' : 'change', () => {
        prefs[key] = read();
        if (output) output.textContent = input.value;
        savePrefs();
        apply?.();
        dirty.plot = dirty.overlay = true;
    });
}
$('palette').replaceChildren(...Object.keys(PALETTES).map((name) => new Option(
    {sdrsharp: 'SDR#', classic: 'SDR# classic', turbo: 'Turbo', viridis: 'Viridis', gray: 'Gray'}[name], name)));
const applyPalette = () => {
    renderer.setPalette(prefs.palette);
    $('palette-swatch').style.background = paletteCss(prefs.palette);
};
applyPalette();
bindPreference('palette', 'palette', applyPalette);
bindPreference('s-attack', 'spectrumAttack');
bindPreference('s-decay', 'spectrumDecay');
bindPreference('w-attack', 'waterfallAttack');
bindPreference('w-decay', 'waterfallDecay');
bindPreference('fill', 'fill');
bindPreference('peak-hold', 'peakHold', () => { for (const sp of spectra) if (sp.target) sp.peak = sp.display.slice(); });
bindPreference('contrast', 'contrast');
bindPreference('range', 'range');
bindPreference('offset', 'top');
bindPreference('max-fps', 'maxFps', sendView);
bindPreference('max-kbps', 'maxKbps', sendView);
bindPreference('max-bins', 'maxBins', sendView);
// A section's preferences back to what a browser that has never seen the page starts with.
function resetPreferences(keys) {
    for (const key of keys) {
        prefs[key] = DEFAULTS[key];
        preferences.get(key)();
    }
    savePrefs();
    dirty.plot = dirty.overlay = true;
}
$('display-defaults').addEventListener('click', () => resetPreferences(
    ['spectrumAttack', 'spectrumDecay', 'waterfallAttack', 'waterfallDecay', 'palette', 'fill', 'peakHold',
     'contrast', 'range', 'top']));
$('link-defaults').addEventListener('click', () => resetPreferences(['maxFps', 'maxKbps', 'maxBins']));
$('peak-reset').addEventListener('click', () => {
    for (const sp of spectra) if (sp.display) sp.peak = sp.display.slice();
    dirty.plot = true;
});
$('clear-waterfall').addEventListener('click', () => {
    renderer.clearWaterfall();
    dirty.plot = true;
});
$('auto-levels').addEventListener('click', () => {
    if (!traceColumns.length) return;
    const shown = drawMode() === 'single' ? [...traceColumns] : [...traceColumns, ...traceColumns1];
    const values = shown.filter((v) => v > NO_DATA).sort((a, b) => a - b);
    if (!values.length) return;
    const floor = values[Math.floor(values.length * 0.1)], peak = values[values.length - 1];
    prefs.top = clamp(Math.ceil((peak + 8) / 10) * 10, -160, 20);
    prefs.range = clamp(prefs.top - Math.floor((floor - 15) / 10) * 10, 10, 200);
    $('offset').value = String(prefs.top);
    $('range').value = String(prefs.range);
    savePrefs();
    dirty.plot = dirty.overlay = true;
});
$('zoom').addEventListener('input', () => {
    const f = fullSpan();
    if (!f) return;
    const z = Number($('zoom').value) / 1000;
    setView(view.offset, f.rate * Math.pow(minimumSpan() / f.rate, z));
});

// Sidebar and panels.
// On a narrow screen the controls cover the plot, so they start closed there.
const narrow = window.matchMedia('(max-width: 900px)');
let sidebarOpen = narrow.matches ? false : prefs.sidebar;
function showSidebar() {
    document.body.classList.toggle('sidebar-closed', !sidebarOpen);
    $('menu').setAttribute('aria-expanded', String(sidebarOpen));
}
$('menu').addEventListener('click', () => {
    sidebarOpen = !sidebarOpen;
    if (!narrow.matches) {
        prefs.sidebar = sidebarOpen;
        savePrefs();
    }
    showSidebar();
});
showSidebar();
for (const panel of document.querySelectorAll('details[data-panel]')) {
    panel.open = prefs.open[panel.dataset.panel];
    panel.addEventListener('toggle', () => {
        prefs.open[panel.dataset.panel] = panel.open;
        savePrefs();
    });
}

enableControls();
connect();
