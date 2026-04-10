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
        app.ui.settings.addSetting({
            id: "RunOnRunpod.apiKey",
            name: "RunPod API Key",
            type: "text",
            defaultValue: "",
            category: ["RunOnRunpod", "RunPod"],
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.endpointId",
            name: "Endpoint ID",
            type: "text",
            defaultValue: "",
            category: ["RunOnRunpod", "RunPod"],
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3Provider",
            name: "S3 Provider",
            type: "combo",
            defaultValue: "aws",
            options: [
                { text: "AWS S3", value: "aws" },
                { text: "Cloudflare R2", value: "r2" },
                { text: "Google Cloud Storage", value: "gcs" },
                { text: "RunPod", value: "runpod" },
                { text: "Custom", value: "custom" },
            ],
            category: ["RunOnRunpod", "S3 Storage"],
            onChange: (value) => {
                const endpoint = S3_PROVIDER_ENDPOINTS[value] || "";
                app.ui.settings.setSettingValue("RunOnRunpod.s3Endpoint", endpoint);
            },
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3Endpoint",
            name: "S3 Endpoint",
            type: "text",
            defaultValue: "",
            category: ["RunOnRunpod", "S3 Storage"],
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3AccessKey",
            name: "S3 Access Key",
            type: "text",
            defaultValue: "",
            category: ["RunOnRunpod", "S3 Storage"],
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3SecretKey",
            name: "S3 Secret Key",
            type: "text",
            defaultValue: "",
            category: ["RunOnRunpod", "S3 Storage"],
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.s3Bucket",
            name: "S3 Bucket",
            type: "text",
            defaultValue: "",
            category: ["RunOnRunpod", "S3 Storage"],
        });
        app.ui.settings.addSetting({
            id: "RunOnRunpod.maxOutputUrls",
            name: "Max Output URLs per Job",
            type: "number",
            defaultValue: 5,
            attrs: { min: 1, max: 50, step: 1 },
            category: ["RunOnRunpod", "S3 Storage"],
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
                background: var(--comfy-secondary-background, #333);
                color: var(--comfy-base-foreground, #fff);
                transition: background 0.3s;
                height: 32px;
                display: inline-flex;
                align-items: center;
            }
            .runpod-btn:disabled {
                cursor: not-allowed;
                opacity: 0.9;
            }
            .runpod-btn.queued {
                background: #f0ad4e;
                border-color: #eea236;
                color: #000;
            }
            .runpod-btn.running {
                background: #337ab7;
                border-color: #2e6da4;
                color: #fff;
                animation: runpod-pulse 1.5s ease-in-out infinite;
            }
            .runpod-btn.completed {
                background: #5cb85c;
                border-color: #4cae4c;
                color: #fff;
            }
            .runpod-btn.failed {
                background: #d9534f;
                border-color: #d43f3a;
                color: #fff;
            }
            @keyframes runpod-pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.6; }
            }
            .runpod-cancel {
                padding: 2px 6px;
                border: 1px solid #999;
                border-radius: 3px;
                cursor: pointer;
                font-size: 12px;
                background: #eee;
                color: #333;
                display: none;
            }
            .runpod-cancel.visible {
                display: inline-block;
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

        const cancelBtn = document.createElement("button");
        cancelBtn.textContent = "✕";
        cancelBtn.className = "runpod-cancel";

        wrapper.appendChild(btn);
        wrapper.appendChild(cancelBtn);

        // --- State management ---
        function setState(state) {
            currentState = state;
            btn.classList.remove("queued", "running", "completed", "failed");

            switch (state) {
                case STATE.IDLE:
                    btn.disabled = false;
                    btn.textContent = "Run on RunPod";
                    cancelBtn.classList.remove("visible");
                    break;
                case STATE.QUEUED:
                    btn.disabled = true;
                    btn.textContent = "Queued...";
                    btn.classList.add("queued");
                    cancelBtn.classList.add("visible");
                    break;
                case STATE.RUNNING:
                    btn.disabled = true;
                    btn.textContent = "Running...";
                    btn.classList.add("running");
                    cancelBtn.classList.add("visible");
                    break;
                case STATE.COMPLETED:
                    btn.disabled = true;
                    btn.textContent = "Completed";
                    btn.classList.add("completed");
                    cancelBtn.classList.remove("visible");
                    setTimeout(() => setState(STATE.IDLE), 3000);
                    break;
                case STATE.FAILED:
                    btn.disabled = true;
                    btn.textContent = "Failed";
                    btn.classList.add("failed");
                    cancelBtn.classList.remove("visible");
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

        // --- Submit ---
        btn.addEventListener("click", async () => {
            if (currentState !== STATE.IDLE) return;

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
        });

        // --- Cancel ---
        cancelBtn.addEventListener("click", async () => {
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
