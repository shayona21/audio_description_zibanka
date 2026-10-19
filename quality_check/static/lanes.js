/*
 * LaneView: one lane per character on a shared time axis, DAW style.
 * Each lane draws the scripted slots (TCR IN to OUT), the dubbed waveform where AD version 2
 * actually placed it, and an underline wherever audio runs past its TCR OUT. Clicking or
 * dragging anywhere on the timeline scrubs the video.
 */

const LANE_HEIGHT = 48;
const RULER_HEIGHT = 28;
const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
const OVERRUN = '#e8590c';

function el(tag, props = {}, ...children) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
}

function clock(seconds) {
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function rgba(hex, alpha) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export class LaneView {
    constructor(root, { tracks, lines, peaks, colours, duration, handlers }) {
        this.root = root;
        this.tracks = tracks;
        this.colours = colours;
        this.peaks = peaks;
        this.duration = Math.max(duration, 1);
        this.handlers = handlers;
        this.linesByTrack = tracks.map(t => lines.filter(l => l.char_index === t.index));
        this._build();
        this._resizeObserver = new ResizeObserver(() => this.draw());
        this._resizeObserver.observe(this.timeline);
        this.draw();
    }

    /* ---------------- DOM ---------------- */

    _build() {
        this.rulerCanvas = el('canvas', { className: 'ruler-canvas' });
        this.playhead = el('div', { className: 'playhead', ariaHidden: 'true' });
        this.canvases = [];
        this.heads = [];

        const heads = el('div', { className: 'lane-heads' }, el('div', { className: 'lane-head ruler-head' }, 'Characters'));
        this.timeline = el('div', { className: 'lane-timeline' },
            el('div', { className: 'lane-body ruler-body' }, this.rulerCanvas), this.playhead);

        this.tracks.forEach((track, i) => {
            const colour = this.colours(track.index);
            const mute = el('button', { type: 'button', className: 'ms-btn', textContent: 'M',
                title: `Mute ${track.character}${i < 9 ? ` (${i + 1})` : ''}` });
            const solo = el('button', { type: 'button', className: 'ms-btn ms-solo', textContent: 'S',
                title: `Solo ${track.character}${i < 9 ? ` (Shift + ${i + 1})` : ''}` });
            mute.setAttribute('aria-pressed', 'false');
            solo.setAttribute('aria-pressed', 'false');
            mute.setAttribute('aria-label', `Mute ${track.character}`);
            solo.setAttribute('aria-label', `Solo ${track.character}`);
            const volume = el('input', { type: 'range', min: '0', max: '1.5', step: '0.01', value: '1', className: 'lane-vol' });
            volume.setAttribute('aria-label', `${track.character} volume`);
            mute.addEventListener('click', () => this.handlers.mute(i));
            solo.addEventListener('click', () => this.handlers.solo(i));
            volume.addEventListener('input', () => this.handlers.volume(i, Number(volume.value)));
            volume.addEventListener('dblclick', () => { volume.value = '1'; this.handlers.volume(i, 1); });

            const head = el('div', { className: 'lane-head' },
                el('span', { className: 'lane-key', textContent: i < 9 ? String(i + 1) : '' }),
                el('span', { className: 'swatch', style: `background:${colour}` }),
                el('div', { className: 'lane-title' },
                    el('strong', { textContent: track.character, title: track.character }),
                    el('span', { textContent: `${track.lines} line${track.lines === 1 ? '' : 's'}` })),
                el('div', { className: 'lane-ms' }, mute, solo),
                volume);
            head.style.setProperty('--lane', colour);
            heads.append(head);
            this.heads.push({ head, mute, solo, volume });

            const canvas = el('canvas', { className: 'lane-canvas' });
            canvas.setAttribute('role', 'img');
            canvas.setAttribute('aria-label', `${track.character} track`);
            this.canvases.push(canvas);
            this.timeline.insertBefore(el('div', { className: 'lane-body' }, canvas), this.playhead);
        });

        this.root.replaceChildren(el('div', { className: 'lanes-grid' }, heads, this.timeline));
        this._bindScrub();
    }

    _bindScrub() {
        let dragging = false;
        let pending = null;
        const seekTo = clientX => {
            const rect = this.timeline.getBoundingClientRect();
            const fraction = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
            pending = fraction * this.duration;
            if (!this._scrubFrame) {
                // Coalesce pointer moves: at most one seek per animation frame.
                this._scrubFrame = requestAnimationFrame(() => {
                    this._scrubFrame = 0;
                    if (pending !== null) this.handlers.seek(pending);
                    pending = null;
                });
            }
        };
        this.timeline.addEventListener('pointerdown', event => {
            if (event.button !== 0) return;
            dragging = true;
            this.timeline.setPointerCapture(event.pointerId);
            this.timeline.classList.add('is-scrubbing');
            seekTo(event.clientX);
        });
        this.timeline.addEventListener('pointermove', event => { if (dragging) seekTo(event.clientX); });
        const end = () => { dragging = false; this.timeline.classList.remove('is-scrubbing'); };
        this.timeline.addEventListener('pointerup', end);
        this.timeline.addEventListener('pointercancel', end);
    }

    /* ---------------- state from the player ---------------- */

    /** New lines, peaks and length after a line is regenerated. */
    update({ lines, peaks, duration }) {
        this.linesByTrack = this.tracks.map(t => lines.filter(l => l.char_index === t.index));
        this.peaks = peaks;
        if (Number.isFinite(duration) && duration > 0) this.duration = Math.max(this.duration, duration);
        this.draw();
    }

    /** Outline one line (Excel row) in its lane: the line selected in the script panel. */
    setSelected(row) {
        if (row === this.selected) return;
        this.selected = row;
        this.draw();
    }

    setDuration(seconds) {
        if (Number.isFinite(seconds) && seconds > 0 && Math.abs(seconds - this.duration) > 0.01) {
            this.duration = seconds;
            this.draw();
        }
    }

    setPlayhead(seconds) {
        const x = Math.min(1, Math.max(0, seconds / this.duration)) * this.timeline.clientWidth;
        this.playhead.style.transform = `translateX(${x}px)`;
    }

    setMix(states) {
        states.forEach((state, i) => {
            const { head, mute, solo } = this.heads[i];
            mute.setAttribute('aria-pressed', String(state.muted));
            solo.setAttribute('aria-pressed', String(state.solo));
            head.classList.toggle('is-silent', !state.audible);
            this.canvases[i].parentElement.classList.toggle('is-silent', !state.audible);
        });
    }

    /* ---------------- drawing ---------------- */

    _prepare(canvas, height) {
        const dpr = window.devicePixelRatio || 1;
        const width = canvas.parentElement.clientWidth;
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        canvas.width = Math.max(1, Math.round(width * dpr));
        canvas.height = Math.round(height * dpr);
        const g = canvas.getContext('2d');
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, width, height);
        return { g, width, dpr };
    }

    draw() {
        this._drawRuler();
        this.canvases.forEach((canvas, i) => this._drawLane(canvas, i));
    }

    _drawRuler() {
        const { g, width } = this._prepare(this.rulerCanvas, RULER_HEIGHT);
        const perSecond = width / this.duration;
        const step = TICK_STEPS.find(s => s * perSecond >= 64) || TICK_STEPS[TICK_STEPS.length - 1];
        const minor = step >= 10 ? step / 5 : step / 2 >= 1 ? step / 2 : step;
        g.fillStyle = '#8a949d';
        g.font = '500 11px "IBM Plex Sans", system-ui, sans-serif';
        g.textBaseline = 'top';
        for (let t = 0; t <= this.duration; t += minor) {
            const x = Math.round(t * perSecond) + 0.5;
            const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
            g.fillRect(x - 0.5, major ? RULER_HEIGHT - 10 : RULER_HEIGHT - 5, 1, major ? 10 : 5);
            if (major && x + 30 < width) g.fillText(clock(t), x + 4, 6);
        }
    }

    _drawLane(canvas, i) {
        const { g, width, dpr } = this._prepare(canvas, LANE_HEIGHT);
        const track = this.tracks[i];
        const colour = this.colours(track.index);
        const msToX = ms => ms / 1000 / this.duration * width;
        const mid = LANE_HEIGHT / 2;

        // Scripted slots
        for (const line of this.linesByTrack[i]) {
            const x0 = msToX(line.start_ms);
            const w = Math.max(1, msToX(line.end_ms) - x0);
            g.fillStyle = rgba(colour, 0.09);
            g.fillRect(x0, 4, w, LANE_HEIGHT - 8);
            const selected = line.row === this.selected;
            g.strokeStyle = selected ? '#14212c' : rgba(colour, 0.45);
            g.lineWidth = selected ? 2 : 1;
            g.strokeRect(Math.round(x0) + 0.5, 4.5, Math.max(1, Math.round(w) - 1), LANE_HEIGHT - 9);
            if (selected) {
                // Bracket the audio too, which may start late or run past the slot.
                const a0 = msToX(line.audio_start_ms), a1 = msToX(line.audio_end_ms);
                g.fillStyle = 'rgba(20, 33, 44, 0.06)';
                g.fillRect(a0, 2, Math.max(1, a1 - a0), LANE_HEIGHT - 4);
            }
        }

        // Waveform: max peak per device pixel column. Square-root scaling keeps quiet speech visible.
        const peaks = this.peaks.tracks[String(track.index)] || [];
        const perSecond = this.peaks.per_second;
        const columns = Math.round(width * dpr);
        const binsPerColumn = this.duration * perSecond / columns;
        g.fillStyle = colour;
        for (let c = 0; c < columns; c++) {
            const b0 = Math.floor(c * binsPerColumn);
            const b1 = Math.min(peaks.length, Math.max(b0 + 1, Math.floor((c + 1) * binsPerColumn)));
            let peak = 0;
            for (let b = b0; b < b1; b++) if (peaks[b] > peak) peak = peaks[b];
            if (peak < 2) continue;
            const h = Math.sqrt(peak / 255) * (mid - 6);
            g.fillRect(c / dpr, mid - h, 1 / dpr, h * 2);
        }

        // Audio running past TCR OUT
        g.fillStyle = OVERRUN;
        for (const line of this.linesByTrack[i]) {
            if (!line.overrun_ms) continue;
            const x0 = msToX(Math.max(line.end_ms, line.audio_start_ms));
            g.fillRect(x0, LANE_HEIGHT - 4, Math.max(2, msToX(line.audio_end_ms) - x0), 3);
        }
    }

    destroy() {
        this._resizeObserver.disconnect();
        cancelAnimationFrame(this._scrubFrame);
        this.root.replaceChildren();
    }
}
