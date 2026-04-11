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
            .runpod-btn-wrapper {
                display: flex;
                align-items: center;
                gap: 4px;
                height: 100%;
                position: relative;
            }
            .runpod-btn {
                padding: 4px 12px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 300;
                font-family: Inter, sans-serif;
                background: #4a9a5c;
                color: #fff;
                transition: background 0.3s;
                height: 32px;
                display: inline-flex;
                align-items: center;
            }
            .runpod-btn:hover {
                opacity: 0.85;
            }
            .runpod-btn.preparing {
                background: #17a2b8;
                color: #fff;
                animation: runpod-pulse 1.5s ease-in-out infinite;
            }
            .runpod-btn.queued {
                background: #f0ad4e;
                color: #000;
            }
            .runpod-btn.running {
                background: #337ab7;
                color: #fff;
                animation: runpod-pulse 1.5s ease-in-out infinite;
            }
            @keyframes runpod-pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.6; }
            }
            .runpod-panel {
                display: none;
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
            .runpod-panel.open {
                display: block;
            }
            .runpod-panel-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 8px 12px;
                border-bottom: 1px solid #333;
            }
            .runpod-panel-title {
                font-weight: 600;
            }
            .runpod-panel-close {
                background: none;
                border: none;
                color: #888;
                cursor: pointer;
                font-size: 16px;
                padding: 0 4px;
            }
            .runpod-panel-close:hover {
                color: #fff;
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

        // --- Create button ---
        const wrapper = document.createElement("div");
        wrapper.className = "runpod-btn-wrapper";

        const btn = document.createElement("button");
        btn.textContent = "Run on RunPod";
        btn.className = "runpod-btn";
        btn.title = "Run on RunPod";

        wrapper.appendChild(btn);

        // --- Create status panel ---
        const panel = document.createElement("div");
        panel.className = "runpod-panel";

        const panelHeader = document.createElement("div");
        panelHeader.className = "runpod-panel-header";

        const panelTitle = document.createElement("span");
        panelTitle.className = "runpod-panel-title";
        panelTitle.textContent = "Run on RunPod";

        const panelClose = document.createElement("button");
        panelClose.className = "runpod-panel-close";
        panelClose.textContent = "\u00d7";
        panelClose.addEventListener("click", (e) => {
            e.stopPropagation();
            panel.classList.remove("open");
        });

        panelHeader.appendChild(panelTitle);
        panelHeader.appendChild(panelClose);

        const panelDetail = document.createElement("div");
        panelDetail.className = "runpod-panel-detail";

        const panelGallery = document.createElement("div");
        panelGallery.className = "runpod-panel-gallery";

        panel.appendChild(panelHeader);
        panel.appendChild(panelDetail);
        panel.appendChild(panelGallery);
        wrapper.appendChild(panel);

        // Close panel on click outside
        document.addEventListener("click", (e) => {
            if (!wrapper.contains(e.target)) {
                panel.classList.remove("open");
            }
        });

        function showPanel(statusText, detailText = "", gallery = []) {
            panelDetail.textContent = detailText ? `${statusText}: ${detailText}` : statusText;
            panelGallery.innerHTML = "";
            for (const relPath of gallery) {
                const parts = relPath.split("/");
                const filename = parts.pop();
                const subfolder = parts.join("/");
                const img = document.createElement("img");
                img.src = `/view?filename=${encodeURIComponent(filename)}&subfolder=${encodeURIComponent(subfolder)}&type=output`;
                panelGallery.appendChild(img);
            }
            panel.classList.add("open");
        }

        // --- State management ---
        function setState(state) {
            currentState = state;
            btn.classList.remove("preparing", "queued", "running");

            switch (state) {
                case STATE.IDLE:
                    break;
                case STATE.PREPARING:
                    btn.classList.add("preparing");
                    showPanel("Preparing...", "Uploading models and inputs");
                    break;
                case STATE.QUEUED:
                    btn.classList.add("queued");
                    showPanel("Queued on RunPod...");
                    break;
                case STATE.RUNNING:
                    btn.classList.add("running");
                    showPanel("Running...");
                    break;
            }
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
            pollInterval = setInterval(async () => {
                try {
                    const res = await api.fetchApi("/RunOnRunpod/status", {
                        method: "POST",
                        body: JSON.stringify({ job_id: jobId, settings: getSettings() }),
                    });
                    const data = await res.json();

                    if (data.status === "IN_PROGRESS") {
                        setState(STATE.RUNNING);
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

        // --- Click handler: submit or cancel ---
        btn.addEventListener("click", async () => {
            if (currentState === STATE.IDLE) {
                // Quick local check first
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
                    setState(STATE.IDLE);
                    return;
                }

                // Immediately show we're preparing
                setState(STATE.PREPARING);

                // Submit
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
            } else if (currentState === STATE.QUEUED || currentState === STATE.RUNNING) {
                // Cancel
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
                setState(STATE.IDLE);
            }
        });

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

        // --- Insert button into menu ---
        const insertButton = () => {
            const actionbar = document.querySelector(".actionbar-container");
            if (actionbar) {
                actionbar.appendChild(wrapper);
                return true;
            }
            return false;
        };

        if (!insertButton()) {
            const observer = new MutationObserver(() => {
                if (insertButton()) {
                    observer.disconnect();
                }
            });
            observer.observe(document.body, { childList: true, subtree: true });
        }

        console.log("[RunOnRunpod] Extension loaded");
    },
});
