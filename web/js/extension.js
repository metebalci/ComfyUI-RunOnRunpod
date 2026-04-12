import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// --- Job state constants ---
// FAILED is reserved for worker-side workflow failures (check ComfyUI worker
// logs). ERROR is for pre-submission failures (bad credentials, upload
// errors, network issues) — fixable locally. TIMED_OUT is distinct so users
// know to check endpoint timeout / worker availability rather than workflow
// correctness.
const JOB_STATE = {
    PREPARING: "preparing",
    QUEUED: "queued",
    RUNNING: "running",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELLED: "cancelled",
    TIMED_OUT: "timed_out",
    ERROR: "error",
};

// --- Job list (newest first) ---
let jobs = [];
let jobListEl = null;
let workerInfoEl = null;

function renderWorkerInfo(info) {
    if (!workerInfoEl) return;
    if (!info || !info.worker_version) {
        workerInfoEl.textContent = "Worker info appears after your first submit.";
        return;
    }
    const parts = [
        `Worker ${info.worker_version}`,
        `protocol ${info.protocol_version}`,
    ];
    if (info.cuda_version) parts.push(`CUDA ${info.cuda_version}`);
    if (info.pytorch_version) parts.push(`Torch ${info.pytorch_version}`);
    if (info.comfyui_version && info.comfyui_version !== "unknown") {
        parts.push(`ComfyUI ${info.comfyui_version}`);
    }
    workerInfoEl.textContent = parts.join(" · ");
    workerInfoEl.title = parts.join("\n");
}

// localStorage persistence — all jobs are persisted, including in-flight
// ones. On reload, finished jobs are restored as-is (after a file
// existence check); in-flight jobs are revalidated via the
// recover-jobs backend endpoint, which queries RunPod for their
// current status and re-attaches polling if needed.
const STORAGE_KEY = "runonrunpod.jobs";
const FINISHED_STATES = [
    JOB_STATE.COMPLETED,
    JOB_STATE.FAILED,
    JOB_STATE.CANCELLED,
    JOB_STATE.TIMED_OUT,
    JOB_STATE.ERROR,
];
const ACTIVE_STATES = [
    JOB_STATE.PREPARING,
    JOB_STATE.QUEUED,
    JOB_STATE.RUNNING,
];

function getHistoryCap() {
    const v = app.extensionManager.setting.get("Run on Runpod.Job.historySize");
    return Number.isFinite(v) && v >= 0 ? v : 20;
}

function saveJobs() {
    try {
        const cap = getHistoryCap();
        // In-flight jobs are always persisted (no cap) so reload can
        // recover them. Finished jobs are capped by the history setting;
        // a cap of 0 means "no history persistence" but in-flight jobs
        // still survive a reload.
        const inFlight = jobs.filter(j => ACTIVE_STATES.includes(j.state));
        const finished = cap > 0
            ? jobs.filter(j => FINISHED_STATES.includes(j.state)).slice(0, cap)
            : [];
        const serialized = [...inFlight, ...finished].map(j => ({
            id: j.id,
            state: j.state,
            message: j.message,
            files: j.files || [],
            createdAt: j.createdAt.toISOString(),
        }));
        if (serialized.length === 0) {
            localStorage.removeItem(STORAGE_KEY);
        } else {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(serialized));
        }
    } catch (err) {
        console.error("[RunOnRunpod] saveJobs error:", err);
    }
}

async function loadJobs() {
    let raw;
    try {
        raw = localStorage.getItem(STORAGE_KEY);
    } catch (err) {
        return;
    }
    if (!raw) return;

    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (err) {
        return;
    }
    if (!Array.isArray(parsed)) return;

    const restored = parsed.map(j => ({
        id: j.id,
        state: j.state,
        message: j.message || "",
        files: Array.isArray(j.files) ? j.files : [],
        uploadPercent: -1,
        fetchResults: null,
        createdAt: new Date(j.createdAt),
    }));

    // Verify completed-job files still exist on disk; drop missing files
    // and drop the whole card if a completed job ends up with zero files.
    const allFiles = [...new Set(restored.flatMap(j => j.files))];
    let existingSet = new Set(allFiles);
    if (allFiles.length > 0) {
        try {
            const resp = await api.fetchApi("/RunOnRunpod/check-local-outputs", {
                method: "POST",
                body: JSON.stringify({ files: allFiles }),
            });
            const data = await resp.json();
            existingSet = new Set(data.existing || []);
        } catch (err) {
            console.error("[RunOnRunpod] check-local-outputs error:", err);
        }
    }

    const filtered = restored.filter(j => {
        j.files = j.files.filter(f => existingSet.has(f));
        if (j.state === JOB_STATE.COMPLETED && j.files.length === 0) return false;
        return true;
    });

    jobs = filtered;
    renderJobList();
    saveJobs();

    // For each in-flight job, ask the backend to query RunPod for
    // its current status and re-attach polling if needed.
    const inFlightJobs = jobs.filter(j => ACTIVE_STATES.includes(j.state));
    const inFlightIds = inFlightJobs.map(j => j.id);
    if (inFlightIds.length > 0) {
        // Show a placeholder message on each card so the user sees
        // something is happening while we wait for RunPod's status.
        for (const j of inFlightJobs) {
            j.message = "Querying job state on RunPod...";
        }
        renderJobList();

        try {
            const resp = await api.fetchApi("/RunOnRunpod/recover-jobs", {
                method: "POST",
                body: JSON.stringify({ settings: getSettings(), job_ids: inFlightIds }),
            });
            const data = await resp.json();
            for (const entry of data.recovered || []) {
                const job = findJob(entry.job_id);
                if (!job) continue;
                if (entry.state === "lost") {
                    const idx = jobs.findIndex(j => j.id === entry.job_id);
                    if (idx >= 0) jobs.splice(idx, 1);
                    continue;
                }
                const updates = { state: entry.state };
                if (entry.state === "completed") {
                    updates.files = entry.files || [];
                    updates.message = `Completed - ${(entry.files || []).length} output file(s)`;
                } else if (entry.state === "failed") {
                    updates.message = `Failed: ${entry.error || "unknown error"}`;
                } else if (entry.state === "cancelled") {
                    updates.message = "Cancelled";
                } else if (entry.state === "timed_out") {
                    updates.message = "Timed out";
                } else if (entry.state === "queued") {
                    updates.message = "Queued on RunPod (recovered)";
                } else if (entry.state === "running") {
                    updates.message = "Running (recovered)";
                }
                Object.assign(job, updates);
            }
            renderJobList();
            saveJobs();
        } catch (err) {
            console.error("[RunOnRunpod] recover-jobs error:", err);
        }
    }
}

function addJob(jobId) {
    const job = {
        id: jobId,
        state: JOB_STATE.PREPARING,
        message: "Preparing...",
        files: [],
        uploadPercent: -1,
        fetchResults: null, // [{filename, status, error?}] during worker fetch_models
        createdAt: new Date(),
    };
    jobs.unshift(job);
    renderJobList();
    saveJobs();
    return job;
}

function findJob(jobId) {
    return jobs.find(j => j.id === jobId);
}

function updateJob(jobId, updates) {
    const job = findJob(jobId);
    if (!job) return;
    Object.assign(job, updates);
    renderJobCard(job);
    saveJobs();
}

// --- One-shot migration for renamed setting IDs ---
// Older versions stored these under different IDs. On startup, if the
// old key holds a non-default value and the new key is still at its
// default, copy it forward. Heuristic: we can't distinguish "user
// explicitly set the new key to its default" from "never touched it",
// so we only migrate when the old value diverges from the default.
function migrateSettings() {
    const moves = [
        {
            oldId: "Run on Runpod.Job.deleteInputsAfterJob",
            newId: "Run on Runpod.Storage.deleteInputsAfterJob",
            defaultValue: false,
        },
        {
            oldId: "Run on Runpod.Job.deleteOutputsAfterJob",
            newId: "Run on Runpod.Storage.deleteOutputsAfterJob",
            defaultValue: true,
        },
        {
            oldId: "Run on Runpod.Job.downloadFromTheSource",
            newId: "Run on Runpod.Job.downloadModelsFromTheSource",
            defaultValue: false,
        },
    ];
    for (const { oldId, newId, defaultValue } of moves) {
        try {
            const oldVal = app.extensionManager.setting.get(oldId);
            if (oldVal === undefined || oldVal === defaultValue) continue;
            const newVal = app.extensionManager.setting.get(newId);
            if (newVal !== defaultValue) continue;
            app.extensionManager.setting.set(newId, oldVal);
            console.log(`[RunOnRunpod] Migrated setting ${oldId} -> ${newId} (${oldVal})`);
        } catch (err) {
            console.error(`[RunOnRunpod] migrate ${oldId} error:`, err);
        }
    }
}

// --- Settings helper ---
function getSettings() {
    return {
        apiKey: app.extensionManager.setting.get("Run on Runpod.Keys.apiKey") || "",
        endpointId: app.extensionManager.setting.get("Run on Runpod.Serverless.endpointId") || "",
        s3AccessKey: app.extensionManager.setting.get("Run on Runpod.Keys.s3AccessKey") || "",
        s3SecretKey: app.extensionManager.setting.get("Run on Runpod.Keys.s3SecretKey") || "",
        endpointUrl: app.extensionManager.setting.get("Run on Runpod.Storage.endpointUrl") || "",
        region: app.extensionManager.setting.get("Run on Runpod.Storage.region") || "",
        bucketName: app.extensionManager.setting.get("Run on Runpod.Storage.bucketName") || "",
        deleteInputsAfterJob: app.extensionManager.setting.get("Run on Runpod.Storage.deleteInputsAfterJob") ?? false,
        deleteOutputsAfterJob: app.extensionManager.setting.get("Run on Runpod.Storage.deleteOutputsAfterJob") ?? true,
        uploadMissingModels: app.extensionManager.setting.get("Run on Runpod.Job.uploadMissingModels") ?? true,
        downloadModelsFromTheSource: app.extensionManager.setting.get("Run on Runpod.Job.downloadModelsFromTheSource") ?? false,
        civitaiApiKey: app.extensionManager.setting.get("Run on Runpod.Keys.civitaiApiKey") || "",
        hfToken: app.extensionManager.setting.get("Run on Runpod.Keys.hfToken") || "",
    };
}

// --- Media helpers ---
const IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp", ".gif"];
const VIDEO_EXTS = [".mp4", ".webm"];
const AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".flac"];

function findPreview(files) {
    for (const exts of [IMAGE_EXTS, VIDEO_EXTS, AUDIO_EXTS]) {
        const found = files.find(p => {
            const ext = p.substring(p.lastIndexOf(".")).toLowerCase();
            return exts.includes(ext);
        });
        if (found) return found;
    }
    return null;
}

// --- State badge colors ---
function stateBadge(state) {
    // Badge colors pull from PrimeVue palette tokens so they adapt to
    // light/dark themes. Each state uses a palette color for the text and
    // a 20%-opacity tint of the same color for the background via
    // color-mix, which gives a subtle filled chip that reads well on both
    // themes without needing hand-tuned dark/light pairs.
    const palettes = {
        [JOB_STATE.PREPARING]: "var(--p-amber-500, #cccc44)",
        [JOB_STATE.QUEUED]: "var(--p-blue-500, #4488cc)",
        [JOB_STATE.RUNNING]: "var(--p-emerald-500, #44cc88)",
        [JOB_STATE.COMPLETED]: "var(--p-green-500, #44cc44)",
        [JOB_STATE.FAILED]: "var(--p-red-500, #cc4444)",
        [JOB_STATE.CANCELLED]: "var(--p-text-muted-color, #888)",
        [JOB_STATE.TIMED_OUT]: "var(--p-orange-500, #cc8844)",
        [JOB_STATE.ERROR]: "var(--p-red-500, #cc4444)",
    };
    const color = palettes[state] || "var(--p-text-muted-color, #888)";
    const label = state === JOB_STATE.TIMED_OUT ? "timed out" : state;
    return `<span style="
        display:inline-block; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600;
        background:color-mix(in srgb, ${color} 20%, transparent);
        color:${color};
    ">${label}</span>`;
}

// --- Render a single job card ---
function renderJobCard(job) {
    const card = document.getElementById(`runpod-job-${job.id}`);
    if (!card) {
        renderJobList();
        return;
    }

    const time = job.createdAt.toLocaleTimeString();
    const isActive = [JOB_STATE.PREPARING, JOB_STATE.QUEUED, JOB_STATE.RUNNING].includes(job.state);

    let previewHtml = "";
    if (job.state === JOB_STATE.COMPLETED && job.files.length > 0) {
        const previewFile = findPreview(job.files);
        if (previewFile) {
            const parts = previewFile.split("/");
            const filename = parts.pop();
            const subfolder = parts.join("/");
            const ext = filename.substring(filename.lastIndexOf(".")).toLowerCase();
            const src = `/view?filename=${encodeURIComponent(filename)}&subfolder=${encodeURIComponent(subfolder)}&type=output`;

            if (IMAGE_EXTS.includes(ext)) {
                previewHtml = `<img src="${src}" class="runpod-job-preview">`;
            } else if (VIDEO_EXTS.includes(ext)) {
                previewHtml = `<video src="${src}" controls class="runpod-job-preview"></video>`;
            } else if (AUDIO_EXTS.includes(ext)) {
                previewHtml = `<audio src="${src}" controls style="width:100%;margin-top:6px;"></audio>`;
            }
        }
    }

    const cancelBtnHtml = isActive
        ? `<button class="runpod-job-cancel" data-job-id="${job.id}" title="Cancel">X</button>`
        : `<button class="runpod-job-remove" data-job-id="${job.id}" title="Remove from list (also deletes local output files)">X</button>`;

    let fetchListHtml = "";
    if (job.fetchResults && job.fetchResults.length > 0) {
        const rows = job.fetchResults.map(r => {
            const icon = r.status === "done" ? "✓"
                : r.status === "failed" ? "✗"
                : r.status === "downloading" ? "…"
                : "○";
            const color = r.status === "done" ? "#44cc44"
                : r.status === "failed" ? "#cc4444"
                : r.status === "downloading" ? "#cccc44"
                : "var(--p-text-muted-color, #888)";
            const nameStyle = r.status === "pending" ? "opacity:0.6;" : "";
            const title = r.error ? ` title="${r.error.replace(/"/g, "&quot;")}"` : "";
            return `<div class="runpod-fetch-item"${title}><span style="color:${color};font-weight:600;">${icon}</span> <span style="${nameStyle}">${r.filename}</span></div>`;
        }).join("");
        fetchListHtml = `<div class="runpod-fetch-list">${rows}</div>`;
    }

    card.innerHTML = `
        <div class="runpod-job-header">
            <span class="runpod-job-time">${time}</span>
            <div style="display:flex;align-items:center;gap:6px;">
                ${stateBadge(job.state)}
                ${cancelBtnHtml}
            </div>
        </div>
        <div class="runpod-job-id" title="${job.id}">${job.id}</div>
        <div class="runpod-job-message">${job.message || ""}</div>
        <div class="runpod-job-progress">${job.uploadPercent >= 0 ? `<div class="runpod-job-progress-bar" style="width:${job.uploadPercent}%"></div>` : ""}</div>
        ${fetchListHtml}
        ${previewHtml}
    `;

    // Attach cancel handler
    const cancelBtn = card.querySelector(".runpod-job-cancel");
    if (cancelBtn) {
        cancelBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            cancelJob(job.id);
        });
    }

    const removeBtn = card.querySelector(".runpod-job-remove");
    if (removeBtn) {
        removeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            removeJob(job.id);
        });
    }
}

function showRemoveJobDialog(fileCount) {
    return new Promise((resolve) => {
        const backdrop = document.createElement("div");
        backdrop.className = "runpod-modal-backdrop";
        const modal = document.createElement("div");
        modal.className = "runpod-modal";
        modal.innerHTML = `
            <h3>Remove job</h3>
            <p style="margin:8px 0 16px 0;">This job produced ${fileCount} output file${fileCount === 1 ? "" : "s"}.</p>
            <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;">
                <button class="runpod-modal-close" data-action="cancel">Cancel</button>
                <button class="runpod-modal-close" data-action="job-only">Delete Job Only</button>
                <button class="runpod-modal-close" data-action="delete-files">Delete All Output Files</button>
            </div>
        `;

        const finish = (action) => {
            backdrop.remove();
            document.removeEventListener("keydown", onKey);
            resolve(action);
        };
        function onKey(e) {
            if (e.key === "Escape") finish("cancel");
        }
        backdrop.addEventListener("click", (e) => {
            if (e.target === backdrop) finish("cancel");
        });
        modal.querySelectorAll("button[data-action]").forEach(btn => {
            btn.addEventListener("click", () => finish(btn.dataset.action));
        });
        document.addEventListener("keydown", onKey);

        backdrop.appendChild(modal);
        document.body.appendChild(backdrop);
    });
}

async function removeJob(jobId) {
    const job = findJob(jobId);
    if (!job) return;

    const behavior = app.extensionManager.setting.get("Run on Runpod.Job.removeBehavior") || "delete";
    const hasFiles = job.files && job.files.length > 0;

    let deleteFiles = false;
    if (!hasFiles) {
        deleteFiles = false;
    } else if (behavior === "delete") {
        deleteFiles = true;
    } else if (behavior === "keep") {
        deleteFiles = false;
    } else { // "ask"
        const action = await showRemoveJobDialog(job.files.length);
        if (action === "cancel") return;
        deleteFiles = action === "delete-files";
    }

    if (deleteFiles && hasFiles) {
        try {
            await api.fetchApi("/RunOnRunpod/delete-local-outputs", {
                method: "POST",
                body: JSON.stringify({ files: job.files }),
            });
        } catch (err) {
            console.error("[RunOnRunpod] delete-local-outputs error:", err);
        }
    }

    const idx = jobs.findIndex(j => j.id === jobId);
    if (idx >= 0) jobs.splice(idx, 1);
    renderJobList();
    saveJobs();
}

// --- Render full job list ---
function renderJobList() {
    if (!jobListEl) return;
    jobListEl.innerHTML = "";

    // Settings warning
    const s = getSettings();
    const missing = [];
    if (!s.apiKey) missing.push("API Key");
    if (!s.endpointId) missing.push("Endpoint ID");
    if (!s.s3AccessKey) missing.push("S3 Access Key");
    if (!s.s3SecretKey) missing.push("S3 Secret Key");
    if (!s.endpointUrl) missing.push("Endpoint URL");
    if (!s.bucketName) missing.push("Bucket Name");
    if (missing.length > 0) {
        jobListEl.innerHTML = `<div class="runpod-warning">Configure in Settings &gt; Run on Runpod:<br>${missing.join(", ")}</div>`;
    }

    if (jobs.length === 0) {
        jobListEl.innerHTML += '<div class="runpod-empty">No jobs yet. Click Run to submit a workflow.</div>';
        return;
    }

    for (const job of jobs) {
        const card = document.createElement("div");
        card.className = "runpod-job-card";
        card.id = `runpod-job-${job.id}`;
        jobListEl.appendChild(card);
        renderJobCard(job);
    }
}

// --- Actions ---
async function submitJob() {
    const s = getSettings();
    const missing = [];
    if (!s.apiKey) missing.push("API Key");
    if (!s.endpointId) missing.push("Endpoint ID");
    if (!s.s3AccessKey) missing.push("S3 Access Key");
    if (!s.s3SecretKey) missing.push("S3 Secret Key");
    if (!s.endpointUrl) missing.push("Endpoint URL");
    if (!s.bucketName) missing.push("Bucket Name");
    if (missing.length > 0) {
        console.error("[RunOnRunpod] Missing settings:", missing.join(", "));
        alert(`Missing settings: ${missing.join(", ")}`);
        return;
    }

    // Create a temporary job entry while preparing
    const prepId = `prep-${crypto.randomUUID()}`;
    const job = addJob(prepId);

    try {
        const prompt = await app.graphToPrompt();

        const res = await api.fetchApi("/RunOnRunpod/submit", {
            method: "POST",
            body: JSON.stringify({ workflow: prompt.output, settings: getSettings(), prep_id: prepId }),
        });
        const data = await res.json();

        if (data.error) {
            if (data.error === "Cancelled") {
                job.state = JOB_STATE.CANCELLED;
                job.message = "Cancelled";
            } else {
                // Pre-submission errors (credentials, validation, upload
                // failures) use ERROR so users know to fix it locally —
                // distinct from FAILED which means the workflow ran on the
                // worker and that is where to look.
                console.error("[RunOnRunpod] Submit error:", data.error);
                job.state = JOB_STATE.ERROR;
                job.message = data.error;
            }
            renderJobList();
            return;
        }

        // Replace prep ID with real job ID (WS "queued" event handles state update)
        if (job.id !== data.job_id) {
            const oldCard = document.getElementById(`runpod-job-${job.id}`);
            job.id = data.job_id;
            if (oldCard) oldCard.id = `runpod-job-${job.id}`;
            renderJobCard(job);
        }
    } catch (err) {
        console.error("[RunOnRunpod] Submit error:", err);
        job.state = JOB_STATE.ERROR;
        job.message = String(err);
        renderJobList();
    }
}

async function cancelJob(jobId) {
    const job = findJob(jobId);
    if (!job) return;

    if (job.state === JOB_STATE.PREPARING) {
        // Cancel the upload/preparation phase — backend will stop after current upload finishes
        updateJob(jobId, { message: "Cancelling after current upload..." });
        try {
            await api.fetchApi("/RunOnRunpod/cancel-prepare", {
                method: "POST",
                body: JSON.stringify({ prep_id: jobId }),
            });
        } catch (err) {
            console.error("[RunOnRunpod] Cancel-prepare error:", err);
        }
        // The submit fetch will return an error which submitJob() handles
        return;
    }

    try {
        await api.fetchApi("/RunOnRunpod/cancel", {
            method: "POST",
            body: JSON.stringify({ job_id: jobId, settings: getSettings() }),
        });
    } catch (err) {
        console.error("[RunOnRunpod] Cancel error:", err);
    }
    updateJob(jobId, { state: JOB_STATE.CANCELLED, message: "Cancelled" });
}

// Latency check modal — opened immediately on button click and filled in
// live as per-region results stream back via WebSocket events.
let _latencyModal = null;

function _renderLatencyRows(results) {
    const sorted = results
        .filter(r => r.median_ms != null)
        .slice()
        .sort((a, b) => {
            if (a.median_ms !== b.median_ms) return a.median_ms - b.median_ms;
            return (a.stdev_ms || 0) - (b.stdev_ms || 0);
        });
    const best = sorted.length > 0 ? sorted[0].region : null;
    return sorted.map(r => {
        const cls = r.region === best ? "runpod-best" : "";
        return `<tr class="${cls}">
            <td>${r.region}</td>
            <td class="num">${r.median_ms}</td>
            <td class="num">${r.min_ms}</td>
            <td class="num">${r.max_ms}</td>
            <td class="num">${r.stdev_ms}</td>
        </tr>`;
    }).join("");
}

function showLatencyModal() {
    if (_latencyModal) return _latencyModal;

    const backdrop = document.createElement("div");
    backdrop.className = "runpod-modal-backdrop";
    const modal = document.createElement("div");
    modal.className = "runpod-modal";

    modal.innerHTML = `
        <h3>Runpod datacenter latency</h3>
        <div class="runpod-latency-status">Loading regions...</div>
        <table>
            <thead>
                <tr>
                    <th>Region</th>
                    <th style="text-align:right;">Median (ms)</th>
                    <th style="text-align:right;">Min</th>
                    <th style="text-align:right;">Max</th>
                    <th style="text-align:right;">StdDev</th>
                </tr>
            </thead>
            <tbody class="runpod-latency-body"></tbody>
        </table>
        <div style="clear:both;"><button class="runpod-modal-close">Close</button></div>
    `;

    const state = {
        backdrop,
        modal,
        statusEl: modal.querySelector(".runpod-latency-status"),
        bodyEl: modal.querySelector(".runpod-latency-body"),
        total: 0,
        completed: 0,
        results: [],
        done: false,
    };

    const close = () => {
        backdrop.remove();
        _latencyModal = null;
        document.removeEventListener("keydown", onKey);
    };
    function onKey(e) {
        if (e.key === "Escape") close();
    }
    backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) close();
    });
    modal.querySelector(".runpod-modal-close").addEventListener("click", close);
    document.addEventListener("keydown", onKey);

    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    _latencyModal = state;
    return state;
}

function _updateLatencyStatus(state) {
    if (state.done) {
        const shown = state.results.filter(r => r.median_ms != null).length;
        const dropped = state.total - shown;
        state.statusEl.textContent = dropped > 0
            ? `Done — ${shown} reachable, ${dropped} unreachable (hidden)`
            : `Done — ${shown} regions`;
    } else if (state.total > 0) {
        state.statusEl.textContent = `Checking ${state.completed}/${state.total} regions...`;
    }
}

function handleLatencyStart(total) {
    const state = _latencyModal;
    if (!state) return;
    state.total = total;
    state.completed = 0;
    state.results = [];
    state.bodyEl.innerHTML = "";
    _updateLatencyStatus(state);
}

function handleLatencyProgress(result) {
    const state = _latencyModal;
    if (!state) return;
    state.completed += 1;
    if (result && result.median_ms != null) {
        state.results.push(result);
        state.bodyEl.innerHTML = _renderLatencyRows(state.results);
    }
    _updateLatencyStatus(state);
}

function handleLatencyDone(results) {
    const state = _latencyModal;
    if (!state) return;
    state.done = true;
    // Final authoritative list from the backend — replaces whatever we
    // accumulated from the streaming events.
    state.results = results || state.results;
    state.bodyEl.innerHTML = _renderLatencyRows(state.results);
    _updateLatencyStatus(state);
}

function handleLatencyError(error) {
    const state = _latencyModal;
    if (!state) return;
    state.done = true;
    state.statusEl.textContent = `Failed: ${error}`;
    state.statusEl.style.color = "#cc4444";
}

async function checkLatency(btn) {
    btn.disabled = true;
    showLatencyModal();

    try {
        const res = await api.fetchApi("/RunOnRunpod/check-latency", { method: "POST" });
        const data = await res.json();
        if (data.error) {
            handleLatencyError(data.error);
        } else {
            handleLatencyDone(data.results || []);
        }
    } catch (err) {
        console.error("[RunOnRunpod] check-latency error:", err);
        handleLatencyError(String(err));
    }

    btn.disabled = false;
}

async function cleanFolder(folder, btn) {
    const s = getSettings();
    if (!confirm(`Delete all files from ${folder}/ on s3://${s.bucketName} at ${s.endpointUrl}?`)) return;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.classList.add("pulsing");
    btn.textContent = `Cleaning ${folder}...`;

    try {
        const res = await api.fetchApi("/RunOnRunpod/clean", {
            method: "POST",
            body: JSON.stringify({ folder, settings: getSettings() }),
        });
        const data = await res.json();
        if (data.error) {
            btn.textContent = `Failed`;
        } else {
            btn.textContent = `Deleted ${data.deleted} file(s)`;
        }
    } catch (err) {
        btn.textContent = `Failed`;
    }

    btn.classList.remove("pulsing");
    setTimeout(() => {
        btn.textContent = originalText;
        btn.disabled = false;
    }, 2000);
}

async function cleanAll(btn) {
    const s = getSettings();
    const msg =
        `This will delete EVERYTHING on the network volume (s3://${s.bucketName}):\n\n` +
        `  • inputs/\n  • outputs/\n  • models/  ← your uploaded models will be gone\n\n` +
        `Models will need to be re-uploaded (or re-downloaded by the worker) on the next job.\n\n` +
        `Are you absolutely sure?`;
    if (!confirm(msg)) return;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.classList.add("pulsing");
    btn.textContent = `Cleaning all...`;

    try {
        const res = await api.fetchApi("/RunOnRunpod/clean", {
            method: "POST",
            body: JSON.stringify({ folder: "all", settings: getSettings() }),
        });
        const data = await res.json();
        if (data.error) {
            btn.textContent = `Failed`;
        } else {
            btn.textContent = `Deleted ${data.deleted} file(s)`;
        }
    } catch (err) {
        btn.textContent = `Failed`;
    }

    btn.classList.remove("pulsing");
    setTimeout(() => {
        btn.textContent = originalText;
        btn.disabled = false;
    }, 2000);
}

// --- CSS ---
const STYLES = `
    .runpod-sidebar {
        display: flex;
        flex-direction: column;
        height: 100%;
    }
    /* Fallback for p-toolbar when opened before PrimeVue loads it */
    .runpod-sidebar .p-toolbar {
        display: flex;
        align-items: center;
    }
    .runpod-sidebar .p-toolbar-start {
        display: flex;
        flex: 1;
    }
    .runpod-sidebar .p-toolbar-end {
        display: flex;
    }
    .runpod-version-label {
        margin-left: 8px;
        font-size: 11px;
        font-weight: 400;
        color: var(--p-text-muted-color, #888);
    }
    .runpod-title-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2rem;
        height: 2rem;
        border-radius: 0.375rem;
        text-decoration: none;
        color: var(--p-text-color, inherit);
        font-size: 14px;
        font-weight: 700;
        flex-shrink: 0;
        background: transparent;
        border: none;
        cursor: pointer;
    }
    .runpod-title-btn:hover {
        background: var(--p-content-hover-background, rgba(255,255,255,0.08));
    }
    .runpod-toolbar {
        flex-shrink: 0;
        padding: 10px 12px;
        border-bottom: 1px solid #333;
        display: flex;
        flex-direction: column;
        gap: 6px;
    }
    .runpod-toolbar-row {
        display: flex;
        gap: 6px;
    }
    /* Every button has a filled background so it's visible regardless of
       theme. Colors use PrimeVue palette tokens which are defined on
       :root in Aura and are consistent across light/dark (the theme only
       picks a slightly different shade). Fallbacks match the token
       values in case an older PrimeVue build doesn't expose them. */
    .runpod-btn {
        flex: 1;
        min-width: 0;
        height: 40px;
        padding: 0 6px;
        border: 1px solid transparent;
        border-radius: 8px;
        color: var(--p-text-color, #fff);
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        line-height: 1.15;
        text-align: center;
        white-space: normal;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background 0.15s;
    }
    /* Run — green */
    .runpod-btn.run {
        background: color-mix(in srgb, var(--p-green-600, #16a34a) 65%, transparent);
        border-color: color-mix(in srgb, var(--p-green-600, #16a34a) 85%, transparent);
    }
    .runpod-btn.run:hover {
        background: color-mix(in srgb, var(--p-green-500, #22c55e) 85%, transparent);
        border-color: var(--p-green-500, #22c55e);
    }
    /* Clean Inputs / Outputs / Jobs / Check Latency — amber */
    .runpod-btn.clean {
        background: color-mix(in srgb, var(--p-amber-600, #d97706) 65%, transparent);
        border-color: color-mix(in srgb, var(--p-amber-600, #d97706) 85%, transparent);
    }
    .runpod-btn.clean:hover {
        background: color-mix(in srgb, var(--p-amber-500, #f59e0b) 85%, transparent);
        border-color: var(--p-amber-500, #f59e0b);
    }
    /* Clean All — red (ordered after .clean so it wins on Clean All,
       which carries both classes). */
    .runpod-btn.danger {
        background: color-mix(in srgb, var(--p-red-600, #dc2626) 65%, transparent);
        border-color: color-mix(in srgb, var(--p-red-600, #dc2626) 85%, transparent);
    }
    .runpod-btn.danger:hover {
        background: color-mix(in srgb, var(--p-red-500, #ef4444) 85%, transparent);
        border-color: var(--p-red-500, #ef4444);
    }
    .runpod-btn.pulsing {
        animation: runpod-pulse 1s ease-in-out infinite;
    }
    @keyframes runpod-pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.6; }
    }
    .runpod-worker-info {
        padding: 6px 12px;
        font-size: 11px;
        color: var(--p-text-muted-color, #888);
        border-bottom: 1px solid var(--p-content-border-color, #333);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .runpod-jobs {
        flex: 1;
        overflow-y: auto;
        padding: 8px;
    }
    .runpod-job-card {
        background: var(--p-content-background, #252525);
        border: 1px solid var(--p-content-border-color, #333);
        border-radius: 8px;
        padding: 8px 10px;
        margin-bottom: 6px;
    }
    .runpod-job-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .runpod-job-id {
        font-family: monospace;
        font-size: 11px;
        color: var(--p-text-muted-color, #666);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        margin-top: 4px;
    }
    .runpod-job-time {
        font-size: 11px;
        color: var(--p-text-muted-color, #888);
    }
    .runpod-job-message {
        font-size: 12px;
        color: var(--p-text-color, #aaa);
        margin-top: 4px;
        word-break: break-word;
    }
    .runpod-job-message:empty {
        display: none;
    }
    .runpod-job-progress {
        margin-top: 4px;
        height: 4px;
        background: var(--p-content-border-color, #333);
        border-radius: 2px;
        overflow: hidden;
    }
    .runpod-job-progress:empty {
        display: none;
    }
    .runpod-job-progress-bar {
        height: 100%;
        background: var(--p-primary-color, #4488cc);
        border-radius: 2px;
        transition: width 0.3s ease;
    }
    .runpod-job-preview {
        max-width: 100%;
        border-radius: 4px;
        margin-top: 6px;
    }
    .runpod-fetch-list {
        margin-top: 6px;
        font-size: 11px;
        color: var(--p-text-color, #aaa);
        max-height: 120px;
        overflow-y: auto;
    }
    .runpod-fetch-item {
        padding: 1px 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .runpod-modal-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.6);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 9999;
    }
    .runpod-modal {
        background: var(--p-content-background, #1e1e1e);
        border: 1px solid var(--p-content-border-color, #444);
        border-radius: 8px;
        padding: 16px 20px;
        min-width: 360px;
        max-width: 520px;
        max-height: 80vh;
        overflow-y: auto;
        color: var(--p-text-color, #ddd);
        box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    }
    .runpod-modal h3 {
        margin: 0 0 12px 0;
        font-size: 14px;
        color: var(--p-text-color, #fff);
    }
    .runpod-latency-status {
        font-size: 11px;
        color: var(--p-text-muted-color, #888);
        margin-bottom: 8px;
    }
    .runpod-modal table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
    }
    .runpod-modal th, .runpod-modal td {
        padding: 4px 8px;
        text-align: left;
        border-bottom: 1px solid var(--p-content-border-color, #333);
    }
    .runpod-modal th {
        color: var(--p-text-muted-color, #888);
        font-weight: 500;
    }
    .runpod-modal td.num {
        text-align: right;
        font-family: monospace;
    }
    .runpod-modal .runpod-best {
        color: var(--p-green-500, #44cc44);
        font-weight: 600;
    }
    .runpod-modal .runpod-unreachable {
        color: var(--p-text-muted-color, #666);
    }
    .runpod-modal-close {
        margin-top: 12px;
        float: right;
        background: var(--p-content-background, #2a2a2a);
        border: 1px solid var(--p-content-border-color, #555);
        color: var(--p-text-color, #ccc);
        padding: 4px 12px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 12px;
    }
    .runpod-modal-close:hover {
        background: var(--p-content-hover-background, #3a3a3a);
        color: var(--p-text-color, #fff);
    }
    .runpod-job-cancel {
        background: none;
        border: 1px solid var(--p-content-border-color, #555);
        border-radius: 4px;
        color: var(--p-text-muted-color, #888);
        cursor: pointer;
        font-size: 10px;
        padding: 1px 5px;
        line-height: 1;
    }
    .runpod-job-cancel:hover {
        background: color-mix(in srgb, var(--p-red-600, #cc4444) 30%, transparent);
        border-color: var(--p-red-500, #8e3a3a);
        color: var(--p-red-500, #cc4444);
    }
    .runpod-job-remove {
        background: none;
        border: 1px solid var(--p-content-border-color, #555);
        border-radius: 4px;
        color: var(--p-text-muted-color, #888);
        cursor: pointer;
        font-size: 11px;
        padding: 1px 5px;
        line-height: 1;
    }
    .runpod-job-remove:hover {
        background: color-mix(in srgb, var(--p-red-600, #cc4444) 30%, transparent);
        border-color: var(--p-red-500, #8e3a3a);
        color: var(--p-red-500, #cc4444);
    }
    .runpod-warning {
        padding: 8px 12px;
        margin: 8px;
        font-size: 12px;
        color: var(--p-amber-500, #cccc44);
        background: color-mix(in srgb, var(--p-amber-500, #cccc44) 15%, transparent);
        border: 1px solid color-mix(in srgb, var(--p-amber-500, #cccc44) 35%, transparent);
        border-radius: 8px;
    }
    .runpod-empty {
        color: var(--p-text-muted-color, #666);
        text-align: center;
        padding: 24px 12px;
        font-size: 12px;
    }
`;

// --- Extension registration ---
app.registerExtension({
    name: "RunOnRunpod",

    settings: [
        {
            id: "Run on Runpod.Job.downloadModelsFromTheSource",
            name: "Download models from the source when possible",
            type: "boolean",
            defaultValue: false,
            tooltip: "When enabled, missing models are downloaded directly by the worker from their original source (ComfyUI Manager database, HuggingFace cache URL, or CivitAI) instead of being uploaded from your machine. Much faster for large checkpoints since the worker has datacenter bandwidth. File hashes may be sent to CivitAI to identify models when enabled.",
        },
        {
            id: "Run on Runpod.Job.uploadMissingModels",
            name: "Upload missing models automatically",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "Run on Runpod.Storage.deleteOutputsAfterJob",
            name: "Delete output files from network volume after job finishes",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "Run on Runpod.Storage.deleteInputsAfterJob",
            name: "Delete input files from network volume after job finishes",
            type: "boolean",
            defaultValue: false,
        },
        {
            id: "Run on Runpod.Job.historySize",
            name: "Number of jobs kept in history",
            type: "number",
            defaultValue: 20,
            tooltip: "How many finished job cards to remember across page reloads and ComfyUI restarts. Set to 0 to disable persistence entirely.",
            attrs: { min: 0, max: 200, step: 1 },
        },
        {
            id: "Run on Runpod.Job.removeBehavior",
            name: "When removing a job",
            type: "combo",
            defaultValue: "delete",
            options: [
                { text: "Delete output files", value: "delete" },
                { text: "Keep output files", value: "keep" },
                { text: "Ask each time", value: "ask" },
            ],
            tooltip: "What happens when you click the X on a finished job card. 'Delete' removes the local output files (and any folder that becomes empty). 'Keep' only removes the card. 'Ask' shows a confirmation every time, even for single-file jobs.",
        },
        {
            id: "Run on Runpod.Keys.hfToken",
            name: "HuggingFace Token",
            type: "text",
            defaultValue: "",
            attrs: { type: "password" },
            tooltip: "Optional. Used when 'Download from the source' is enabled and the worker needs to fetch gated/private HuggingFace models.",
        },
        {
            id: "Run on Runpod.Keys.civitaiApiKey",
            name: "CivitAI API Key",
            type: "text",
            defaultValue: "",
            attrs: { type: "password" },
            tooltip: "Optional. Used when 'Download from the source' is enabled and the worker needs to fetch CivitAI models that require authentication.",
        },
        {
            id: "Run on Runpod.Keys.s3SecretKey",
            name: "S3 Secret Key",
            type: "text",
            defaultValue: "",
            attrs: { type: "password" },
        },
        {
            id: "Run on Runpod.Keys.s3AccessKey",
            name: "S3 Access Key",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Keys.apiKey",
            name: "API Key",
            type: "text",
            defaultValue: "",
            attrs: { type: "password" },
        },
        {
            id: "Run on Runpod.Storage.endpointUrl",
            name: "Endpoint URL",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Storage.region",
            name: "Region",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Storage.bucketName",
            name: "Bucket Name",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Serverless.endpointId",
            name: "Endpoint ID",
            type: "text",
            defaultValue: "",
        },
    ],

    async setup() {
        migrateSettings();

        // Inject styles
        const style = document.createElement("style");
        style.textContent = STYLES;
        document.head.appendChild(style);

        // WebSocket event handling — route to correct job
        api.addEventListener("runonrunpod", (event) => {
            const { event: evt, job_id, prep_id, message, files, error, percent, uploaded_mb, total_mb, results, total, result } = event.detail;

            // Worker info push from the prep flow's version action.
            if (evt === "worker_info") {
                renderWorkerInfo(event.detail);
                return;
            }

            // Latency check events — bypass the job list entirely.
            if (evt === "latency_start") {
                handleLatencyStart(total || 0);
                return;
            }
            if (evt === "latency_progress") {
                handleLatencyProgress(result || {});
                return;
            }
            if (evt === "latency_done") {
                handleLatencyDone(results || []);
                return;
            }
            if (evt === "latency_error") {
                handleLatencyError(error || "unknown error");
                return;
            }

            // For progress/upload/fetch events during preparation, route by prep_id
            if (evt === "progress" || evt === "upload_progress" || evt === "fetch_progress") {
                const targetId = prep_id || job_id;
                const prepJob = targetId ? findJob(targetId) : null;
                if (prepJob) {
                    const updates = { message };
                    if (evt === "upload_progress" && percent != null) {
                        updates.message = `${message} (${uploaded_mb}/${total_mb} MB)`;
                        updates.uploadPercent = percent;
                    } else if (evt === "fetch_progress") {
                        updates.fetchResults = results || [];
                    } else {
                        updates.uploadPercent = -1;
                    }
                    updateJob(prepJob.id, updates);
                }
                return;
            }

            if (!job_id) return;

            // For "queued", the job might still have prep_id — link them
            if (evt === "queued" && prep_id) {
                const prepJob = findJob(prep_id);
                if (prepJob) {
                    const oldCard = document.getElementById(`runpod-job-${prep_id}`);
                    prepJob.id = job_id;
                    if (oldCard) oldCard.id = `runpod-job-${job_id}`;
                }
            }

            const job = findJob(job_id);
            if (!job) return;

            switch (evt) {
                case "queued":
                    updateJob(job_id, { state: JOB_STATE.QUEUED, message: "Queued on RunPod...", uploadPercent: -1 });
                    break;
                case "running":
                    updateJob(job_id, { state: JOB_STATE.RUNNING, message: "Running..." });
                    break;
                case "completed":
                    console.log(`[RunOnRunpod] Job ${job_id} completed, ${(files || []).length} file(s)`);
                    updateJob(job_id, {
                        state: JOB_STATE.COMPLETED,
                        message: `Completed - ${(files || []).length} output file(s)`,
                        files: files || [],
                    });
                    break;
                case "failed":
                    console.error(`[RunOnRunpod] Job ${job_id} failed: ${error}`);
                    updateJob(job_id, {
                        state: JOB_STATE.FAILED,
                        message: error || "Workflow failed on worker",
                    });
                    break;
                case "cancelled":
                    console.log(`[RunOnRunpod] Job ${job_id} cancelled`);
                    updateJob(job_id, {
                        state: JOB_STATE.CANCELLED,
                        message: "Cancelled",
                    });
                    break;
                case "timed_out":
                    console.warn(`[RunOnRunpod] Job ${job_id} timed out: ${error}`);
                    updateJob(job_id, {
                        state: JOB_STATE.TIMED_OUT,
                        message: error || "Timed out",
                    });
                    break;
            }
        });

        // Register sidebar tab
        app.extensionManager.registerSidebarTab({
            id: "runpod",
            icon: "pi pi-cloud",
            title: "RoRp",
            tooltip: "Run on Runpod",
            type: "custom",
            render: (el) => {
                el.innerHTML = "";

                const container = document.createElement("div");
                container.className = "runpod-sidebar";

                // Title (native p-toolbar classes, with CSS fallback for first-open)
                const title = document.createElement("div");
                title.className = "p-toolbar p-component min-h-16 rounded-none border-x-0 border-t-0 bg-transparent px-3 2xl:px-4";
                title.setAttribute("role", "toolbar");

                const titleStart = document.createElement("div");
                titleStart.className = "p-toolbar-start min-w-0 flex-1 overflow-hidden";

                const titleText = document.createElement("span");
                titleText.className = "truncate font-bold";
                titleText.title = "Run on Runpod";
                titleText.textContent = "Run on Runpod";

                const versionLabel = document.createElement("span");
                versionLabel.className = "runpod-version-label";
                versionLabel.textContent = "";

                titleStart.appendChild(titleText);
                titleStart.appendChild(versionLabel);

                api.fetchApi("/RunOnRunpod/info")
                    .then(r => r.json())
                    .then(info => {
                        if (info && info.plugin_version) {
                            versionLabel.textContent = `${info.plugin_version} · p${info.protocol_version}`;
                            versionLabel.title = `Plugin ${info.plugin_version}, protocol version ${info.protocol_version}`;
                        }
                        if (info && info.worker_info) {
                            renderWorkerInfo(info.worker_info);
                        }
                    })
                    .catch(err => console.error("[RunOnRunpod] info fetch error:", err));

                const titleEnd = document.createElement("div");
                titleEnd.className = "p-toolbar-end";

                const infoBtn = document.createElement("a");
                infoBtn.className = "runpod-title-btn";
                infoBtn.href = "https://github.com/metebalci/ComfyUI-RunOnRunpod";
                infoBtn.target = "_blank";
                infoBtn.title = "GitHub";
                infoBtn.textContent = "?";

                titleEnd.appendChild(infoBtn);

                title.appendChild(titleStart);
                title.appendChild(titleEnd);

                // Toolbar (always visible at top)
                const toolbar = document.createElement("div");
                toolbar.className = "runpod-toolbar";

                const runBtn = document.createElement("button");
                runBtn.className = "runpod-btn run";
                runBtn.innerHTML = '<i class="pi pi-send" style="margin-right:6px;font-size:12px;"></i>Run';
                runBtn.title = "Submit current workflow to Runpod";
                runBtn.addEventListener("click", submitJob);

                const cleanInputsBtn = document.createElement("button");
                cleanInputsBtn.className = "runpod-btn clean";
                cleanInputsBtn.textContent = "Clean Inputs";
                cleanInputsBtn.title = "Delete uploaded input files from S3 bucket";
                cleanInputsBtn.addEventListener("click", () => cleanFolder("inputs", cleanInputsBtn));

                const cleanOutputsBtn = document.createElement("button");
                cleanOutputsBtn.className = "runpod-btn clean";
                cleanOutputsBtn.textContent = "Clean Outputs";
                cleanOutputsBtn.title = "Delete output files from S3 bucket";
                cleanOutputsBtn.addEventListener("click", () => cleanFolder("outputs", cleanOutputsBtn));

                const cleanJobsBtn = document.createElement("button");
                cleanJobsBtn.className = "runpod-btn clean";
                cleanJobsBtn.textContent = "Clean Jobs";
                cleanJobsBtn.title = "Cancel active jobs and clear the list";
                cleanJobsBtn.addEventListener("click", async () => {
                    const active = [JOB_STATE.PREPARING, JOB_STATE.QUEUED, JOB_STATE.RUNNING];
                    const activeJobs = jobs.filter(j => active.includes(j.state));

                    if (activeJobs.length > 0) {
                        const ok = confirm(
                            `Cancel ${activeJobs.length} active job(s) and clear the list?\n\n` +
                            `This purges the endpoint queue on RunPod and cancels running jobs. ` +
                            `Any jobs submitted to this endpoint from elsewhere will also be affected.`
                        );
                        if (!ok) return;

                        // Send every tracked prep_id so in-flight preps also stop.
                        const prepIds = activeJobs
                            .filter(j => j.state === JOB_STATE.PREPARING)
                            .map(j => j.id);

                        try {
                            await api.fetchApi("/RunOnRunpod/purge-queue", {
                                method: "POST",
                                body: JSON.stringify({ settings: getSettings(), prep_ids: prepIds }),
                            });
                        } catch (err) {
                            console.error("[RunOnRunpod] purge-queue error:", err);
                        }
                    }

                    jobs = [];
                    renderJobList();
                    saveJobs();
                });

                const cleanAllBtn = document.createElement("button");
                cleanAllBtn.className = "runpod-btn clean danger";
                cleanAllBtn.textContent = "Clean All";
                cleanAllBtn.title = "Delete ALL files on the network volume: inputs, outputs, AND models";
                cleanAllBtn.addEventListener("click", () => cleanAll(cleanAllBtn));

                const latencyBtn = document.createElement("button");
                latencyBtn.className = "runpod-btn clean";
                latencyBtn.textContent = "Check Latency";
                latencyBtn.title = "Measure HTTPS round-trip to every Runpod S3 datacenter";
                latencyBtn.addEventListener("click", () => checkLatency(latencyBtn));

                const row1 = document.createElement("div");
                row1.className = "runpod-toolbar-row";
                row1.appendChild(runBtn);
                row1.appendChild(cleanJobsBtn);
                toolbar.appendChild(row1);

                const row2 = document.createElement("div");
                row2.className = "runpod-toolbar-row";
                row2.appendChild(cleanInputsBtn);
                row2.appendChild(cleanOutputsBtn);
                row2.appendChild(cleanAllBtn);
                row2.appendChild(latencyBtn);
                toolbar.appendChild(row2);

                // Worker info row — populated by the version action on
                // first successful submit (cached server-side and pushed
                // via the worker_info WebSocket event).
                workerInfoEl = document.createElement("div");
                workerInfoEl.className = "runpod-worker-info";
                workerInfoEl.textContent = "Worker info appears after your first submit.";

                // Job list (scrollable)
                jobListEl = document.createElement("div");
                jobListEl.className = "runpod-jobs";

                container.appendChild(title);
                container.appendChild(toolbar);
                container.appendChild(workerInfoEl);
                container.appendChild(jobListEl);
                el.appendChild(container);

                renderJobList();
                loadJobs();
            },
        });

        console.log("[RunOnRunpod] Extension loaded (sidebar)");
    },
});
