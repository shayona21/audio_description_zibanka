import { MultitrackPlayer } from './player.js';
import { LaneView } from './lanes.js';
import { ScriptPanel, renderDialogue } from './script.js';

const $ = id => document.getElementById(id);
const MB = 1024 * 1024;
const LIMITS = { script: 20 * MB, video: 500 * MB, run: 500 * MB };
const POLL_MS = 800;
const PREROLL = 0.75;     // seconds of picture before a line when auditioning it
const POSTROLL = 0.4;     // and after it
const VIDEO_EXT = ['.mp4', '.m4v', '.webm', '.mov'];
const ALIGN_TOLERANCE_MS = 500;
// One colour per character, in order of first appearance; shared by the strip, lanes and panel.
const CAST_COLOURS = ['#2563a8', '#d9480f', '#2b8a3e', '#9c36b5', '#c2255c',
                      '#0b7285', '#b35c00', '#5f3dc4', '#5c940d', '#495057'];
const colourFor = i => CAST_COLOURS[i % CAST_COLOURS.length];

const state = {
    source: 'generate',   // 'generate': dub the script here; 'v2': review an AD Tool V2 run
    script: { file: null, status: 'empty', summary: null, error: '', check: 0 },
    // The chosen AD Tool V2 run: details = { id, name, fps, episodes: [summary] }
    run: { status: 'empty', id: null, name: '', meta: '', details: null, episode: null, error: '', check: 0, xhr: null },
    runs: { status: 'idle', list: [] },     // finished runs on this server
    video: { file: null, status: 'empty', durationMs: null, error: '', url: null },
    dub: null,            // { id, record, upload: {status, loaded, total, error}, poll }
    session: null,
    player: null,
    lanes: null,
    panel: null,          // script panel (Block 4) with the line editor (Block 5)
};

/* ================================================================ helpers */

function sizeText(bytes) {
    return bytes >= MB ? `${(bytes / MB).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function clockText(ms) {
    const total = Math.round(ms / 1000);
    const h = Math.floor(total / 3600), m = Math.floor(total / 60) % 60, s = total % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
function timecode(seconds, fps) {
    const frames = Math.floor(Math.max(0, seconds) * fps + 1e-4);
    const ff = frames % fps, total = Math.floor(frames / fps);
    const pad = n => String(n).padStart(2, '0');
    return `${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}:${pad(ff)}`;
}
function extOf(name) {
    const dot = name.lastIndexOf('.');
    return dot < 0 ? '' : name.slice(dot).toLowerCase();
}
function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }
function el(tag, props = {}, ...children) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
}
let toastTimer;
function toast(message) {
    const box = $('toast');
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 2400);
}
function discardSession(id) {
    if (id) fetch(`/session/${id}`, { method: 'DELETE' }).catch(() => {});
}

/** POST a FormData with upload progress (fetch cannot report upload progress). */
function send(url, formData, onProgress) {
    const xhr = new XMLHttpRequest();
    const promise = new Promise((resolve, reject) => {
        xhr.open('POST', url);
        xhr.responseType = 'json';
        xhr.upload.addEventListener('progress', event => {
            if (event.lengthComputable) onProgress(event.loaded, event.total);
        });
        xhr.addEventListener('load', () => {
            const data = xhr.response || {};
            if (xhr.status >= 200 && xhr.status < 300) resolve(data);
            else {
                const error = new Error(data.error || `The server answered HTTP ${xhr.status}.`);
                error.errors = data.errors || [];
                reject(error);
            }
        });
        xhr.addEventListener('error', () => reject(new Error('The connection to the server dropped. Check the app is running, then try again.')));
        xhr.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { aborted: true })));
        xhr.send(formData);
    });
    return { xhr, promise };
}

/* ================================================================ settings */

function settingsText() {
    const sheet = $('sheet-name').value.trim();
    return `${$('fps').value} fps, ${sheet ? `worksheet "${sheet}"` : 'first worksheet'}`;
}
$('settings-toggle').addEventListener('click', () => {
    const open = $('settings').hidden;
    $('settings').hidden = !open;
    $('settings-toggle').setAttribute('aria-expanded', String(open));
    $('settings-toggle').textContent = open ? 'Done' : 'Change';
});
$('format-toggle').addEventListener('click', () => {
    const open = $('format-help').hidden;
    $('format-help').hidden = !open;
    $('format-toggle').setAttribute('aria-expanded', String(open));
});
let sheetTimer;
$('fps').addEventListener('change', () => { $('settings-summary').textContent = settingsText(); recheckScript(); });
$('sheet-name').addEventListener('input', () => {
    $('settings-summary').textContent = settingsText();
    clearTimeout(sheetTimer);
    sheetTimer = setTimeout(recheckScript, 450);
});
function settingsForm(form) {
    form.append('fps', $('fps').value);
    form.append('sheet', $('sheet-name').value);
    return form;
}


/* ================================================================ script */

function recheckScript() {
    if (state.script.file) checkScript(state.script.file);
    // An uploaded ZIP is read with the settings (its frame rate is found from its report anyway).
    if (state.run.id && state.run.id.startsWith('up-') && state.run.status !== 'checking') readRun(state.run.id);
}

async function checkScript(file) {
    const check = ++state.script.check; // answers to superseded checks are ignored
    Object.assign(state.script, { file, status: 'checking', summary: null, error: '' });
    render();
    const form = settingsForm(new FormData());
    form.append('script', file);
    let result = { status: 'error', summary: null, error: '' };
    try {
        const response = await fetch('/check-script', { method: 'POST', body: form });
        const body = await response.json().catch(() => ({}));
        result = response.ok ? { status: 'ok', summary: body.summary, error: '' }
            : { status: 'error', summary: null, error: body.error || `The server could not check the script (HTTP ${response.status}).` };
    } catch (_) {
        result.error = 'The server is not responding. Check the app is running, then try again.';
    }
    if (check !== state.script.check) return;
    Object.assign(state.script, result);
    render();
}

function selectScript(file) {
    if (!file) return;
    $('form-error').hidden = true;
    let problem = '';
    if (extOf(file.name) !== '.xlsx') problem = 'This is not an .xlsx workbook. Save the script from Excel as .xlsx.';
    else if (file.size > LIMITS.script) problem = `The script is ${sizeText(file.size)}; the limit is 20 MB.`;
    if (problem) {
        state.script.check++;
        Object.assign(state.script, { file, status: 'error', summary: null, error: problem });
        render();
        return;
    }
    checkScript(file);
}

/* ================================================================ source and AD Tool V2 run */

function setSource(source) {
    state.source = source;
    document.querySelectorAll('[data-source]').forEach(btn =>
        btn.setAttribute('aria-checked', String(btn.dataset.source === source)));
    $('script-slot').hidden = source !== 'generate';
    $('run-slot').hidden = source !== 'v2';
    $('form-error').hidden = true;
    if (source === 'v2' && state.runs.status !== 'loading') loadRuns();
    render();
}
document.querySelectorAll('[data-source]').forEach(btn =>
    btn.addEventListener('click', () => setSource(btn.dataset.source)));

function dateText(seconds) {
    if (!seconds) return '';
    return new Date(seconds * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

async function loadRuns() {
    state.runs.status = 'loading';
    renderRunList();
    try {
        const response = await fetch('/v2-runs', { cache: 'no-store' });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error);
        state.runs = { status: 'ok', list: body.runs };
    } catch (_) {
        state.runs = { status: 'error', list: [] };
    }
    renderRunList();
}

function renderRunList() {
    const { status, list } = state.runs;
    if (status !== 'ok' || !list.length) {
        const text = status === 'loading' ? 'Looking for finished runs…'
            : status === 'error' ? "Couldn't list the runs on this server. Upload the ZIP instead."
            : 'No finished AD Tool V2 runs on this server yet. Upload a ZIP instead.';
        $('run-list').replaceChildren(el('li', { className: 'run-empty' }, text));
        return;
    }
    $('run-list').replaceChildren(...list.map(run => {
        const episodes = run.episodes.length === 1 ? `Episode ${run.episodes[0]}` : `${run.episodes.length} episodes`;
        const meta = `${episodes}, ${dateText(run.finished)}`;
        const button = el('button', { type: 'button', className: 'run-item', title: `${run.name}: ${plural(run.rows || 0, 'line')}, ${run.fps} fps` },
            el('b', {}, run.name), el('span', {}, meta));
        button.addEventListener('click', () => {
            Object.assign(state.run, { name: run.name, meta });
            readRun(run.id);
        });
        return el('li', {}, button);
    }));
}

/** Read a run's episodes from the server (a run on this server, or a ZIP uploaded before). */
async function readRun(id) {
    const run = state.run;
    if (run.xhr) { run.xhr.abort(); run.xhr = null; }   // a ZIP still uploading is replaced
    const check = ++run.check;
    Object.assign(run, { id, status: 'checking', details: null, error: '' });
    render();
    const query = new URLSearchParams({ fps: $('fps').value, sheet: $('sheet-name').value });
    let result;
    try {
        const response = await fetch(`/v2-runs/${id}?${query}`, { cache: 'no-store' });
        const body = await response.json().catch(() => ({}));
        result = response.ok ? { details: body } : { error: body.error || `The server could not read this run (HTTP ${response.status}).` };
    } catch (_) {
        result = { error: 'The server is not responding. Check the app is running, then try again.' };
    }
    if (check !== run.check) return;
    setRunResult(result);
}

function setRunResult({ details, error }) {
    const run = state.run;
    if (details) {
        const keep = details.episodes.some(e => e.episode === run.episode);
        Object.assign(run, { status: 'ok', details, error: '', episode: keep ? run.episode : details.episodes[0].episode });
    } else {
        Object.assign(run, { status: 'error', details: null, error });
    }
    render();
}

function uploadRun(file) {
    if (!file) return;
    $('form-error').hidden = true;
    const run = state.run;
    if (run.xhr) run.xhr.abort();
    const check = ++run.check;
    Object.assign(run, { id: null, name: file.name, meta: sizeText(file.size), details: null, error: '' });
    if (extOf(file.name) !== '.zip') {
        Object.assign(run, { status: 'error', error: 'This is not a ZIP. Upload the ZIP that AD Tool V2 delivered.' });
        render();
        return;
    }
    if (file.size > LIMITS.run) {
        Object.assign(run, { status: 'error', error: `The ZIP is ${sizeText(file.size)}; the limit is 500 MB.` });
        render();
        return;
    }
    run.status = 'checking';
    render();
    const form = settingsForm(new FormData());
    form.append('dub', file);
    const job = send('/v2-runs', form, (loaded, total) => {
        if (check === run.check) $('run-progress').style.width = `${Math.round(loaded / total * 100)}%`;
    });
    run.xhr = job.xhr;
    job.promise.then(details => {
        if (check !== run.check) return;
        run.xhr = null;
        run.id = details.id;
        setRunResult({ details });
    }, error => {
        if (check !== run.check || error.aborted) return;
        run.xhr = null;
        setRunResult({ error: error.message });
    });
}

function runSummary() {
    const run = state.run;
    if (run.status !== 'ok') return null;
    return run.details.episodes.find(e => e.episode === run.episode) || null;
}

function renderRunSlot() {
    const run = state.run;
    $('run-progress').style.width = run.status === 'checking' && !run.id ? $('run-progress').style.width : '0';
    let body = [];
    if (run.status === 'checking') body = [el('p', { className: 'checking' }, run.id ? 'Reading the run…' : 'Uploading and reading the ZIP…')];
    else if (run.status === 'error') body = errorBody("This run can't be used", run.error);
    else if (run.status === 'ok') {
        const s = runSummary();
        const episodes = run.details.episodes;
        if (episodes.length > 1) {
            const select = el('select', { id: 'run-episode' }, ...episodes.map(e =>
                el('option', { value: e.episode, selected: e.episode === run.episode }, `Episode ${e.episode} (${plural(e.lines, 'line')})`)));
            select.addEventListener('change', () => { run.episode = select.value; render(); });
            body.push(el('label', { className: 'field' }, el('span', {}, `Episode (${episodes.length} in this run)`), select));
        } else {
            body.push(el('p', { className: 'headline' }, `Episode ${s.episode}`));
        }
        body.push(el('p', { className: 'sub' }, `${plural(s.lines, 'line')}, ${plural(s.characters.length, 'character')}, ${run.details.fps} fps`),
                  el('p', { className: 'sub tc' }, `${s.first_start_tcr} to ${s.last_end_tcr}`));
    }
    const slot = $('run-slot');
    slot.dataset.state = run.status;
    slot.classList.toggle('is-busy', run.status === 'checking');
    $('run-name').textContent = run.name;
    $('run-size').textContent = run.meta;
    $('run-body').replaceChildren(...body);
}

/* ================================================================ video */

function selectVideo(file) {
    if (!file) return;
    $('form-error').hidden = true;
    const v = state.video;
    if (v.url) URL.revokeObjectURL(v.url);
    Object.assign(v, { file, status: 'checking', durationMs: null, error: '', url: null });
    if (!VIDEO_EXT.includes(extOf(file.name))) {
        Object.assign(v, { status: 'error', error: 'Use an .mp4, .m4v, .webm or .mov file. MP4 (H.264) works in every browser.' });
    } else if (file.size > LIMITS.video) {
        Object.assign(v, { status: 'error', error: `The video is ${sizeText(file.size)}; the limit is 500 MB.` });
    } else if (file.size === 0) {
        Object.assign(v, { status: 'error', error: 'This file is empty.' });
    } else {
        // Read the length locally, before any upload, so the fit check is instant.
        const url = URL.createObjectURL(file);
        v.url = url;
        const probe = document.createElement('video');
        probe.preload = 'metadata';
        probe.muted = true;
        probe.addEventListener('loadedmetadata', () => {
            if (v.url !== url) return;
            Object.assign(v, { status: 'ok', durationMs: probe.duration * 1000 });
            render();
        }, { once: true });
        probe.addEventListener('error', () => {
            if (v.url !== url) return;
            Object.assign(v, { status: 'error',
                error: "This browser can't play this video, so it can't be reviewed. Convert it to MP4 (H.264 video, AAC audio) and add it again." });
            render();
        }, { once: true });
        probe.src = url;
    }
    render();
}

/* ================================================================ slots: replace, clear, drop */

function clearSlot(kind) {
    if (kind === 'run') {
        if (state.run.xhr) state.run.xhr.abort();
        Object.assign(state.run, { status: 'empty', id: null, name: '', meta: '', details: null, episode: null,
                                   error: '', check: state.run.check + 1, xhr: null });
        if (state.source === 'v2') loadRuns();  // runs finished since the list was read
    } else if (kind === 'script') {
        state.script.check++;
        Object.assign(state.script, { file: null, status: 'empty', summary: null, error: '' });
    } else {
        if (state.video.url) URL.revokeObjectURL(state.video.url);
        Object.assign(state.video, { file: null, status: 'empty', durationMs: null, error: '', url: null });
    }
    render();
}

const select = { script: selectScript, video: selectVideo, run: uploadRun };
['script', 'video', 'run'].forEach(kind => {
    const input = $(`${kind}-input`);
    const slot = $(`${kind}-slot`);
    input.addEventListener('change', () => { select[kind](input.files[0]); input.value = ''; });
    ['dragenter', 'dragover'].forEach(type => slot.addEventListener(type, event => {
        event.preventDefault();
        slot.classList.add('is-dragover');
    }));
    slot.addEventListener('dragleave', event => {
        if (!slot.contains(event.relatedTarget)) slot.classList.remove('is-dragover');
    });
    slot.addEventListener('drop', event => {
        event.preventDefault();
        slot.classList.remove('is-dragover');
        if (event.dataTransfer.files.length) select[kind](event.dataTransfer.files[0]);
    });
});
document.querySelectorAll('[data-replace]').forEach(btn => btn.addEventListener('click', () => {
    if (btn.dataset.replace === 'run') clearSlot('run');   // back to the list of runs
    else $(`${btn.dataset.replace}-input`).click();
}));
document.querySelectorAll('[data-clear]').forEach(btn =>
    btn.addEventListener('click', () => clearSlot(btn.dataset.clear)));
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());

/* ================================================================ timeline strip */

function drawStrip(strip, summary, videoMs) {
    const scriptEnd = summary.last_end_ms;
    const hasVideo = Number.isFinite(videoMs) && videoMs > 0;
    const total = Math.max(scriptEnd, hasVideo ? videoMs : 0) * 1.02;
    const pct = ms => `${(ms / total * 100).toFixed(3)}%`;
    const parts = [];
    if (hasVideo && scriptEnd > videoMs) parts.push(el('div', { className: 'overflow', style: `left:${pct(videoMs)}` }));
    parts.push(el('div', { className: 'span', style: `left:${pct(summary.first_start_ms)};width:${pct(scriptEnd - summary.first_start_ms)}` }));
    for (const [start, end, who] of summary.cues) {
        parts.push(el('div', { className: 'cue', style: `left:${pct(start)};width:${pct(end - start)};background:${colourFor(who)}` }));
    }
    if (hasVideo) parts.push(el('div', { className: 'end', style: `left:${pct(videoMs)}`, title: `Video ends at ${clockText(videoMs)}` }));
    strip.replaceChildren(...parts);
    strip.setAttribute('role', 'img');
    strip.setAttribute('aria-label', `Dialogue from ${summary.first_start_tcr} to ${summary.last_end_tcr}`
        + (hasVideo ? `; video length ${clockText(videoMs)}` : ''));
}

function writeFitNote(note, summary, videoMs) {
    note.replaceChildren();
    if (!Number.isFinite(videoMs)) {
        note.className = 'fit-note';
        note.append('Dialogue runs from ', el('b', {}, summary.first_start_tcr), ' to ', el('b', {}, summary.last_end_tcr), '. Add the video to check it fits.');
        return 'none';
    }
    if (summary.last_end_ms > videoMs + ALIGN_TOLERANCE_MS) {
        note.className = 'fit-note warn';
        note.append('The last line ends at ', el('b', {}, summary.last_end_tcr), ', after the video ends at ',
            el('b', {}, clockText(videoMs)), `. Check this is the video for episode ${summary.episode}.`);
        return 'warn';
    }
    note.className = 'fit-note ok';
    note.append('Script fits the video. The last line ends at ', el('b', {}, summary.last_end_tcr),
        ' and the video runs ', el('b', {}, clockText(videoMs)), '.');
    return 'ok';
}

/* ================================================================ render the upload form */

function errorBody(title, message) {
    const items = (message || '').split('\n').filter(Boolean);
    if (items.length <= 1) return [el('p', { className: 'error-title' }, title), el('p', { className: 'sub' }, items[0] || '')];
    return [el('p', { className: 'error-title' }, `${plural(items.length, 'problem')} to fix`),
            el('ul', { className: 'error-list' }, ...items.map(item => el('li', {}, item)))];
}

function renderSlot(kind, slotState, body) {
    const slot = $(`${kind}-slot`);
    const s = state[kind];
    slot.dataset.state = slotState;
    if (s.file) {
        $(`${kind}-name`).textContent = s.file.name;
        $(`${kind}-size`).textContent = sizeText(s.file.size);
    }
    $(`${kind}-body`).replaceChildren(...body);
}

function render() {
    const { script, video } = state;
    const fromRun = state.source === 'v2';

    let body = [];
    if (script.status === 'checking') body = [el('p', { className: 'checking' }, 'Checking the script…')];
    else if (script.status === 'ok') {
        const s = script.summary;
        body = [el('p', { className: 'headline' }, `Episode ${s.episode}`),
                el('p', { className: 'sub' }, `${plural(s.lines, 'line')}, ${plural(s.characters.length, 'character')}`),
                el('p', { className: 'sub tc' }, `${s.first_start_tcr} to ${s.last_end_tcr}`)];
    } else if (script.status === 'error') body = errorBody("This script can't be used", script.error);
    renderSlot('script', script.status, body);
    renderRunSlot();
    // What the episode's dub comes from: the script (generate) or the chosen run's episode.
    const dub = fromRun ? { status: state.run.status, summary: runSummary() } : script;
    const noun = fromRun ? 'run' : 'script';

    body = [];
    if (video.status === 'checking') body = [el('p', { className: 'checking' }, 'Reading the video…')];
    else if (video.status === 'ok') body = [el('p', { className: 'headline tc' }, clockText(video.durationMs)), el('p', { className: 'sub' }, 'Video length')];
    else if (video.status === 'error') body = errorBody("This video can't be used", video.error);
    let videoState = video.status;

    $('upload-fit').hidden = dub.status !== 'ok';
    if (dub.status === 'ok') {
        const videoMs = video.status === 'ok' ? video.durationMs : NaN;
        drawStrip($('upload-strip'), dub.summary, videoMs);
        if (writeFitNote($('upload-fit-note'), dub.summary, videoMs) === 'warn') videoState = 'warn';
    }
    renderSlot('video', videoState, body);

    const ready = dub.status === 'ok' && video.status === 'ok';
    $('load-btn').disabled = !ready;
    let label;
    if (dub.status === 'checking') label = fromRun ? 'Reading the run…' : 'Checking the script…';
    else if (dub.status === 'error') label = fromRun ? 'Choose a run that can be used' : 'Fix the script to continue';
    else if (video.status === 'error') label = 'Add a playable video to continue';
    else if (dub.status === 'empty' && video.status === 'empty') label = fromRun ? 'Choose a run and add the video to continue' : 'Add the script and video to continue';
    else if (dub.status === 'empty') label = fromRun ? 'Choose a run to continue' : `Add the ${noun} to continue`;
    else if (video.status === 'empty') label = 'Add the video to continue';
    else if (video.status === 'checking') label = 'Reading the video…';
    else label = `${fromRun ? 'Review' : 'Dub'} episode ${dub.summary.episode} (${plural(dub.summary.lines, 'line')})`;
    $('load-label').textContent = label;
}

/* ================================================================ dub: generate + upload in parallel */

function showView(name) {
    $('upload-view').hidden = name !== 'upload';
    $('dub-view').hidden = name !== 'dub';
    $('review-view').hidden = name !== 'review';
    setStep(name);
    window.scrollTo({ top: 0 });
}

$('upload-form').addEventListener('submit', async event => {
    event.preventDefault();
    if ($('load-btn').disabled) return;
    $('form-error').hidden = true;
    $('load-btn').disabled = true;
    $('load-label').textContent = 'Starting…';
    const form = settingsForm(new FormData());
    if (state.source === 'v2') {
        form.append('source', 'v2');
        form.append('run', state.run.id);
        form.append('episode', state.run.episode);
    } else {
        form.append('script', state.script.file);
    }
    let record;
    try {
        const response = await fetch('/sessions', { method: 'POST', body: form });
        record = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(record.error || `The server answered HTTP ${response.status}.`);
    } catch (error) {
        $('form-error').textContent = error.message.startsWith('Failed to fetch')
            ? 'The server is not responding. Check the app is running, then try again.' : error.message;
        $('form-error').hidden = false;
        render();
        return;
    }
    startDubView(record, state.video.file);
});

function startDubView(record, videoFile) {
    stopPolling();
    state.dub = { id: record.session_id, record, upload: { status: 'idle', loaded: 0, total: 0, error: '' }, poll: 0, xhr: null };
    try { sessionStorage.setItem('qc-session', record.session_id); } catch (_) { /* optional */ }
    const fromRun = record.source === 'v2';
    $('dub-title').textContent = `${fromRun ? 'Preparing' : 'Dubbing'} episode ${record.summary.episode}`;
    $('dub-heading').textContent = fromRun ? 'Building tracks from AD Tool V2 audio' : 'Dubbing with ElevenLabs';
    $('dub-retry').textContent = fromRun ? 'Try again' : 'Retry from this line';
    showView('dub');
    if (record.video) state.dub.upload.status = 'done';
    else if (videoFile) uploadVideo(videoFile);
    else state.dub.upload = { status: 'error', loaded: 0, total: 0,
        error: 'The page was reloaded before the video finished uploading. Choose the video again.' };
    renderDub();
    poll();
}

function uploadVideo(file) {
    const dub = state.dub;
    dub.upload = { status: 'running', loaded: 0, total: file.size, error: '' };
    const form = new FormData();
    form.append('video', file);
    const job = send(`/session/${dub.id}/video`, form, (loaded, total) => {
        Object.assign(dub.upload, { loaded, total });
        renderDub();
    });
    dub.xhr = job.xhr;
    job.promise.then(record => {
        if (state.dub !== dub) return;
        dub.record = record;
        dub.upload.status = 'done';
        renderDub();
        maybeOpenReview();
    }, error => {
        if (state.dub !== dub || error.aborted) return;
        Object.assign(dub.upload, { status: 'error', error: error.message });
        renderDub();
    });
}

function stopPolling() {
    if (state.dub) clearTimeout(state.dub.poll);
}

async function poll() {
    const dub = state.dub;
    if (!dub) return;
    try {
        const response = await fetch(`/session/${dub.id}`, { cache: 'no-store' });
        if (response.status === 404) { backToUpload('This session no longer exists on the server. Start again.'); return; }
        if (response.ok && state.dub === dub) dub.record = await response.json();
    } catch (_) { /* transient: keep polling */ }
    if (state.dub !== dub) return;
    renderDub();
    if (maybeOpenReview()) return;
    if (dub.record.status === 'dubbing') dub.poll = setTimeout(poll, POLL_MS);
}

function maybeOpenReview() {
    const dub = state.dub;
    if (!dub || dub.record.status !== 'dubbed' || dub.upload.status !== 'done') return false;
    stopPolling();
    state.dub = null;
    // Re-read so the record includes both the audio and the video.
    fetch(`/session/${dub.id}`, { cache: 'no-store' }).then(r => r.json()).then(openReview);
    return true;
}

function setJob(id, jobState, fraction, count, note) {
    $(`job-${id}`).dataset.state = jobState;
    $(`${id}-bar`).style.width = `${Math.round(Math.min(1, fraction) * 100)}%`;
    $(`${id}-count`).textContent = count;
    $(`${id}-note`).textContent = note;
}

function renderDub() {
    const dub = state.dub;
    if (!dub) return;
    const r = dub.record;
    const p = r.progress || { phase: 'dubbing', done: 0, total: r.summary.lines, current: '' };
    if (r.status === 'error') {
        setJob('dub', 'error', p.done / p.total, `${p.done} of ${p.total}`, r.error || 'Dubbing stopped.');
    } else if (r.status === 'dubbed') {
        setJob('dub', 'ok', 1, `${p.total} of ${p.total}`, `Done. ${plural(r.audio.tracks.length, 'character track')} built.`);
    } else if (p.phase === 'building') {
        setJob('dub', 'running', 1, `${p.total} of ${p.total}`, 'Placing lines and building one track per character…');
    } else {
        setJob('dub', 'running', p.done / p.total, `${p.done} of ${p.total}`,
            p.current ? `Line ${Math.min(p.done + 1, p.total)}: ${p.current}` : 'Starting…');
    }
    $('dub-actions').hidden = r.status !== 'error';

    const u = dub.upload;
    if (u.status === 'done') setJob('video', 'ok', 1, '', 'Uploaded.');
    else if (u.status === 'error') setJob('video', 'error', 0, '', u.error);
    else {
        const pct = u.total ? u.loaded / u.total : 0;
        setJob('video', 'running', pct, u.total ? `${Math.round(pct * 100)}%` : '',
            u.total ? `${sizeText(u.loaded)} of ${sizeText(u.total)}` : 'Starting…');
    }
    $('video-actions').hidden = u.status !== 'error';
}

$('dub-retry').addEventListener('click', async () => {
    const dub = state.dub;
    if (!dub) return;
    $('dub-retry').disabled = true;
    try {
        const response = await fetch(`/session/${dub.id}/retry`, { method: 'POST' });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        dub.record = body;
        renderDub();
        stopPolling();
        poll();
    } catch (error) {
        toast(`Retry failed: ${error.message}`);
    } finally {
        $('dub-retry').disabled = false;
    }
});
$('video-retry').addEventListener('click', () => {
    // Re-use the file chosen on the upload screen if we still have it; otherwise ask again.
    if (state.video.file && state.dub && state.dub.upload.error.indexOf('reloaded') < 0) uploadVideo(state.video.file);
    else $('video-again').click();
    renderDub();
});
$('video-again').addEventListener('change', () => {
    const file = $('video-again').files[0];
    $('video-again').value = '';
    if (file && state.dub) { uploadVideo(file); renderDub(); }
});

function backToUpload(message) {
    stopPolling();
    if (state.dub && state.dub.xhr) state.dub.xhr.abort();
    state.dub = null;
    try { sessionStorage.removeItem('qc-session'); } catch (_) { /* optional */ }
    showView('upload');
    render();
    if (message) { $('form-error').textContent = message; $('form-error').hidden = false; }
}

$('dub-cancel').addEventListener('click', () => {
    const id = state.dub && state.dub.id;
    discardSession(id); // stops the dub on the server before its next ElevenLabs request
    backToUpload();
});

/* ================================================================ review */

function setStep(active) {
    const order = ['upload', 'dub', 'review', 'export'];
    const index = order.indexOf(active);
    order.forEach((name, i) => {
        const step = $(`step-${name}`);
        step.classList.toggle('is-active', i === index);
        step.classList.toggle('is-done', i < index);
        if (i === index) step.setAttribute('aria-current', 'step'); else step.removeAttribute('aria-current');
    });
}

function renderNotes(session) {
    const audio = session.audio;
    const from = session.source === 'v2' ? ` from AD Tool V2 (${session.run.name})` : ' with ElevenLabs';
    const notes = [el('li', { className: 'note-ok' }, `${plural(audio.lines.length, 'line')} dubbed${from}, on ${plural(audio.tracks.length, 'character track')}.`)];
    const fitted = audio.lines.filter(l => l.fit_speed !== 1).length;
    if (fitted) notes.push(el('li', { className: 'note-ok' }, `${plural(fitted, 'line')} fitted to ${fitted === 1 ? 'its slot' : 'their slots'} by FFmpeg (${SPEED_MIN.toFixed(2)}x to ${SPEED_MAX.toFixed(2)}x).`));
    for (const warning of audio.warnings) notes.push(el('li', { className: 'note-warn' }, warning));
    $('import-notes').replaceChildren(...notes);
}

function renderFacts(session) {
    const s = session.summary;
    const audio = session.audio;
    const edited = audio.lines.filter(l => l.edited).length;
    const fact = (value, label, cls = '') => el('li', { className: cls }, el('b', {}, String(value)), ` ${label}`);
    $('episode-facts').replaceChildren(
        fact(s.lines, s.lines === 1 ? 'line' : 'lines'),
        fact(s.characters.length, s.characters.length === 1 ? 'character' : 'characters'),
        fact(s.fps, 'fps'),
        ...(audio.overrun_lines ? [fact(audio.overrun_lines, 'past TCR OUT', 'fact-warn')] : []),
        ...(edited ? [fact(edited, edited === 1 ? 'line edited' : 'lines edited', 'fact-edit')] : []));
}

async function openReview(session) {
    state.session = session;
    const s = session.summary;
    const audio = session.audio;
    try { sessionStorage.setItem('qc-session', session.session_id); } catch (_) { /* optional */ }

    $('episode-title').textContent = `Episode ${s.episode}`;
    renderFacts(session);
    renderNotes(session);
    $('file-meta').replaceChildren(
        `${session.script.name} (${session.script.size})`, el('br'),
        `${session.video.name} (${session.video.size})`);

    showView('review');

    // Player and lanes
    const video = $('player');
    video.src = session.video_url;
    video.load();
    const player = new MultitrackPlayer(video, { sampleRate: audio.sample_rate, fps: s.fps });
    state.player = player;
    player.setVideoSound(false, Number($('video-volume').value));
    $('video-sound').setAttribute('aria-pressed', 'false');
    player.setMasterVolume(Number($('master-volume').value));
    setTransportEnabled(false);
    $('loading-overlay').hidden = false;
    $('loading-text').textContent = 'Loading audio…';
    $('loading-bar').style.width = '0';

    try {
        const peaks = await (await fetch(session.peaks_url, { cache: 'no-store' })).json();
        if (state.player !== player) return;
        state.lanes = new LaneView($('lanes'), {
            tracks: audio.tracks, lines: audio.lines, peaks, colours: colourFor,
            duration: audio.duration_ms / 1000,
            handlers: {
                seek: t => player.seek(t),
                mute: i => player.setMuted(i, !player.tracks[i].muted),
                solo: i => player.setSolo(i, !player.tracks[i].solo),
                volume: (i, v) => player.setVolume(i, v),
            },
        });
        state.panel = new ScriptPanel($('panel-script'), {
            lines: audio.lines, colours: colourFor,
            handlers: {
                seek: line => player.seek(Math.max(0, line.audio_start_ms / 1000 - 0.2)),
                playLine: line => player.playRange(Math.max(0, line.audio_start_ms / 1000 - PREROLL), line.audio_end_ms / 1000 + POSTROLL),
                select: row => { if (state.lanes) state.lanes.setSelected(row); updateSpeedControl(); },
                regenerate: (row, text) => regenerateLine(row, text),
            },
        });
        await player.load(audio.tracks, fraction => {
            $('loading-bar').style.width = `${Math.round(fraction * 100)}%`;
            $('loading-text').textContent = fraction < 1 ? `Loading audio ${Math.round(fraction * 100)}%` : 'Decoding audio…';
        });
        if (state.player !== player) return;
        $('loading-overlay').hidden = true;
        setTransportEnabled(true);
        bindPlayer(player);
        updateMix();
    } catch (error) {
        $('loading-text').textContent = `${error.message} Reload the page to try again.`;
    }
}

function setTransportEnabled(on) {
    ['frame-back', 'play-btn', 'frame-fwd', 'video-sound', 'video-volume', 'master-volume'].forEach(id => { $(id).disabled = !on; });
}

function updateMix() {
    const player = state.player;
    if (!player || !state.lanes) return;
    state.lanes.setMix(player.tracks.map((t, i) => ({ muted: t.muted, solo: t.solo, audible: player.audible(i) })));
}

function bindPlayer(player) {
    const fps = state.session.summary.fps;
    const video = player.video;
    const total = () => player.duration;
    const showTotal = () => {
        $('tc-total').textContent = `/ ${timecode(total(), fps)}`;
        state.lanes.setDuration(total());
    };
    // Listeners on the shared <video> die with this player (signal), so sessions don't stack them.
    video.addEventListener('loadedmetadata', showTotal, { signal: player.signal });
    if (video.readyState >= 1) showTotal();
    player.addEventListener('mix', updateMix);
    player.addEventListener('state', () => {
        const playing = player.playing;
        $('play-btn').classList.toggle('is-playing', playing);
        $('play-btn').setAttribute('aria-label', playing ? 'Pause' : 'Play');
        $('play-btn').title = playing ? 'Pause (Space)' : 'Play (Space)';
    });
    player.addEventListener('error', event => toast(event.detail.message));
    player.dispatchEvent(new CustomEvent('state'));
    let lastTc = '';
    player.addEventListener('tick', () => {
        const t = player.currentTime;
        const tc = timecode(t, fps);
        if (tc !== lastTc) { $('tc-now').textContent = tc; lastTc = tc; }
        state.lanes.setPlayhead(t);
        renderSubs(state.panel.setTime(t * 1000, player.playing));
        const sync = $('sync-state');
        const off = Math.round(player.drift * 1000);
        sync.textContent = player.playing ? `Sync ${off >= 0 ? '+' : '−'}${Math.abs(off)} ms` : 'Sync';
        sync.classList.toggle('is-live', player.playing);
    });
}

/* ================================================================ Block 4: subtitles and tabs */

let subsKey = '';
function renderSubs(lines) {
    const on = $('subs-toggle').getAttribute('aria-pressed') === 'true';
    const key = on ? lines.map(l => `${l.row}:${l.text}`).join('|') : '';
    if (key === subsKey) return;
    subsKey = key;
    $('subs').replaceChildren(...(on ? lines : []).map(line =>
        el('p', { className: 'sub-line' },
            el('b', { style: `color:${colourFor(line.char_index)}` }, line.character), ' ', renderDialogue(line.text))));
}
function toggleSubs() {
    const on = $('subs-toggle').getAttribute('aria-pressed') !== 'true';
    $('subs-toggle').setAttribute('aria-pressed', String(on));
    subsKey = null;
    if (state.player && state.panel) renderSubs(state.panel.setTime(state.player.currentTime * 1000, false));
}
$('subs-toggle').addEventListener('click', toggleSubs);

function showTab(name) {
    ['script', 'info'].forEach(tab => {
        $(`tab-${tab}`).setAttribute('aria-selected', String(tab === name));
        $(`panel-${tab}`).hidden = tab !== name;
    });
}
$('tab-script').addEventListener('click', () => showTab('script'));
$('tab-info').addEventListener('click', () => showTab('info'));

/* ================================================================ Block 5: regenerate one line */

/** POST JSON to a line endpoint; returns the updated session or throws a readable error. */
async function postLine(url, payload) {
    try {
        const response = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `The server answered HTTP ${response.status}.`);
        return body;
    } catch (error) {
        throw new Error(error.message === 'Failed to fetch'
            ? 'The server is not responding. Check the app is running, then try again.' : error.message);
    }
}

async function regenerateLine(row, text) {
    const session = state.session;
    const body = await postLine(`/session/${session.session_id}/lines/${row}`, { text });
    if (state.session === session) await applyLineChange(body, row, 'regenerated');
}

/**
 * After a line changed on the server (new take or new speed): swap only the rebuilt tracks into
 * the running player, redraw lanes and script, say what else moved, and play the line.
 */
async function applyLineChange(body, row, verb) {
    const player = state.player;
    const before = state.session.audio.lines.find(l => l.row === row);
    const stamp = Date.now(); // cache-buster: the track URLs do not change, their content does
    await player.replaceTracks(body.changed_tracks.map(index => ({
        index, url: `${body.audio.tracks.find(t => t.index === index).url}?v=${stamp}` })));
    const peaks = await (await fetch(`${body.peaks_url}?v=${stamp}`, { cache: 'no-store' })).json();
    state.session = body;
    state.lanes.update({ lines: body.audio.lines, peaks, duration: player.duration });
    state.panel.setLines(body.audio.lines);
    renderFacts(body);
    renderNotes(body);
    updateSpeedControl();
    $('tc-total').textContent = `/ ${timecode(player.duration, body.summary.fps)}`;

    const line = body.audio.lines.find(l => l.row === row);
    let message = `Row ${row} ${verb}.`;
    if (body.moved.length) {
        const who = [...new Set(body.moved.map(m => m.character))].join(', ');
        const shift = Math.max(...body.moved.map(m => Math.abs(m.shift_ms))) / 1000;
        const dir = body.moved.every(m => m.shift_ms < 0) ? 'earlier' : body.moved.every(m => m.shift_ms > 0) ? 'later' : 'around';
        message += ` ${body.moved.length} later ${who} line${body.moved.length === 1 ? '' : 's'} moved ${dir} (up to ${shift.toFixed(1)} s).`;
    }
    if (line.overrun_ms) message += ` Still ${(line.overrun_ms / 1000).toFixed(1)} s past TCR OUT.`;
    else if (before && before.overrun_ms) message += ' It now fits before TCR OUT.';
    toast(message);
    player.playRange(Math.max(0, line.audio_start_ms / 1000 - PREROLL), line.audio_end_ms / 1000 + POSTROLL);
}

/* ================================================================ line speed */

const HAS_FFMPEG = document.body.dataset.ffmpeg === 'yes';
const SPEED_MIN = Number($('line-speed').min);
const SPEED_MAX = Number($('line-speed').max);
let speedTimer = 0;
let speedBusy = false;

function selectedLine() {
    if (!state.panel || state.panel.selected == null || !state.session) return null;
    return state.session.audio.lines.find(l => l.row === state.panel.selected) || null;
}

/**
 * Suggested speed for a line: pass 2's fit, computed on the server from the take's spoken length
 * and the slot (TCR OUT - TCR IN), held between the limits; 1.00x when the line already fits. A
 * suggestion only: the reviewer decides with the slider. Shown in amber when even the limit does
 * not make the line fit.
 */
function showSuggestion(line) {
    const box = $('line-speed-suggested');
    if (!line) { box.hidden = true; return; }
    const speed = line.fit_speed;
    const held = line.needed_speed > SPEED_MAX || line.needed_speed < SPEED_MIN;
    box.hidden = false;
    box.classList.toggle('is-over', held);
    box.replaceChildren('Suggested speed: ', el('b', {}, `${speed.toFixed(2)}x`));
    box.title = held
        ? `Row ${line.row} would need ${line.needed_speed.toFixed(2)}x to fit between TCR IN and TCR OUT; ${speed.toFixed(2)}x is the limit.`
        : `Row ${line.row} fits between TCR IN and TCR OUT at ${speed.toFixed(2)}x. This is a suggestion only.`;
}

/** Point the slider at the selected line (or disable it when none is selected). */
function updateSpeedControl() {
    const line = selectedLine();
    showSuggestion(line);
    const box = $('line-speed-box');
    const slider = $('line-speed');
    box.classList.toggle('is-active', Boolean(line));
    slider.disabled = !line || !HAS_FFMPEG || speedBusy;
    if (!line) {
        slider.value = '1.00';
        $('line-speed-label').textContent = 'Line speed';
        $('line-speed-value').textContent = 'Select a line';
        box.title = 'Select a line in the script to change its speed';
        return;
    }
    slider.value = String(line.line_speed);
    $('line-speed-label').textContent = `Row ${line.row} speed`;
    $('line-speed-value').textContent = `${Number(slider.value).toFixed(2)}x`;
    box.title = HAS_FFMPEG ? `Change the speed of row ${line.row} (${line.character}); its start stays where it is`
        : 'Line speed needs FFmpeg installed on the server';
    if (!HAS_FFMPEG) $('line-speed-value').textContent = 'Needs FFmpeg';
}

$('line-speed').addEventListener('input', () => {
    $('line-speed-value').textContent = `${Number($('line-speed').value).toFixed(2)}x`;
});
// Apply when the slider is released (or after arrow keys settle), not on every step.
$('line-speed').addEventListener('change', () => {
    clearTimeout(speedTimer);
    speedTimer = setTimeout(applyLineSpeed, 350);
});

async function applyLineSpeed() {
    const line = selectedLine();
    const value = Number(Number($('line-speed').value).toFixed(2));
    if (!line || speedBusy || value === line.line_speed) return;
    const session = state.session;
    speedBusy = true;
    $('line-speed-box').classList.add('is-busy');
    $('line-speed').disabled = true;
    $('line-speed-value').textContent = 'Applying…';
    try {
        const body = await postLine(`/session/${session.session_id}/lines/${line.row}/speed`, { speed: value });
        if (state.session !== session) return;
        speedBusy = false;
        await applyLineChange(body, line.row, value === line.fit_speed
            ? `is back to its fitted speed (${value.toFixed(2)}x)` : `set to ${value.toFixed(2)}x`);
    } catch (error) {
        toast(`Line speed not changed: ${error.message}`);
    } finally {
        speedBusy = false;
        $('line-speed-box').classList.remove('is-busy');
        updateSpeedControl();
    }
}

/* ================================================================ layout: a 16:9 picture frame */

// The video column is as wide as a 16:9 frame of the available height, so the picture is never
// stretched wider than widescreen; the script panel takes the remaining width. Phone (9:16) and
// other shapes play inside the frame, letterboxed or pillarboxed, never cropped.
const workspace = document.querySelector('.workspace');
const MIN_SCRIPT_WIDTH = 400;
function fitStage() {
    const height = workspace.clientHeight;
    const width = workspace.clientWidth;
    if (!height || !width) return;
    const stage = Math.round(Math.max(320, Math.min(height * 16 / 9, width - 14 - MIN_SCRIPT_WIDTH)));
    workspace.style.setProperty('--stage-cols', `${stage}px minmax(${MIN_SCRIPT_WIDTH}px, 1fr)`);
}
new ResizeObserver(fitStage).observe(workspace);

/* ================================================================ Block 6: export */

function openExport() {
    const session = state.session;
    if (!session || !session.audio) return;
    const audio = session.audio;
    const edited = audio.lines.filter(l => l.edited).length;
    const sped = audio.lines.filter(l => l.speed_set).length;
    const changed = audio.lines.filter(l => l.edited || l.speed_set).length;
    const stem = session.script.name.replace(/\.xlsx$/i, '');
    const item = (title, detail) => el('li', {}, el('b', {}, title), el('span', {}, detail));
    $('export-title').textContent = `Export episode ${session.summary.episode}`;
    $('export-list').replaceChildren(
        item(`${plural(audio.tracks.length, 'character track')} (.wav)`,
             `${audio.sample_rate / 1000} kHz mono, as you heard them in the review`),
        item('Timeline and processing report (.csv)', 'Same columns as AD version 2, with the reviewed timings'),
        item(`${stem}_QC.xlsx`, edited
            ? `${plural(edited, 'edited line')} written into TARGET DIALOGUE, highlighted, with the script wording in a note`
            : 'No lines edited: the script as uploaded'),
        item('qc_changes.csv', changed
            ? `${plural(changed, 'line')} changed in review${sped ? `, ${sped} with a new line speed` : ''}`
            : 'No changes made in review'));
    $('export-download').href = `/session/${session.session_id}/export`;
    if (state.player) state.player.pause();
    $('export-dialog').showModal();
}
$('export-btn').addEventListener('click', openExport);
$('export-close').addEventListener('click', () => $('export-dialog').close());
$('export-download').addEventListener('click', () => {
    setStep('export');
    toast('Preparing the ZIP. Your browser will save it in a moment.');
    setTimeout(() => $('export-dialog').close(), 300);
});

/* transport controls */
$('play-btn').addEventListener('click', () => state.player && state.player.toggle());
$('frame-back').addEventListener('click', () => state.player && state.player.step(-1));
$('frame-fwd').addEventListener('click', () => state.player && state.player.step(1));
$('video-sound').addEventListener('click', () => toggleVideoSound());
$('video-volume').addEventListener('input', () => {
    if (!state.player) return;
    state.player.setVideoSound(true, Number($('video-volume').value));
    $('video-sound').setAttribute('aria-pressed', 'true');
});
$('master-volume').addEventListener('input', () => state.player && state.player.setMasterVolume(Number($('master-volume').value)));
$('master-volume').addEventListener('dblclick', () => { $('master-volume').value = '1'; state.player && state.player.setMasterVolume(1); });
function toggleVideoSound() {
    if (!state.player) return;
    const on = $('video-sound').getAttribute('aria-pressed') !== 'true';
    state.player.setVideoSound(on, Number($('video-volume').value));
    $('video-sound').setAttribute('aria-pressed', String(on));
    toast(on ? 'Video sound on' : 'Video sound off');
}
$('player').addEventListener('click', () => state.player && state.player.ready && state.player.toggle());

/* keyboard: the review is driven from the keyboard as much as the mouse */
document.addEventListener('keydown', event => {
    const player = state.player;
    if (!player || !player.ready || $('review-view').hidden || $('export-dialog').open) return;
    const target = event.target;
    if (target.matches('input[type="text"], textarea, select, [contenteditable="true"]')) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const onSlider = target.matches('input[type="range"]');

    if (event.code === 'Space') {
        event.preventDefault(); // also stops Space from re-clicking the last focused button
        player.toggle();
    } else if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !onSlider) {
        event.preventDefault();
        const dir = event.key === 'ArrowLeft' ? -1 : 1;
        if (event.shiftKey) player.seek(player.currentTime + dir);
        else player.step(dir);
    } else if (event.key === 'Home' && !onSlider) {
        event.preventDefault();
        player.seek(0);
    } else if (/^Digit[1-9]$/.test(event.code)) {
        const i = Number(event.code.slice(5)) - 1;
        if (i >= player.tracks.length) return;
        event.preventDefault();
        if (event.shiftKey) player.setSolo(i, !player.tracks[i].solo);
        else player.setMuted(i, !player.tracks[i].muted);
    } else if ((event.key === 'ArrowUp' || event.key === 'ArrowDown') && !onSlider && state.panel) {
        event.preventDefault();
        showTab('script');
        const line = state.panel.step(event.key === 'ArrowUp' ? -1 : 1);
        player.seek(Math.max(0, line.audio_start_ms / 1000 - 0.2));
    } else if ((event.key === 'p' || event.key === 'P') && state.panel && state.panel.selected != null) {
        const line = state.panel.lines.find(l => l.row === state.panel.selected);
        player.playRange(Math.max(0, line.audio_start_ms / 1000 - PREROLL), line.audio_end_ms / 1000 + POSTROLL);
    } else if (event.key === 'Enter' && state.panel && (target === document.body || target.closest('.line'))) {
        event.preventDefault();
        showTab('script');
        state.panel.edit();
    } else if (event.key === 'v' || event.key === 'V') {
        toggleVideoSound();
    } else if (event.key === 's' || event.key === 'S') {
        toggleSubs();
    }
});


function closeReview() {
    if (state.player) state.player.destroy();
    if (state.lanes) state.lanes.destroy();
    state.player = null;
    state.lanes = null;
    state.panel = null;
    $('panel-script').replaceChildren();
    updateSpeedControl();
    subsKey = null;
    $('subs').replaceChildren();
    showTab('script');
    const video = $('player');
    video.pause();
    video.removeAttribute('src');
    video.load();
}

$('reset-btn').addEventListener('click', () => {
    const id = state.session && state.session.session_id;
    closeReview();
    discardSession(id);
    state.session = null;
    clearSlot('script');
    clearSlot('run');
    clearSlot('video');
    backToUpload();
});

// Reloading the page reopens the current episode (review or dubbing) instead of starting over.
(async () => {
    render();
    let id = null;
    try { id = sessionStorage.getItem('qc-session'); } catch (_) { /* optional */ }
    if (!id) return;
    try {
        const response = await fetch(`/session/${id}`, { cache: 'no-store' });
        const record = response.ok ? await response.json() : null;
        if (!record) sessionStorage.removeItem('qc-session');
        else if (record.ready) openReview(record);
        else startDubView(record, null);
    } catch (_) { /* server unavailable: stay on the upload screen */ }
})();
