import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// --- State machine ---
const STATE = {
    IDLE: "idle",
    PREPARING: "preparing",
    QUEUED: "queued",
    RUNNING: "running",
};

let currentState = STATE.IDLE;
let currentJobId = null;
let currentInputFiles = {};
let pollInterval = null;

app.registerExtension({
    name: "RunOnRunpod",

    // ComfyUI renders settings in reverse order — last in array appears first in UI
    settings: [
        // Job (bottom in UI)
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
        // Keys
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
        // Storage (UI order: Bucket Name, Region, Endpoint URL)
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
        // Serverless (top in UI)
        {
            id: "Run on Runpod.Serverless.endpointId",
            name: "Endpoint ID",
            type: "text",
            defaultValue: "",
        },
    ],

    async setup() {

        // --- Helper to gather current settings ---
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

        // --- Inject CSS ---
        const style = document.createElement("style");
        style.textContent = `
            .runpod-panel {
                position: absolute;
                top: calc(100% + 4px);
                right: 0;
                width: 320px;
                max-height: 400px;
                overflow-y: auto;
                background: #1e1e1e;
                border: 1px solid #444;
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.4);
                z-index: 1000;
                font-family: Inter, sans-serif;
                font-size: 13px;
                color: #ddd;
            }
            .runpod-panel-header {
                padding: 8px 12px;
                border-bottom: 1px solid #333;
                font-weight: 600;
            }
            .runpod-panel-actions {
                display: flex;
                gap: 8px;
                padding: 8px 12px;
            }
            .runpod-panel-btn {
                padding: 4px 12px;
                border: 1px solid #555;
                border-radius: 4px;
                background: #2a2a2a;
                color: #ccc;
                cursor: pointer;
                font-size: 12px;
            }
            .runpod-panel-btn:hover:not(:disabled) {
                background: #3a3a3a;
                color: #fff;
            }
            .runpod-panel-btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }
            .runpod-panel-btn.run {
                background: #2a6e2a;
                border-color: #3a8e3a;
            }
            .runpod-panel-btn.run:hover:not(:disabled) {
                background: #3a8e3a;
            }
            .runpod-panel-btn.cancel {
                background: #6e2a2a;
                border-color: #8e3a3a;
            }
            .runpod-panel-btn.cancel:hover:not(:disabled) {
                background: #8e3a3a;
            }
            .runpod-panel-detail {
                padding: 8px 12px;
                color: #aaa;
                font-size: 12px;
            }
            .runpod-panel-detail:empty {
                display: none;
            }
            .runpod-panel-gallery {
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
                padding: 8px 12px;
            }
            .runpod-panel-gallery:empty {
                display: none;
            }
            .runpod-panel-gallery img {
                max-width: 100%;
                border-radius: 4px;
            }
        `;
        document.head.appendChild(style);

        // --- Create panel ---
        const wrapper = document.createElement("div");
        wrapper.style.position = "relative";
        wrapper.style.display = "flex";
        wrapper.style.alignItems = "center";
        wrapper.style.height = "100%";

        const panel = document.createElement("div");
        panel.className = "runpod-panel";

        // Header
        const panelHeader = document.createElement("div");
        panelHeader.className = "runpod-panel-header";
        panelHeader.textContent = "Run on RunPod";

        // Row 1: Run | Cancel
        const row1 = document.createElement("div");
        row1.className = "runpod-panel-actions";

        const runBtn = document.createElement("button");
        runBtn.className = "runpod-panel-btn run";
        runBtn.textContent = "Run";

        const cancelBtn = document.createElement("button");
        cancelBtn.className = "runpod-panel-btn cancel";
        cancelBtn.textContent = "Cancel";
        cancelBtn.disabled = true;

        row1.appendChild(runBtn);
        row1.appendChild(cancelBtn);

        // Row 2: Clean Inputs | Clean Outputs
        const row2 = document.createElement("div");
        row2.className = "runpod-panel-actions";

        const cleanInputsBtn = document.createElement("button");
        cleanInputsBtn.className = "runpod-panel-btn";
        cleanInputsBtn.textContent = "Clean Inputs";

        const cleanOutputsBtn = document.createElement("button");
        cleanOutputsBtn.className = "runpod-panel-btn";
        cleanOutputsBtn.textContent = "Clean Outputs";

        row2.appendChild(cleanInputsBtn);
        row2.appendChild(cleanOutputsBtn);

        // Status detail
        const panelDetail = document.createElement("div");
        panelDetail.className = "runpod-panel-detail";

        // Gallery
        const panelGallery = document.createElement("div");
        panelGallery.className = "runpod-panel-gallery";

        panel.appendChild(panelHeader);
        panel.appendChild(row1);
        panel.appendChild(row2);
        panel.appendChild(panelDetail);
        panel.appendChild(panelGallery);
        wrapper.appendChild(panel);

        // --- Panel helpers ---
        const IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp", ".gif"];
        const VIDEO_EXTS = [".mp4", ".webm"];
        const AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".flac"];

        function findPreview(gallery) {
            for (const exts of [IMAGE_EXTS, VIDEO_EXTS, AUDIO_EXTS]) {
                const found = gallery.find(p => {
                    const ext = p.substring(p.lastIndexOf(".")).toLowerCase();
                    return exts.includes(ext);
                });
                if (found) return found;
            }
            return null;
        }

        function showPanel(statusText, detailText = "", gallery = []) {
            panelDetail.textContent = detailText ? `${statusText}: ${detailText}` : statusText;
            panelGallery.innerHTML = "";

            const previewable = findPreview(gallery);
            if (!previewable) return;

            const parts = previewable.split("/");
            const filename = parts.pop();
            const subfolder = parts.join("/");
            const ext = filename.substring(filename.lastIndexOf(".")).toLowerCase();
            const src = `/view?filename=${encodeURIComponent(filename)}&subfolder=${encodeURIComponent(subfolder)}&type=output`;

            if (IMAGE_EXTS.includes(ext)) {
                const img = document.createElement("img");
                img.src = src;
                panelGallery.appendChild(img);
            } else if (VIDEO_EXTS.includes(ext)) {
                const video = document.createElement("video");
                video.src = src;
                video.controls = true;
                video.style.maxWidth = "100%";
                video.style.borderRadius = "4px";
                panelGallery.appendChild(video);
            } else if (AUDIO_EXTS.includes(ext)) {
                const audio = document.createElement("audio");
                audio.src = src;
                audio.controls = true;
                audio.style.width = "100%";
                panelGallery.appendChild(audio);
            }
        }

        async function cleanFolder(folder) {
            const s = getSettings();
            if (!confirm(`Delete all files from ${folder}/ on s3://${s.bucketName} at ${s.endpointUrl}?`)) return;
            try {
                panelDetail.textContent = `Cleaning ${folder}...`;
                const res = await api.fetchApi("/RunOnRunpod/clean", {
                    method: "POST",
                    body: JSON.stringify({ folder, settings: getSettings() }),
                });
                const data = await res.json();
                if (data.error) {
                    panelDetail.textContent = `Clean failed: ${data.error}`;
                } else {
                    panelDetail.textContent = `Deleted ${data.deleted} file(s) from ${folder}/`;
                }
            } catch (err) {
                panelDetail.textContent = `Clean failed: ${err}`;
            }
        }

        cleanInputsBtn.addEventListener("click", () => cleanFolder("inputs"));
        cleanOutputsBtn.addEventListener("click", () => cleanFolder("outputs"));

        // --- State management ---
        function setState(state) {
            currentState = state;
            const busy = state !== STATE.IDLE;

            runBtn.disabled = busy;
            cancelBtn.disabled = !busy;
            cleanInputsBtn.disabled = busy;
            cleanOutputsBtn.disabled = busy;
        }

        // --- Download outputs and cleanup ---
        async function handleJobEnd(outputFiles) {
            const s = getSettings();
            try {
                const res = await api.fetchApi("/RunOnRunpod/download", {
                    method: "POST",
                    body: JSON.stringify({
                        output_files: outputFiles,
                        input_files: currentInputFiles,
                        delete_inputs: s.deleteInputsAfterJob,
                        delete_outputs: s.deleteOutputsAfterJob,
                        settings: s,
                    }),
                });
                const data = await res.json();
                if (data.downloaded && data.downloaded.length > 0) {
                    console.log(`[RunOnRunpod] Downloaded ${data.downloaded.length} file(s) to local output directory`);
                }
                return data.downloaded || [];
            } catch (err) {
                console.error("[RunOnRunpod] Download/cleanup error:", err);
                return [];
            }
        }

        // --- Polling ---
        function startPolling(jobId) {
            setState(STATE.QUEUED);
            showPanel("Queued on RunPod...");
            pollInterval = setInterval(async () => {
                try {
                    const res = await api.fetchApi("/RunOnRunpod/status", {
                        method: "POST",
                        body: JSON.stringify({ job_id: jobId, settings: getSettings() }),
                    });
                    const data = await res.json();

                    if (data.status === "IN_PROGRESS") {
                        setState(STATE.RUNNING);
                        showPanel("Running...");
                    } else if (data.status === "COMPLETED") {
                        stopPolling();
                        console.log(`[RunOnRunpod] Job completed, output:`, data.output);
                        const outputFiles = data.output?.output_files || [];
                        const downloaded = await handleJobEnd(outputFiles);
                        showPanel("Completed", "", downloaded);
                        setState(STATE.IDLE);
                    } else if (
                        data.status === "FAILED" ||
                        data.status === "CANCELLED" ||
                        data.status === "TIMED_OUT" ||
                        data.status === "UNKNOWN"
                    ) {
                        stopPolling();
                        const errorMsg = data.error || data.output?.error || `Job ${data.status}`;
                        console.error(`[RunOnRunpod] ${errorMsg}`);
                        await handleJobEnd([]);
                        showPanel("Failed", errorMsg);
                        setState(STATE.IDLE);
                    }
                } catch (err) {
                    console.error("[RunOnRunpod] Polling error:", err);
                    stopPolling();
                    showPanel("Failed", "Polling error");
                    setState(STATE.IDLE);
                }
            }, 2000);
        }

        function stopPolling() {
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
            currentJobId = null;
            currentInputFiles = {};
            sessionStorage.removeItem("runpod_job_id");
            sessionStorage.removeItem("runpod_input_files");
        }

        // --- Submit job ---
        async function submitJob() {
            if (currentState !== STATE.IDLE) return;

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
                showPanel("Failed", `Missing settings: ${missing.join(", ")}`);
                return;
            }

            setState(STATE.PREPARING);
            showPanel("Preparing...", "Uploading models and inputs");

            try {
                const prompt = await app.graphToPrompt();

                const res = await api.fetchApi("/RunOnRunpod/submit", {
                    method: "POST",
                    body: JSON.stringify({ workflow: prompt.output, settings: getSettings() }),
                });
                const data = await res.json();

                if (data.error) {
                    console.error("[RunOnRunpod] Submit error:", data.error);
                    showPanel("Failed", data.error);
                    setState(STATE.IDLE);
                    return;
                }

                currentJobId = data.job_id;
                currentInputFiles = data.input_files || {};
                sessionStorage.setItem("runpod_job_id", currentJobId);
                sessionStorage.setItem("runpod_input_files", JSON.stringify(currentInputFiles));
                startPolling(currentJobId);
            } catch (err) {
                console.error("[RunOnRunpod] Submit error:", err);
                showPanel("Failed", String(err));
                setState(STATE.IDLE);
            }
        }

        // --- Cancel job ---
        async function cancelJob() {
            if (!currentJobId) return;
            try {
                await api.fetchApi("/RunOnRunpod/cancel", {
                    method: "POST",
                    body: JSON.stringify({ job_id: currentJobId, settings: getSettings() }),
                });
            } catch (err) {
                console.error("[RunOnRunpod] Cancel error:", err);
            }
            stopPolling();
            showPanel("Cancelled");
            setState(STATE.IDLE);
        }

        runBtn.addEventListener("click", submitJob);
        cancelBtn.addEventListener("click", cancelJob);

        // --- Resume polling on page reload ---
        const savedJobId = sessionStorage.getItem("runpod_job_id");
        if (savedJobId) {
            currentJobId = savedJobId;
            try {
                currentInputFiles = JSON.parse(sessionStorage.getItem("runpod_input_files") || "{}");
            } catch {
                currentInputFiles = {};
            }
            startPolling(savedJobId);
        }

        // --- Insert panel into menu ---
        const insertPanel = () => {
            const actionbar = document.querySelector(".actionbar-container");
            if (actionbar) {
                actionbar.appendChild(wrapper);
                return true;
            }
            return false;
        };

        if (!insertPanel()) {
            const observer = new MutationObserver(() => {
                if (insertPanel()) {
                    observer.disconnect();
                }
            });
            observer.observe(document.body, { childList: true, subtree: true });
        }

        console.log("[RunOnRunpod] Extension loaded");
    },
});
