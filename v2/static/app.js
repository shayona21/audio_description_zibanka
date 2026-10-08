const byId = id => document.getElementById(id);
const form = byId('upload-form');
const fileInput = byId('file-input');
const uploadArea = byId('upload-area');
const outputName = byId('output-name');
const submit = byId('submit-btn');
const submitLabel = byId('submit-label');
const progressSection = byId('progress-section');
const downloadSection = byId('download-section');
const retry = byId('retry-btn');
let selectedFile = null;
let busy = false;
let activeJob = null;
let pollTimer = null;
let lastLogCount = 0;

// Storage may be unavailable in private browsing; generation still works.
function rememberJob(job) {
    try {
        if (job) sessionStorage.setItem('ad-v2-job', job);
        else sessionStorage.removeItem('ad-v2-job');
    } catch (_) { /* Optional reload recovery. */ }
}

function updateSubmitState() {
    submit.disabled = busy || !selectedFile || !outputName.value.trim();
}

function setBusy(value) {
    busy = value;
    [fileInput, outputName, byId('fps'), byId('sheet-name')].forEach(input => {
        input.disabled = value;
    });
    uploadArea.classList.toggle('disabled', value);
    updateSubmitState();
}

function setProgress(value) {
    const percent = Math.max(0, Math.min(100, Number(value) || 0));
    byId('progress-bar').style.width = `${percent}%`;
    byId('progress-percent').textContent = `${percent}%`;
    byId('progress-bar-wrap').setAttribute('aria-valuenow', percent);
}

function appendLog(message, className = '') {
    const line = document.createElement('p');
    line.textContent = message;
    line.className = className;
    byId('log').appendChild(line);
    byId('log').scrollTop = byId('log').scrollHeight;
}

function resetProgress() {
    clearTimeout(pollTimer);
    lastLogCount = 0;
    progressSection.style.display = 'block';
    downloadSection.style.display = 'none';
    retry.hidden = true;
    byId('log').replaceChildren();
    setProgress(0);
}

function finishWithError(message) {
    clearTimeout(pollTimer);
    byId('progress-title').textContent = 'Generation stopped';
    appendLog(message, 'error');
    rememberJob(null);
    activeJob = null;
    retry.hidden = true;
    setBusy(false);
    submitLabel.textContent = 'Try again';
}

function selectFile(file) {
    if (busy) return;
    if (!file.name.toLowerCase().endsWith('.xlsx') || file.size > 20 * 1024 * 1024) {
        selectedFile = null;
        fileInput.value = '';
        byId('file-selection').classList.remove('visible');
        resetProgress();
        finishWithError('Please choose an .xlsx workbook no larger than 20 MB.');
        return;
    }
    selectedFile = file;
    byId('file-type').textContent = 'XLSX';
    byId('file-name').textContent = file.name;
    byId('file-size').textContent = file.size < 1024 ? `${file.size} bytes`
        : file.size < 1024 * 1024 ? `${(file.size / 1024).toFixed(1)} KB`
        : `${(file.size / (1024 * 1024)).toFixed(1)} MB`;
    byId('file-selection').classList.add('visible');
    submitLabel.textContent = 'Generate audio';
    updateSubmitState();
}

fileInput.addEventListener('change', () => {
    if (fileInput.files.length) selectFile(fileInput.files[0]);
});
['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
    uploadArea.addEventListener(eventName, event => {
        event.preventDefault();
        if (busy) return;
        uploadArea.classList.toggle('dragover', eventName === 'dragenter' || eventName === 'dragover');
        if (eventName === 'drop' && event.dataTransfer.files.length) {
            selectFile(event.dataTransfer.files[0]);
        }
    });
});
// Prevent a dropped workbook from navigating away from the app.
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
outputName.addEventListener('input', updateSubmitState);

async function readResponse(response) {
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error('The server returned an unreadable response.'); }
    if (!response.ok) {
        const error = new Error(data.error || 'Request failed.');
        error.status = response.status;
        throw error;
    }
    return data;
}

async function pollStatus() {
    clearTimeout(pollTimer);
    retry.hidden = true;
    try {
        const data = await readResponse(await fetch(`/status/${activeJob}`));
        const lines = data.progress || [];
        lines.slice(lastLogCount).forEach(line => {
            appendLog(line, line.startsWith('Complete') ? 'done'
                : line.startsWith('Review') ? 'warning' : '');
        });
        lastLogCount = lines.length;
        setProgress(data.percent);
        byId('progress-title').textContent = data.phase || 'Generating audio';
        if (data.status === 'done') {
            setProgress(100);
            byId('download-link').href = `/download/${activeJob}`;
            byId('output-file-name').textContent = data.download_name || 'AD_v2_output.zip';
            byId('review-summary').textContent = `${data.tracks} tracks · ${data.flagged_rows} rows flagged for timing review`;
            downloadSection.style.display = 'block';
            rememberJob(null);
            activeJob = null;
            setBusy(false);
            submitLabel.textContent = 'Generate another workbook';
        } else if (data.status === 'error') {
            finishWithError(data.error || 'Audio generation failed.');
        } else {
            pollTimer = setTimeout(pollStatus, 1500);
        }
    } catch (error) {
        if (error.status === 404) {
            finishWithError('This job is no longer available. If the server restarted, check v2/runtime for a completed ZIP before generating again.');
            return;
        }
        byId('progress-title').textContent = 'Connection interrupted';
        appendLog(`${error.message} Your job may still be running. Reconnect to check its progress.`, 'error');
        retry.hidden = false;
        // Keep generation disabled while the current job's state is unknown.
    }
}

retry.addEventListener('click', pollStatus);
form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !selectedFile || !outputName.value.trim()) return;
    const data = new FormData(form);
    data.set('file', selectedFile); // Also handles drag-and-drop files.
    resetProgress();
    setBusy(true);
    submitLabel.textContent = 'Generating…';
    byId('progress-title').textContent = 'Validating workbook';
    appendLog('Uploading and validating your workbook…');
    try {
        const result = await readResponse(await fetch('/upload', {method: 'POST', body: data}));
        activeJob = result.job_id;
        rememberJob(activeJob);
        await pollStatus();
    } catch (error) {
        finishWithError(error.message);
    }
});

try { activeJob = sessionStorage.getItem('ad-v2-job'); } catch (_) { /* Optional. */ }
if (activeJob) {
    resetProgress();
    setBusy(true);
    submitLabel.textContent = 'Generating…';
    pollStatus();
}
