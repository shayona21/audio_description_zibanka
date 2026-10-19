/*
 * MultitrackPlayer: plays the per-character dub tracks locked to a <video> element.
 *
 * The video is the master clock: it owns play, pause, seek and buffering. The dub tracks are
 * decoded once into AudioBuffers and played through Web Audio, one source per track, with a gain
 * per track for mute, solo and volume. Two things keep picture and sound together:
 *
 *  1. Latency compensation. Audio scheduled now is heard `outputLatency` later (more over
 *     Bluetooth), so each start reads ahead by that amount: the sample under the playhead
 *     comes out of the speakers when its frame is on screen.
 *  2. Drift correction. Every animation frame we compare what is being heard (from
 *     AudioContext.getOutputTimestamp) with the video's media clock (from
 *     requestVideoFrameCallback). If the smoothed difference exceeds one frame we restart the
 *     sources at the right offset, behind a 10 ms fade so the correction does not click.
 */

const LEAD = 0.03;                 // seconds of scheduling headroom for each (re)start
const FADE = 0.01;                 // fade time for starts, stops and resyncs
const DRIFT_SMOOTHING = 0.15;      // EMA weight of each new drift sample
const SETTLE = 0.4;                // ignore drift right after a (re)start while clocks settle

export class MultitrackPlayer extends EventTarget {
    constructor(video, { sampleRate, fps }) {
        super();
        this.video = video;
        this.fps = fps;
        try {
            // Decoding at the tracks' own rate (24 kHz) halves memory against a 48 kHz context.
            this.ctx = new AudioContext({ sampleRate, latencyHint: 'interactive' });
        } catch (_) {
            this.ctx = new AudioContext({ latencyHint: 'interactive' });
        }
        this.master = this.ctx.createGain();
        this.bus = this.ctx.createGain();   // transport fades live here, user volume on master
        this.bus.gain.value = 0;
        this.bus.connect(this.master).connect(this.ctx.destination);
        this.tracks = [];
        this.sources = [];
        this.anchor = null;               // { when, offset }: source position `offset` at ctx time `when`
        this.drift = 0;
        this.resyncs = 0;
        this.frameClock = null;           // { mediaTime, at } from requestVideoFrameCallback
        this.ready = false;
        this.stopAt = null;               // auto-pause point for playRange()
        this._raf = 0;
        this._bindVideo();
    }

    /* ---------------- loading ---------------- */

    /** Fetch and decode every track. `onProgress(fraction)` reports bytes downloaded. */
    async load(trackList, onProgress = () => {}) {
        const totals = trackList.map(() => 0);
        const loaded = trackList.map(() => 0);
        const report = () => {
            const total = totals.reduce((a, b) => a + b, 0);
            onProgress(total ? loaded.reduce((a, b) => a + b, 0) / total : 0);
        };
        const buffers = await Promise.all(trackList.map(async (track, i) => {
            const response = await fetch(track.url, { cache: 'no-store' });
            if (!response.ok) throw new Error(`Could not load the ${track.character} track (HTTP ${response.status}).`);
            totals[i] = Number(response.headers.get('Content-Length')) || 1;
            const reader = response.body.getReader();
            const chunks = [];
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value);
                loaded[i] += value.length;
                report();
            }
            const bytes = new Uint8Array(loaded[i]);
            let pos = 0;
            for (const chunk of chunks) { bytes.set(chunk, pos); pos += chunk.length; }
            return this.ctx.decodeAudioData(bytes.buffer);
        }));
        this.tracks = buffers.map((buffer, i) => {
            const gain = this.ctx.createGain();
            gain.connect(this.bus);
            return { buffer, gain, volume: 1, muted: false, solo: false, character: trackList[i].character };
        });
        this.ready = true;
        this._applyGains(true);
        if (!this.video.paused) this._start();
    }

    /** Swap in rebuilt tracks after a line is regenerated, without interrupting the review. */
    async replaceTracks(list) {
        const decoded = await Promise.all(list.map(async ({ index, url }) => {
            const response = await fetch(url, { cache: 'no-store' });
            if (!response.ok) throw new Error(`Could not reload the ${this.tracks[index].character} track (HTTP ${response.status}).`);
            return [index, await this.ctx.decodeAudioData(await response.arrayBuffer())];
        }));
        for (const [index, buffer] of decoded) this.tracks[index].buffer = buffer;
        if (this.playing) this._start(); // restart on the new buffers at the current position
    }

    /* ---------------- transport (delegates to the video) ---------------- */

    get duration() {
        const audio = Math.max(0, ...this.tracks.map(t => t.buffer.duration));
        return Number.isFinite(this.video.duration) ? Math.max(this.video.duration, audio) : audio;
    }

    get currentTime() { return this.video.currentTime; }

    get playing() { return !this.video.paused && !this.video.ended; }

    async play() {
        // resume() must run inside the user's click or key press (autoplay policy).
        if (this.ctx.state !== 'running') await this.ctx.resume();
        try { await this.video.play(); } catch (error) { this._emit('error', { message: error.message }); }
    }

    pause() { this.stopAt = null; this.video.pause(); }

    /** Play from `start` and pause by itself at `end` (seconds): used to audition one line. */
    playRange(start, end) {
        this.seek(start);
        this.stopAt = end;
        return this.play();
    }

    toggle() { return this.playing ? this.pause() : this.play(); }

    seek(seconds) {
        this.stopAt = null;
        const end = Number.isFinite(this.video.duration) ? this.video.duration : this.duration;
        this.video.currentTime = Math.min(Math.max(0, seconds), Math.max(0, end - 0.001));
    }

    /** Step whole frames from the frame on screen. Lands just inside the frame to avoid rounding back. */
    step(frames) {
        this.pause();
        const current = Math.floor(this.video.currentTime * this.fps + 1e-4);
        this.seek((current + frames) / this.fps + 0.001);
    }

    /* ---------------- mixer ---------------- */

    setVolume(i, volume) { this.tracks[i].volume = volume; this._applyGains(); }
    setMuted(i, muted) { this.tracks[i].muted = muted; this._applyGains(); this._emitMix(); }
    setSolo(i, solo) { this.tracks[i].solo = solo; this._applyGains(); this._emitMix(); }
    setMasterVolume(volume) { this.master.gain.setTargetAtTime(volume, this.ctx.currentTime, 0.015); }
    setVideoSound(on, volume) {
        this.video.muted = !on;
        if (volume !== undefined) this.video.volume = volume;
    }

    /** True if track i is currently audible given mute and solo states. */
    audible(i) {
        const anySolo = this.tracks.some(t => t.solo);
        const t = this.tracks[i];
        return anySolo ? t.solo : !t.muted;
    }

    _applyGains(immediate = false) {
        const now = this.ctx.currentTime;
        this.tracks.forEach((track, i) => {
            const value = this.audible(i) ? track.volume : 0;
            if (immediate) track.gain.gain.value = value;
            else track.gain.gain.setTargetAtTime(value, now, 0.012); // ~40 ms, click-free
        });
    }

    /* ---------------- clocks ---------------- */

    /** Seconds between scheduling a sample and hearing it. */
    get outputLatency() {
        const ts = this.ctx.getOutputTimestamp ? this.ctx.getOutputTimestamp() : null;
        if (ts && ts.contextTime > 0) return Math.max(0, this.ctx.currentTime - ts.contextTime);
        return (this.ctx.baseLatency || 0) + (this.ctx.outputLatency || 0);
    }

    /** The video's media time at performance-clock time `perfNow` (ms). */
    _videoTimeAt(perfNow) {
        const fc = this.frameClock;
        if (fc && this.playing && perfNow - fc.at < 250) {
            return fc.mediaTime + (perfNow - fc.at) / 1000 * this.video.playbackRate;
        }
        return this.video.currentTime;
    }

    /** Media time being heard right now and the performance time it refers to. */
    _heard() {
        if (!this.anchor) return null;
        const ts = this.ctx.getOutputTimestamp ? this.ctx.getOutputTimestamp() : null;
        if (ts && ts.contextTime > 0 && ts.performanceTime > 0) {
            return { media: this.anchor.offset + (ts.contextTime - this.anchor.when), perf: ts.performanceTime };
        }
        const media = this.anchor.offset + (this.ctx.currentTime - this.anchor.when) - this.outputLatency;
        return { media, perf: performance.now() };
    }

    /* ---------------- sources ---------------- */

    _start() {
        if (!this.ready || this.video.paused || this.video.seeking) return;
        this._stopSources(true);
        const ctx = this.ctx;
        const when = ctx.currentTime + LEAD;
        // Position the audio so the sample heard at `when + latency` matches the frame shown then.
        const offset = this.video.currentTime + LEAD + this.outputLatency;
        this.sources = this.tracks.map(track => {
            if (offset >= track.buffer.duration) return null;
            const source = ctx.createBufferSource();
            source.buffer = track.buffer;
            source.connect(track.gain);
            source.start(when, offset);
            return source;
        });
        this.anchor = { when, offset };
        this.startedAt = performance.now();
        this.drift = 0;
        const g = this.bus.gain;
        g.cancelScheduledValues(ctx.currentTime);
        g.setValueAtTime(0, when);
        g.linearRampToValueAtTime(1, when + FADE);
        this._emit('state');
    }

    _stopSources(quick = false) {
        const ctx = this.ctx;
        const now = ctx.currentTime;
        const g = this.bus.gain;
        g.cancelScheduledValues(now);
        g.setValueAtTime(g.value, now);
        g.linearRampToValueAtTime(0, now + FADE);
        for (const source of this.sources) {
            if (!source) continue;
            try { source.stop(now + FADE + (quick ? 0 : 0.002)); } catch (_) { /* already stopped */ }
            source.onended = () => source.disconnect();
        }
        this.sources = [];
        this.anchor = null;
    }

    _stop() {
        this._stopSources();
        this._emit('state');
    }

    /* ---------------- video events and the sync loop ---------------- */

    _bindVideo() {
        const v = this.video;
        // The <video> element outlives this player (New episode), so listeners are removable.
        this._listeners = new AbortController();
        const on = (type, fn) => v.addEventListener(type, fn, { signal: this._listeners.signal });
        v.playbackRate = 1; // review always runs at real time
        on('playing', () => this._start());
        on('pause', () => this._stop());
        on('waiting', () => this._stop());       // buffering: hold the dub too
        on('seeking', () => this._stopSources());
        on('seeked', () => { if (!v.paused) this._start(); this._emit('state'); });
        on('ended', () => this._stop());
        on('ratechange', () => { if (v.playbackRate !== 1) v.playbackRate = 1; });

        if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) {
            const onFrame = (now, meta) => {
                if (this._destroyed) return;
                this.frameClock = { mediaTime: meta.mediaTime, at: meta.expectedDisplayTime || now };
                v.requestVideoFrameCallback(onFrame);
            };
            v.requestVideoFrameCallback(onFrame);
        }

        const loop = () => {
            this._raf = requestAnimationFrame(loop);
            this._checkDrift();
            if (this.stopAt != null && this.video.currentTime >= this.stopAt) this.pause();
            this._emit('tick');
        };
        this._raf = requestAnimationFrame(loop);
    }

    _checkDrift() {
        if (!this.anchor || !this.playing) return;
        if ((performance.now() - this.startedAt) / 1000 < SETTLE) return;
        const heard = this._heard();
        if (!heard) return;
        const sample = heard.media - this._videoTimeAt(heard.perf);
        this.drift += (sample - this.drift) * DRIFT_SMOOTHING;
        // One frame of drift is the threshold: beyond it lips visibly lead or lag the voice.
        if (Math.abs(this.drift) > 1 / this.fps) {
            this.resyncs += 1;
            this._start();
        }
    }

    /** Aborted on destroy(): use it for any listener added to the shared <video>. */
    get signal() { return this._listeners.signal; }

    _emit(type, detail = {}) { this.dispatchEvent(new CustomEvent(type, { detail })); }
    _emitMix() { this._emit('mix'); }

    destroy() {
        this._destroyed = true;
        this._listeners.abort();
        cancelAnimationFrame(this._raf);
        this._stopSources(true);
        this.ctx.close();
    }
}
