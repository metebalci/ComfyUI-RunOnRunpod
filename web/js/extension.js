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
let pollInterval = null;

app.registerExtension({
    name: "RunOnRunpod",

    settings: [
        {
            id: "Run on Runpod.Runpod.volumeId",
            name: "Network Volume ID",
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
            id: "Run on Runpod.Runpod.s3SecretKey",
            name: "S3 Secret Key",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Runpod.s3AccessKey",
            name: "S3 Access Key",
            type: "text",
            defaultValue: "",
        },
        {
            id: "Run on Runpod.Runpod.apiKey",
            name: "API Key",
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
                volumeId: app.extensionManager.setting.get("Run on Runpod.Runpod.volumeId") || "",
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
            .runpod-notification {
                position: fixed;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                background: #333;
                color: #fff;
                padding: 16px 24px;
                border-radius: 8px;
                max-width: 500px;
                z-index: 10000;
                font-size: 14px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            }
            .runpod-notification .close-btn {
                position: absolute;
                top: 4px;
                right: 8px;
                cursor: pointer;
                font-size: 16px;
                color: #aaa;
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

        // --- Notification ---
        function showNotification(message, isError = false) {
            document.querySelectorAll(".runpod-notification").forEach(el => el.remove());

            const div = document.createElement("div");
            div.className = "runpod-notification";
            if (isError) div.style.borderLeft = "4px solid #d9534f";

            const closeBtn = document.createElement("span");
            closeBtn.className = "close-btn";
            closeBtn.textContent = "\u2715";
            closeBtn.onclick = () => div.remove();
            div.appendChild(closeBtn);

            const title = document.createElement("div");
            title.textContent = message;
            title.style.fontWeight = "bold";
            div.appendChild(title);

            document.body.appendChild(div);
            setTimeout(() => div.remove(), isError ? 15000 : 10000);
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
                        setState(STATE.COMPLETED);

                        const outputCount = data.output?.output_count || 0;
                        const outputFiles = data.output?.output_files || [];
                        if (outputCount > 0) {
                            showNotification(`Job completed \u2014 ${outputCount} output(s) on network volume`);
                        }
                    } else if (
                        data.status === "FAILED" ||
                        data.status === "CANCELLED" ||
                        data.status === "TIMED_OUT"
                    ) {
                        stopPolling();
                        setState(STATE.FAILED);
                        if (data.error) {
                            console.error("[RunOnRunpod] Job error:", data.error);
                        }
                    }
                } catch (err) {
                    console.error("[RunOnRunpod] Polling error:", err);
                }
            }, 2000);
        }

        function stopPolling() {
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
            currentJobId = null;
            sessionStorage.removeItem("runpod_job_id");
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
                if (!s.volumeId) missing.push("Network Volume ID");
                if (missing.length > 0) {
                    showNotification("Missing settings: " + missing.join(", "), true);
                    return;
                }

                // Submit
                try {
                    const prompt = await app.graphToPrompt();
                    setState(STATE.QUEUED);

                    const res = await api.fetchApi("/RunOnRunpod/submit", {
                        method: "POST",
                        body: JSON.stringify({ workflow: prompt.output, settings: getSettings() }),
                    });
                    const data = await res.json();

                    if (data.error) {
                        showNotification(data.error, true);
                        setState(STATE.IDLE);
                        return;
                    }

                    currentJobId = data.job_id;
                    sessionStorage.setItem("runpod_job_id", currentJobId);
                    startPolling(currentJobId);
                } catch (err) {
                    console.error("[RunOnRunpod] Submit error:", err);
                    showNotification("Failed to submit job", true);
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
