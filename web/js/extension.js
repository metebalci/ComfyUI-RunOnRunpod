import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const S3_PROVIDER_ENDPOINTS = {
    aws: "",
    r2: "https://{account_id}.r2.cloudflarestorage.com",
    gcs: "https://storage.googleapis.com",
    runpod: "",
    custom: "",
};

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
let outputGetUrls = {};
let pollInterval = null;

app.registerExtension({
    name: "RunOnRunpod",

    async setup() {
        // --- Register settings ---
        const category = ["RunOnRunpod", "Settings"];
        app.ui.settings.addSetting({
            id: "RunOnRunpod.apiKey",
            name: "RunPod API Key",
            type: "string",
            defaultValue: "",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.endpointId",
            name: "RunPod Endpoint ID",
            type: "string",
            defaultValue: "",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3Provider",
            name: "S3 Provider (aws / r2 / gcs / runpod / custom)",
            type: "string",
            defaultValue: "aws",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3Endpoint",
            name: "S3 Endpoint",
            type: "string",
            defaultValue: "",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3AccessKey",
            name: "S3 Access Key",
            type: "string",
            defaultValue: "",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3SecretKey",
            name: "S3 Secret Key",
            type: "string",
            defaultValue: "",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3Bucket",
            name: "S3 Bucket",
            type: "string",
            defaultValue: "",
            category,
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.maxOutputUrls",
            name: "Max Output URLs per Job",
            type: "number",
            defaultValue: 5,
            attrs: { min: 1, max: 50, step: 1 },
            category,
        });

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
                bottom: 20px;
                right: 20px;
                background: #333;
                color: #fff;
                padding: 12px 16px;
                border-radius: 8px;
                max-width: 400px;
                z-index: 10000;
                font-size: 13px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }
            .runpod-notification a {
                color: #6cf;
                text-decoration: underline;
                display: block;
                margin-top: 4px;
                word-break: break-all;
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

        // --- Create button elements ---
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
                case STATE.IDLE:
                    btn.textContent = "Run on RunPod";
                    btn.title = "Run on RunPod";
                    break;
                case STATE.QUEUED:
                    btn.textContent = "Queued...";
                    btn.title = "Click to cancel";
                    btn.classList.add("queued");
                    break;
                case STATE.RUNNING:
                    btn.textContent = "Running...";
                    btn.title = "Click to cancel";
                    btn.classList.add("running");
                    break;
                case STATE.COMPLETED:
                    btn.textContent = "Completed";
                    btn.title = "Run on RunPod";
                    btn.classList.add("completed");
                    setTimeout(() => setState(STATE.IDLE), 3000);
                    break;
                case STATE.FAILED:
                    btn.textContent = "Failed";
                    btn.title = "Run on RunPod";
                    btn.classList.add("failed");
                    setTimeout(() => setState(STATE.IDLE), 3000);
                    break;
            }
        }

        // --- Notification ---
        function showNotification(urls) {
            // Remove any existing notification
            document.querySelectorAll(".runpod-notification").forEach(el => el.remove());

            const div = document.createElement("div");
            div.className = "runpod-notification";

            const closeBtn = document.createElement("span");
            closeBtn.className = "close-btn";
            closeBtn.textContent = "✕";
            closeBtn.onclick = () => div.remove();
            div.appendChild(closeBtn);

            const title = document.createElement("div");
            title.textContent = `Job completed — ${urls.length} output(s):`;
            title.style.fontWeight = "bold";
            title.style.marginBottom = "4px";
            div.appendChild(title);

            urls.forEach((url, i) => {
                const link = document.createElement("a");
                link.href = url;
                link.target = "_blank";
                link.textContent = `Output ${i + 1}`;
                div.appendChild(link);
            });

            document.body.appendChild(div);

            // Auto-dismiss after 30 seconds
            setTimeout(() => div.remove(), 30000);
        }

        // --- Polling ---
        function startPolling(jobId) {
            setState(STATE.QUEUED);
            pollInterval = setInterval(async () => {
                try {
                    const res = await api.fetchApi(`/RunOnRunpod/status/${jobId}`);
                    const data = await res.json();

                    if (data.status === "IN_PROGRESS") {
                        setState(STATE.RUNNING);
                    } else if (data.status === "COMPLETED") {
                        stopPolling();
                        setState(STATE.COMPLETED);

                        // Show output links
                        const urls = Object.values(outputGetUrls);
                        if (urls.length > 0) {
                            // Filter to only URLs that the worker actually used
                            // For now show all — worker returns which indices it used
                            const usedUrls = data.output?.used_indices
                                ? data.output.used_indices.map(i => outputGetUrls[String(i)])
                                : urls;
                            showNotification(usedUrls);
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
                // Submit
                try {
                    const prompt = await app.graphToPrompt();
                    setState(STATE.QUEUED);

                    const res = await api.fetchApi("/RunOnRunpod/submit", {
                        method: "POST",
                        body: JSON.stringify({ workflow: prompt.output }),
                    });
                    const data = await res.json();

                    if (data.error) {
                        alert(`RunOnRunpod: ${data.error}`);
                        setState(STATE.IDLE);
                        return;
                    }

                    currentJobId = data.job_id;
                    outputGetUrls = data.output_get_urls || {};
                    sessionStorage.setItem("runpod_job_id", currentJobId);
                    startPolling(currentJobId);
                } catch (err) {
                    console.error("[RunOnRunpod] Submit error:", err);
                    alert("RunOnRunpod: Failed to submit job");
                    setState(STATE.IDLE);
                }
            } else if (currentState === STATE.QUEUED || currentState === STATE.RUNNING) {
                // Cancel
                if (!currentJobId) return;
                try {
                    await api.fetchApi(`/RunOnRunpod/cancel/${currentJobId}`, {
                        method: "POST",
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
