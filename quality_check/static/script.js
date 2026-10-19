/*
 * ScriptPanel: the episode's lines beside the video.
 *
 * - The lines being heard right now are highlighted, and the list follows playback unless the
 *   reviewer has just scrolled it themselves.
 * - Click a line to jump there; the play button auditions just that line.
 * - Edit opens an inline editor: change the words or the [audio tags], then Regenerate re-dubs
 *   that one line with ElevenLabs. The panel stays where it is, so the review carries on.
 */

const FOLLOW_PAUSE_MS = 3500;   // after the reviewer scrolls, stop auto-following for a moment
// Common ElevenLabs v3 audio tags, offered next to the ones this episode already uses.
const COMMON_TAGS = ['[laughs]', '[sighs]', '[whispers]', '[gasps]', '[shouting]', '[crying]',
                     '[angry]', '[sad]', '[excited]', '[nervous]', '[sarcastic]', '[pause]'];

function el(tag, props = {}, ...children) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
}

function icon(path) {
    const span = el('span', { className: 'ico' });
    span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
    return span;
}

/** Text with [audio tags] set apart, so the words and the direction read separately. */
export function renderDialogue(text) {
    const frag = document.createDocumentFragment();
    for (const part of text.split(/(\[[^\]]*\])/)) {
        if (!part) continue;
        frag.append(part.startsWith('[') ? el('span', { className: 'tag', textContent: part }) : part);
    }
    return frag;
}

function seconds(ms) { return `${(ms / 1000).toFixed(1)} s`; }

export class ScriptPanel {
    constructor(root, { lines, colours, handlers }) {
        this.root = root;
        this.colours = colours;
        this.handlers = handlers;
        this.items = new Map();       // row -> { li, line }
        this.speaking = new Set();
        this.selected = null;
        this.editing = null;
        this.busy = null;
        this.followPausedUntil = 0;
        this.followed = null;
        this.list = el('ol', { className: 'lines' });
        this.root.replaceChildren(this.list);
        this.root.addEventListener('wheel', () => this._pauseFollow(), { passive: true });
        this.root.addEventListener('touchmove', () => this._pauseFollow(), { passive: true });
        this.setLines(lines);
    }

    _pauseFollow() { this.followPausedUntil = performance.now() + FOLLOW_PAUSE_MS; }

    /* ---------------- rendering ---------------- */

    setLines(lines) {
        this.lines = lines;
        this.tags = this._episodeTags(lines);
        this.list.replaceChildren();
        this.items.clear();
        for (const line of lines) {
            const li = this._renderLine(line);
            this.items.set(line.row, { li, line });
            this.list.append(li);
        }
        this.speaking = new Set();
        if (this.selected != null) this._mark(this.selected, 'is-selected', true);
        if (this.editing != null && this.busy == null && this.items.has(this.editing)) this._openEditor(this.editing, this._draft);
    }

    _episodeTags(lines) {
        const counts = new Map();
        for (const line of lines) {
            for (const tag of line.text.match(/\[[^\]]+\]/g) || []) counts.set(tag, (counts.get(tag) || 0) + 1);
        }
        const used = [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([tag]) => tag);
        const seen = new Set(used.map(t => t.toLowerCase()));
        return [...used.slice(0, 10), ...COMMON_TAGS.filter(t => !seen.has(t))].slice(0, 16);
    }

    _renderLine(line) {
        const colour = this.colours(line.char_index);
        const badges = el('span', { className: 'badges' });
        if (line.edited) badges.append(el('span', { className: 'badge badge-edit', textContent: 'Edited', title: `Script: ${line.original}` }));
        if (line.overrun_ms) badges.append(el('span', { className: 'badge badge-over', textContent: `+${seconds(line.overrun_ms)}`, title: 'Audio runs past TCR OUT by this much' }));
        if (line.line_speed && line.line_speed !== 1) badges.append(el('span', { className: 'badge badge-speed', textContent: `${line.line_speed.toFixed(2)}x`, title: 'Sped up from the dubbed take' }));
        if (line.delay_ms) badges.append(el('span', { className: 'badge badge-late', textContent: `late ${seconds(line.delay_ms)}`, title: "Starts late because this character's previous line ran long" }));

        const main = el('button', { type: 'button', className: 'line-main', title: 'Jump to this line' },
            el('span', { className: 'line-meta' },
                el('span', { className: 'line-tc', textContent: line.tcr_in }),
                el('span', { className: 'line-who', style: `--c:${colour}` }, line.character),
                badges),
            el('span', { className: 'line-text' }, renderDialogue(line.text)));
        main.addEventListener('click', () => {
            this.select(line.row);
            this.handlers.seek(line);
        });
        main.addEventListener('dblclick', () => this.edit(line.row));

        const play = el('button', { type: 'button', className: 'line-btn play', title: 'Play this line (P)' }, icon('<path d="M8 5v14l11-7z"/>'));
        play.setAttribute('aria-label', `Play row ${line.row}`);
        play.addEventListener('click', () => { this.select(line.row); this.handlers.playLine(line); });
        const edit = el('button', { type: 'button', className: 'line-btn', title: 'Edit and regenerate (Enter)' },
            icon('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>'));
        edit.setAttribute('aria-label', `Edit row ${line.row}`);
        edit.addEventListener('click', () => this.edit(line.row));

        const li = el('li', { className: 'line' }, main, el('div', { className: 'line-tools' }, play, edit));
        li.dataset.row = line.row;
        li.style.setProperty('--c', colour);
        return li;
    }

    _mark(row, cls, on) {
        const item = this.items.get(row);
        if (item) item.li.classList.toggle(cls, on);
    }

    /* ---------------- playback position ---------------- */

    /** Highlight what is heard at `ms`; follow it while playing. Returns the lines being heard. */
    setTime(ms, playing) {
        const now = new Set();
        let next = null;
        for (const line of this.lines) {
            if (line.audio_start_ms <= ms && ms < line.audio_end_ms) now.add(line.row);
            else if (next === null && line.audio_start_ms > ms) next = line.row;
        }
        for (const row of this.speaking) if (!now.has(row)) this._mark(row, 'is-speaking', false);
        for (const row of now) if (!this.speaking.has(row)) this._mark(row, 'is-speaking', true);
        this.speaking = now;
        // Follow while playing: bring the current (or next) line into view whenever it changes,
        // including the moment playback starts, unless the reviewer is scrolling or editing.
        const target = now.size ? Math.min(...now) : next;
        if (!playing) this.followed = null;
        else if (target != null && target !== this.followed && this.editing === null
                 && performance.now() > this.followPausedUntil) {
            this.followed = target;
            this._scrollTo(target);
        }
        return [...now].map(row => this.items.get(row).line);
    }

    _scrollTo(row, force = false) {
        const item = this.items.get(row);
        if (!item) return;
        const box = this.root.getBoundingClientRect();
        const r = item.li.getBoundingClientRect();
        if (force || r.top < box.top + 8 || r.bottom > box.bottom - 8) {
            const target = this.root.scrollTop + (r.top - box.top) - box.height * 0.3;
            this.root.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
        }
    }

    /* ---------------- selection and keyboard navigation ---------------- */

    select(row, scroll = false) {
        if (this.selected != null) this._mark(this.selected, 'is-selected', false);
        this.selected = row;
        this._mark(row, 'is-selected', true);
        if (scroll) this._scrollTo(row);
        this.handlers.select(row);
    }

    /** Move the selection up or down the list (from the line being heard if none is selected). */
    step(direction) {
        const rows = this.lines.map(l => l.row);
        const from = this.selected ?? (this.speaking.size ? Math.min(...this.speaking) : null);
        let index = from == null ? (direction > 0 ? 0 : rows.length - 1) : rows.indexOf(from) + direction;
        index = Math.max(0, Math.min(rows.length - 1, index));
        this.select(rows[index], true);
        return this.items.get(rows[index]).line;
    }

    /* ---------------- editing ---------------- */

    edit(row = this.selected) {
        if (row == null || this.busy != null) return;
        if (this.editing != null && this.editing !== row) this._closeEditor();
        this.select(row);
        this._openEditor(row, null);
    }

    _openEditor(row, draft) {
        const { li, line } = this.items.get(row);
        li.querySelector('.editor')?.remove();
        this.editing = row;
        li.classList.add('is-editing');

        const area = el('textarea', { className: 'editor-text', rows: 3, value: draft ?? line.text, spellcheck: true });
        area.setAttribute('aria-label', `Dialogue for row ${row}`);
        const chips = el('div', { className: 'tag-chips' }, ...this.tags.map(tag => {
            const chip = el('button', { type: 'button', className: 'tag-chip', textContent: tag });
            chip.addEventListener('click', () => insertTag(area, tag));
            return chip;
        }));
        const error = el('p', { className: 'editor-error', hidden: true, role: 'alert' });
        const regen = el('button', { type: 'button', className: 'regen-btn', textContent: 'Regenerate line' });
        const cancel = el('button', { type: 'button', className: 'link-btn', textContent: 'Cancel' });
        const reset = line.edited ? el('button', { type: 'button', className: 'link-btn', textContent: 'Use script wording' }) : null;
        const hint = el('span', { className: 'editor-hint', textContent: 'Ctrl + Enter to regenerate. Uses ElevenLabs credits.' });

        const submit = () => this._submit(row, area, error, regen);
        regen.addEventListener('click', submit);
        cancel.addEventListener('click', () => this._closeEditor());
        if (reset) reset.addEventListener('click', () => { area.value = line.original; area.focus(); });
        area.addEventListener('input', () => { this._draft = area.value; });
        area.addEventListener('keydown', event => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); }
            else if (event.key === 'Escape') { event.preventDefault(); this._closeEditor(); }
            else if (event.key === 'Enter') event.preventDefault(); // dialogue stays on one line
        });

        const editor = el('div', { className: 'editor' },
            area,
            el('p', { className: 'chips-label', textContent: 'Insert an audio tag' }),
            chips,
            error,
            el('div', { className: 'editor-actions' }, regen, cancel, ...(reset ? [reset] : []), hint));
        li.append(editor);
        this._draft = area.value;
        area.focus();
        area.setSelectionRange(area.value.length, area.value.length);
        this._scrollTo(row);
    }

    _closeEditor() {
        if (this.editing == null) return;
        const item = this.items.get(this.editing);
        if (item) {
            item.li.classList.remove('is-editing');
            item.li.querySelector('.editor')?.remove();
        }
        this.editing = null;
        this._draft = null;
    }

    async _submit(row, area, error, button) {
        const text = area.value.replace(/\s+/g, ' ').trim();
        if (!text) { error.textContent = 'The line cannot be empty.'; error.hidden = false; return; }
        if (this.busy != null) return;
        this.busy = row;
        error.hidden = true;
        button.disabled = true;
        button.textContent = 'Regenerating…';
        area.readOnly = true;
        this._mark(row, 'is-busy', true);
        try {
            await this.handlers.regenerate(row, text);
            this.busy = null;
            this._closeEditor();
            // setLines() has re-rendered the list by now; keep the line in view and selected.
            this.select(row, true);
            this._mark(row, 'is-fresh', true);
            setTimeout(() => this._mark(row, 'is-fresh', false), 2500);
        } catch (exc) {
            this.busy = null;
            const current = this.items.get(row);
            const target = current?.li.querySelector('.editor-error') || error;
            target.textContent = exc.message;
            target.hidden = false;
            const btn = current?.li.querySelector('.regen-btn') || button;
            btn.disabled = false;
            btn.textContent = 'Regenerate line';
            const field = current?.li.querySelector('.editor-text') || area;
            field.readOnly = false;
            this._mark(row, 'is-busy', false);
        }
    }
}

/** Insert a tag at the caret with sensible spacing, keeping focus in the field. */
function insertTag(area, tag) {
    const { selectionStart: a, selectionEnd: b, value } = area;
    const before = value.slice(0, a), after = value.slice(b);
    const pre = before && !/\s$/.test(before) ? ' ' : '';
    const post = after && !/^\s/.test(after) ? ' ' : (after ? '' : ' ');
    area.value = before + pre + tag + post + after;
    const caret = (before + pre + tag + post).length;
    area.focus();
    area.setSelectionRange(caret, caret);
    area.dispatchEvent(new Event('input'));
}
