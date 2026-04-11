import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// --- State machine ---
const STATE = {
    IDLE: "idle",
    QUEUED: "queued",
    RUNNING: "running",
    COMPLETED: "completed",
    FAILED: "failed",
};

let currentState = STATE.IDLE;
let currentJobId = null;
let currentInputFiles = {};
let pollInterval = null;

app.registerExtension({
    name: "RunOnRunpod",

    settings: [
        {
            id: "Run on Runpod.Runpod.deleteOutputsAfterJob",
            name: "Delete outputs from network volume after job",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "Run on Runpod.Runpod.deleteInputsAfterJob",
            name: "Delete inputs from network volume after job",
            type: "boolean",
            defaultValue: false,
        },
        {
            id: "Run on Runpod.Runpod.apiKey",
            name: "API Key",
            type: "text",
            defaultValue: "",
            attrs: { type: "password" },
        },
        {
            id: "Run on Runpod.Runpod.s3SecretKey",
            name: "S3 Secret Key",
            type: "text",
            defaultValue: "",
            attrs: { type: "password" },
        },
        {
            id: "Run on Runpod.Runpod.s3AccessKey",
            name: "S3 Access Key",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Runpod.endpointId",
            name: "Endpoint ID",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Runpod.bucketName",
            name: "Bucket Name",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Runpod.s3Endpoint",
            name: "S3 Endpoint URL",
            type: "text",
            defaultValue: "",
        },
    ],

    async setup() {

        // --- Helper to gather current settings ---
        function getSettings() {
            return {
                apiKey: app.extensionManager.setting.get("Run on Runpod.Runpod.apiKey") || "",
                endpointId: app.extensionManager.setting.get("Run on Runpod.Runpod.endpointId") || "",
                s3AccessKey: app.extensionManager.setting.get("Run on Runpod.Runpod.s3AccessKey") || "",
                s3SecretKey: app.extensionManager.setting.get("Run on Runpod.Runpod.s3SecretKey") || "",
                s3Endpoint: app.extensionManager.setting.get("Run on Runpod.Runpod.s3Endpoint") || "",
                bucketName: app.extensionManager.setting.get("Run on Runpod.Runpod.bucketName") || "",
                deleteInputsAfterJob: app.extensionManager.setting.get("Run on Runpod.Runpod.deleteInputsAfterJob") ?? false,
                deleteOutputsAfterJob: app.extensionManager.setting.get("Run on Runpod.Runpod.deleteOutputsAfterJob") ?? true,
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
            .runpod-btn.queued {
                background: #f0ad4e;
                color: #000;
            }
            .runpod-btn.running {
                background: #337ab7;
                color: #fff;
                animation: runpod-pulse 1.5s ease-in-out infinite;
            }
            .runpod-btn.completed {
                background: #5cb85c;
                color: #fff;
            }
            .runpod-btn.failed {
                background: #d9534f;
                color: #fff;
            }
            @keyframes runpod-pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.6; }
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

        // --- State management ---
        function setState(state) {
            currentState = state;
            btn.classList.remove("queued", "running", "completed", "failed");

            switch (state) {
                case STATE.QUEUED:
                    btn.classList.add("queued");
                    break;
                case STATE.RUNNING:
                    btn.classList.add("running");
                    break;
                case STATE.COMPLETED:
                    btn.classList.add("completed");
                    setTimeout(() => setState(STATE.IDLE), 3000);
                    break;
                case STATE.FAILED:
                    btn.classList.add("failed");
                    setTimeout(() => setState(STATE.IDLE), 3000);
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
            } catch (err) {
                console.error("[RunOnRunpod] Download/cleanup error:", err);
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
                        const outputFiles = data.output?.output_files || [];
                        console.log(`[RunOnRunpod] Job completed — ${outputFiles.length} output(s)`);
                        await handleJobEnd(outputFiles);
                        setState(STATE.COMPLETED);
                    } else if (
                        data.status === "FAILED" ||
                        data.status === "CANCELLED" ||
                        data.status === "TIMED_OUT" ||
                        data.status === "UNKNOWN"
                    ) {
                        stopPolling();
                        console.error(`[RunOnRunpod] Job ${data.status}:`, data.error || "");
                        await handleJobEnd([]);
                        setState(STATE.FAILED);
                    }
                } catch (err) {
                    console.error("[RunOnRunpod] Polling error:", err);
                    stopPolling();
                    setState(STATE.FAILED);
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
                if (!s.s3Endpoint) missing.push("S3 Endpoint URL");
                if (!s.bucketName) missing.push("Bucket Name");
                if (missing.length > 0) {
                    console.error("[RunOnRunpod] Missing settings:", missing.join(", "));
                    setState(STATE.FAILED);
                    return;
                }

                // Immediately show we're working
                setState(STATE.QUEUED);

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
                        setState(STATE.FAILED);
                        return;
                    }

                    currentJobId = data.job_id;
                    currentInputFiles = data.input_files || {};
                    sessionStorage.setItem("runpod_job_id", currentJobId);
                    sessionStorage.setItem("runpod_input_files", JSON.stringify(currentInputFiles));
                    startPolling(currentJobId);
                } catch (err) {
                    console.error("[RunOnRunpod] Submit error:", err);
                    setState(STATE.FAILED);
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
