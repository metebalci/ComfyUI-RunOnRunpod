import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// --- Job state constants ---
const JOB_STATE = {
    PREPARING: "preparing",
    QUEUED: "queued",
    RUNNING: "running",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELLED: "cancelled",
};

// --- Job list (newest first) ---
let jobs = [];
let jobListEl = null;

function addJob(jobId) {
    const job = {
        id: jobId,
        state: JOB_STATE.PREPARING,
        message: "Preparing...",
        files: [],
        createdAt: new Date(),
    };
    jobs.unshift(job);
    renderJobList();
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
        deleteInputsAfterJob: app.extensionManager.setting.get("Run on Runpod.Job.deleteInputsAfterJob") ?? false,
        deleteOutputsAfterJob: app.extensionManager.setting.get("Run on Runpod.Job.deleteOutputsAfterJob") ?? true,
        uploadMissingModels: app.extensionManager.setting.get("Run on Runpod.Job.uploadMissingModels") ?? true,
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
    const colors = {
        [JOB_STATE.PREPARING]: { bg: "#3a3a1a", text: "#cccc44" },
        [JOB_STATE.QUEUED]: { bg: "#1a2a3a", text: "#4488cc" },
        [JOB_STATE.RUNNING]: { bg: "#1a3a2a", text: "#44cc88" },
        [JOB_STATE.COMPLETED]: { bg: "#1a3a1a", text: "#44cc44" },
        [JOB_STATE.FAILED]: { bg: "#3a1a1a", text: "#cc4444" },
        [JOB_STATE.CANCELLED]: { bg: "#2a2a2a", text: "#888888" },
    };
    const c = colors[state] || { bg: "#2a2a2a", text: "#888" };
    return `<span style="
        display:inline-block; padding:2px 6px; border-radius:3px; font-size:11px; font-weight:600;
        background:${c.bg}; color:${c.text};
    ">${state}</span>`;
}

// --- Render a single job card ---
function renderJobCard(job) {
    const card = document.getElementById(`runpod-job-${job.id}`);
    if (!card) {
        renderJobList();
        return;
    }

    const shortId = job.id.length > 12 ? job.id.slice(0, 12) + "..." : job.id;
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
        : "";

    card.innerHTML = `
        <div class="runpod-job-header">
            <div>
                <span class="runpod-job-id" title="${job.id}">${shortId}</span>
                <span class="runpod-job-time">${time}</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
                ${stateBadge(job.state)}
                ${cancelBtnHtml}
            </div>
        </div>
        <div class="runpod-job-message">${job.message || ""}</div>
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
}

// --- Render full job list ---
function renderJobList() {
    if (!jobListEl) return;
    jobListEl.innerHTML = "";

    if (jobs.length === 0) {
        jobListEl.innerHTML = '<div class="runpod-empty">No jobs yet. Click Run to submit a workflow.</div>';
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
    const tempId = `prep-${Date.now()}`;
    const job = addJob(tempId);

    try {
        const prompt = await app.graphToPrompt();

        const res = await api.fetchApi("/RunOnRunpod/submit", {
            method: "POST",
            body: JSON.stringify({ workflow: prompt.output, settings: getSettings() }),
        });
        const data = await res.json();

        if (data.error) {
            console.error("[RunOnRunpod] Submit error:", data.error);
            job.id = tempId;
            job.state = JOB_STATE.FAILED;
            job.message = data.error;
            renderJobList();
            return;
        }

        // Replace temp ID with real job ID
        const oldCard = document.getElementById(`runpod-job-${tempId}`);
        job.id = data.job_id;
        if (oldCard) oldCard.id = `runpod-job-${job.id}`;
        renderJobCard(job);
    } catch (err) {
        console.error("[RunOnRunpod] Submit error:", err);
        job.state = JOB_STATE.FAILED;
        job.message = String(err);
        renderJobList();
    }
}

async function cancelJob(jobId) {
    const job = findJob(jobId);
    if (!job) return;

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

// --- CSS ---
const STYLES = `
    .runpod-sidebar {
        display: flex;
        flex-direction: column;
        height: 100%;
        font-family: Inter, sans-serif;
        font-size: 13px;
        color: #ddd;
        background: #1e1e1e;
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
    .runpod-btn {
        flex: 1;
        padding: 6px 10px;
        border: 1px solid #555;
        border-radius: 4px;
        background: #2a2a2a;
        color: #ccc;
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        transition: background 0.15s;
    }
    .runpod-btn:hover {
        background: #3a3a3a;
        color: #fff;
    }
    .runpod-btn.run {
        background: #2a6e2a;
        border-color: #3a8e3a;
        color: #cfc;
    }
    .runpod-btn.run:hover {
        background: #3a8e3a;
    }
    .runpod-btn.clean {
        font-size: 11px;
        color: #aaa;
    }
    .runpod-btn.pulsing {
        animation: runpod-pulse 1s ease-in-out infinite;
    }
    @keyframes runpod-pulse {
        0%, 100% { background: #2a2a2a; }
        50% { background: #3a3a3a; }
    }
    .runpod-jobs {
        flex: 1;
        overflow-y: auto;
        padding: 8px;
    }
    .runpod-job-card {
        background: #252525;
        border: 1px solid #333;
        border-radius: 6px;
        padding: 8px 10px;
        margin-bottom: 6px;
    }
    .runpod-job-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 4px;
    }
    .runpod-job-id {
        font-family: monospace;
        font-size: 11px;
        color: #888;
    }
    .runpod-job-time {
        font-size: 11px;
        color: #666;
        margin-left: 8px;
    }
    .runpod-job-message {
        font-size: 12px;
        color: #aaa;
        margin-top: 2px;
        word-break: break-word;
    }
    .runpod-job-message:empty {
        display: none;
    }
    .runpod-job-preview {
        max-width: 100%;
        border-radius: 4px;
        margin-top: 6px;
    }
    .runpod-job-cancel {
        background: none;
        border: 1px solid #555;
        border-radius: 3px;
        color: #888;
        cursor: pointer;
        font-size: 10px;
        padding: 1px 5px;
        line-height: 1;
    }
    .runpod-job-cancel:hover {
        background: #6e2a2a;
        border-color: #8e3a3a;
        color: #cc4444;
    }
    .runpod-empty {
        color: #666;
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
            id: "Run on Runpod.Job.uploadMissingModels",
            name: "Upload missing models automatically",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "Run on Runpod.Job.deleteOutputsAfterJob",
            name: "Delete outputs from network volume after job",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "Run on Runpod.Job.deleteInputsAfterJob",
            name: "Delete inputs from network volume after job",
            type: "boolean",
            defaultValue: false,
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
        // Inject styles
        const style = document.createElement("style");
        style.textContent = STYLES;
        document.head.appendChild(style);

        // WebSocket event handling — route to correct job
        api.addEventListener("runonrunpod", (event) => {
            const { event: evt, job_id, message, files, error } = event.detail;

            // For progress events during preparation (before job_id is assigned),
            // update the most recent preparing job
            if (evt === "progress") {
                const prepJob = jobs.find(j =>
                    j.state === JOB_STATE.PREPARING || j.id === job_id
                );
                if (prepJob) {
                    updateJob(prepJob.id, { message });
                }
                return;
            }

            if (!job_id) return;
            const job = findJob(job_id);
            if (!job) return;

            switch (evt) {
                case "queued":
                    updateJob(job_id, { state: JOB_STATE.QUEUED, message: "Queued on RunPod..." });
                    break;
                case "running":
                    updateJob(job_id, { state: JOB_STATE.RUNNING, message: "Running..." });
                    break;
                case "completed":
                    console.log(`[RunOnRunpod] Job ${job_id} completed, ${(files || []).length} file(s)`);
                    updateJob(job_id, {
                        state: JOB_STATE.COMPLETED,
                        message: `Completed - ${(files || []).length} file(s)`,
                        files: files || [],
                    });
                    break;
                case "failed":
                    console.error(`[RunOnRunpod] Job ${job_id} failed: ${error}`);
                    updateJob(job_id, {
                        state: JOB_STATE.FAILED,
                        message: error || "Unknown error",
                    });
                    break;
            }
        });

        // Register sidebar tab
        app.extensionManager.registerSidebarTab({
            id: "runpod",
            icon: "pi pi-cloud",
            title: "RunPod",
            tooltip: "Run on RunPod",
            type: "custom",
            render: (el) => {
                el.innerHTML = "";

                const container = document.createElement("div");
                container.className = "runpod-sidebar";

                // Toolbar (always visible at top)
                const toolbar = document.createElement("div");
                toolbar.className = "runpod-toolbar";

                const row1 = document.createElement("div");
                row1.className = "runpod-toolbar-row";

                const runBtn = document.createElement("button");
                runBtn.className = "runpod-btn run";
                runBtn.textContent = "Run";
                runBtn.addEventListener("click", submitJob);

                row1.appendChild(runBtn);

                const row2 = document.createElement("div");
                row2.className = "runpod-toolbar-row";

                const cleanInputsBtn = document.createElement("button");
                cleanInputsBtn.className = "runpod-btn clean";
                cleanInputsBtn.textContent = "Clean Inputs";
                cleanInputsBtn.addEventListener("click", () => cleanFolder("inputs", cleanInputsBtn));

                const cleanOutputsBtn = document.createElement("button");
                cleanOutputsBtn.className = "runpod-btn clean";
                cleanOutputsBtn.textContent = "Clean Outputs";
                cleanOutputsBtn.addEventListener("click", () => cleanFolder("outputs", cleanOutputsBtn));

                row2.appendChild(cleanInputsBtn);
                row2.appendChild(cleanOutputsBtn);

                toolbar.appendChild(row1);
                toolbar.appendChild(row2);

                // Job list (scrollable)
                jobListEl = document.createElement("div");
                jobListEl.className = "runpod-jobs";

                container.appendChild(toolbar);
                container.appendChild(jobListEl);
                el.appendChild(container);

                renderJobList();
            },
        });

        console.log("[RunOnRunpod] Extension loaded (sidebar)");
    },
});
