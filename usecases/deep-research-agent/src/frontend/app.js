// ── DOM refs ─────────────────────────────────────────────────
const queryInput      = document.getElementById("query-input");
const researchBtn     = document.getElementById("research-btn");
const progressSection = document.getElementById("progress-section");
const progressBar     = document.getElementById("progress-bar");
const progressStatus  = document.getElementById("progress-status");
const progressLog     = document.getElementById("progress-log");
const errorSection    = document.getElementById("error-section");
const errorMessage    = document.getElementById("error-message");
const reportSection   = document.getElementById("report-section");
const reportContent   = document.getElementById("report-content");
const inputSection    = document.getElementById("input-section");
const sessionList     = document.getElementById("session-list");
const activeQueryEl   = document.getElementById("active-query");
const logToggleIcon   = document.getElementById("log-toggle-icon");

// ── Multi-user client identity ────────────────────────────────
// Each browser gets a persistent UUID stored in localStorage so that
// each user's session history is independent of other users' sessions.
function getClientId() {
    const KEY = "deep_research_client_id";
    let id = localStorage.getItem(KEY);
    if (!id) {
        // Generate a UUID v4 without the crypto API for broad compat
        id = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
        });
        localStorage.setItem(KEY, id);
    }
    return id;
}

// Convenience: build headers object that includes the client ID and JSON content type
function apiHeaders(extra) {
    return Object.assign({ "Content-Type": "application/json", "X-Client-ID": getClientId() }, extra || {});
}

const thinkingContainer = document.getElementById("thinking-container");
const thinkingSpinner   = document.getElementById("thinking-spinner");
const thinkingTitle     = document.getElementById("thinking-title");
const thinkingBody      = document.getElementById("thinking-body");
const thinkingText      = document.getElementById("thinking-text");
const thinkingChevron   = document.getElementById("thinking-chevron");

const agentStatus    = document.getElementById("agent-status");
const agentComponent = document.getElementById("agent-component");
const agentHardware  = document.getElementById("agent-hardware");
const agentModel     = document.getElementById("agent-model");
const agentTps       = document.getElementById("agent-tps");

const hwSummary = document.getElementById("hw-summary");
const stepsList = document.getElementById("steps-list");

// ── Running Bedrock cost ──────────────────────────────────────
const costBarAmount = document.getElementById("cost-bar-amount");
const costBarTokens = document.getElementById("cost-bar-tokens");
const costBarTime   = document.getElementById("cost-bar-time");

// Wall-clock timer for the whole run (end-to-end), shown beside the token counts.
let _wallStart = null;      // performance.now() at run start
let _wallTimer = null;      // setInterval id while running

function _fmtWall(ms) {
    const s = ms / 1000;
    return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${(s % 60).toFixed(0).padStart(2, "0")}s`;
}
function startWallClock() {
    _wallStart = performance.now();
    if (_wallTimer) clearInterval(_wallTimer);
    if (costBarTime) costBarTime.textContent = "⏱ 0s";
    // tick every 5s (final value is snapped exactly on stop)
    _wallTimer = setInterval(() => {
        if (costBarTime && _wallStart != null) costBarTime.textContent = "⏱ " + _fmtWall(performance.now() - _wallStart);
    }, 5000);
}
function stopWallClock() {
    if (_wallTimer) { clearInterval(_wallTimer); _wallTimer = null; }
    if (costBarTime && _wallStart != null) costBarTime.textContent = "⏱ " + _fmtWall(performance.now() - _wallStart);
}
// Claude Sonnet 4 on Bedrock, USD per 1M tokens (no prompt caching in use, so
// input is all full-price → simple in×rate + out×rate is exact).
const BEDROCK_PRICE_IN  = 3.00;
const BEDROCK_PRICE_OUT = 15.00;
// hardware labels that bill to Bedrock (matches the backend hw label)
const BEDROCK_HW = new Set(["bedrock"]);

// Jackpot-style count-up animation state
const COST_ANIM_MS = 500;            // each climb ramps linearly over ~0.5s
let _costShown   = 0;                // value currently rendered
let _costTarget  = 0;                // value we're animating toward
let _costFrom    = 0;                // value the current animation started from
let _costAnimT0  = null;             // rAF timestamp when this animation began
let _costRafId   = null;

// Linear ramp — the value climbs at a constant rate for the full duration,
// so it reads as "counting up throughout the 2-3s" rather than a quick jump
// that then crawls to the finish (which is how an ease-out looks).
function _renderCost(v) {
    costBarAmount.textContent = "$" + v.toFixed(3);
}

function _costTick(ts) {
    if (_costAnimT0 === null) _costAnimT0 = ts;
    const t = Math.min(1, (ts - _costAnimT0) / COST_ANIM_MS);
    _costShown = _costFrom + (_costTarget - _costFrom) * t;   // linear interpolation
    _renderCost(_costShown);
    if (t < 1) {
        _costRafId = requestAnimationFrame(_costTick);
    } else {
        _costShown = _costTarget;
        _renderCost(_costShown);
        _costRafId = null;
        costBarAmount.classList.remove("bump");
    }
}

function _animateCostTo(target) {
    // Only animate upward; a non-increase just snaps (shouldn't normally happen).
    if (target <= _costShown + 1e-9) {
        _costTarget = _costShown = target;
        _renderCost(_costShown);
        return;
    }
    // Re-target from wherever we currently are so rapid SSE ticks stay smooth.
    _costFrom   = _costShown;
    _costTarget = target;
    _costAnimT0 = null;              // reset clock; picked up on next frame
    costBarAmount.classList.add("bump");
    if (_costRafId === null) _costRafId = requestAnimationFrame(_costTick);
}

function resetCostBar() {
    if (_costRafId !== null) { cancelAnimationFrame(_costRafId); _costRafId = null; }
    _costShown = _costTarget = _costFrom = 0;
    _costAnimT0 = null;
    if (costBarAmount) {
        costBarAmount.classList.remove("bump");
        costBarAmount.textContent = "$0.000";
    }
    if (costBarTokens) costBarTokens.textContent = "";
    if (_wallTimer) { clearInterval(_wallTimer); _wallTimer = null; }
    _wallStart = null;
    if (costBarTime) costBarTime.textContent = "";
}

function updateCostBar(summary) {
    if (!costBarAmount || !summary) return;
    let inTok = 0, outTok = 0;
    for (const [hw, s] of Object.entries(summary)) {
        if (BEDROCK_HW.has((hw || "").toLowerCase())) {
            inTok  += s.in_tokens  || 0;
            outTok += s.out_tokens || 0;
        }
    }
    const cost = inTok / 1e6 * BEDROCK_PRICE_IN + outTok / 1e6 * BEDROCK_PRICE_OUT;
    _animateCostTo(cost);            // climb smoothly instead of blinking to the new total
    if (costBarTokens) {
        costBarTokens.textContent = (inTok || outTok)
            ? `↓${inTok.toLocaleString()} ↑${outTok.toLocaleString()} tok`
            : "";
    }
}

// ── Thinking panel state ──────────────────────────────────────
let thinkingActive    = false;
let thinkingExpanded  = true;
let thinkingStartTime = null;
let pendingText       = "";
let rafPending        = false;
let currentIteration  = 0;
let _currentProgress  = 0;    // monotonic — never retreat the bar
let _pendingStepEl    = null;  // last step row not yet finalized with tokens

function showThinkingBox() {
    thinkingContainer.classList.remove("hidden");
    thinkingContainer.classList.add("thinking-active");
    thinkingTitle.textContent = "Thinking\u2026";
    thinkingSpinner.style.display = "inline-block";
    thinkingBody.style.display = "block";
    thinkingChevron.style.transform = "rotate(0deg)";
    thinkingExpanded = true;
}

function finalizeThinking() {
    if (!thinkingActive) return;
    thinkingActive = false;
    // Flush any remaining buffered text
    if (pendingText) { flushThinking(); }
    const elapsed = thinkingStartTime ? ((Date.now() - thinkingStartTime) / 1000).toFixed(1) : "?";
    thinkingTitle.textContent = `Thought for ${elapsed}s`;
    thinkingSpinner.style.display = "none";
    thinkingContainer.classList.remove("thinking-active");
    // Don't collapse — keep box visible so user can scroll history
}

function collapseThinking() {
    thinkingBody.style.display = "none";
    thinkingChevron.style.transform = "rotate(-90deg)";
    thinkingExpanded = false;
}

function expandThinking() {
    thinkingBody.style.display = "block";
    thinkingChevron.style.transform = "rotate(0deg)";
    thinkingExpanded = true;
    thinkingBody.scrollTop = thinkingBody.scrollHeight;
}

function toggleThinking() {
    if (thinkingExpanded) {
        collapseThinking();
    } else {
        expandThinking();
    }
}

function handleThinkingChunk(chunk, hardware, tps, component) {
    if (!thinkingActive) {
        thinkingActive = true;
        thinkingStartTime = Date.now();
        currentIteration++;
        // Append a separator between iterations instead of clearing
        if (thinkingText.textContent) {
            thinkingText.textContent += "\n\n--- " + (component || "Thinking") + " ---\n";
        }
        pendingText = "";
        showThinkingBox();
    }
    pendingText += chunk;
    if (!rafPending) {
        rafPending = true;
        requestAnimationFrame(flushThinking);
    }
    updateAgentStatus(component || "Agent reasoning", hardware, tps);
}

function flushThinking() {
    rafPending = false;
    if (pendingText) {
        thinkingText.textContent += pendingText;
        pendingText = "";
        // Always scroll to bottom so user sees latest tokens
        thinkingBody.scrollTop = thinkingBody.scrollHeight;
    }
}

// Shorten "org/Model-Name-version" → "Model-Name-version", and drop the
// gateway "bedrock-" prefix so e.g. "bedrock-claude-sonnet" → "claude-sonnet".
function shortModelName(name) {
    if (!name) return "";
    const slash = name.lastIndexOf("/");
    let short = slash >= 0 ? name.slice(slash + 1) : name;
    short = short.replace(/^bedrock-/i, "");
    return short;
}

// Display label for a hardware key. Currently a passthrough (the backend already
// labels the cloud slot "Bedrock"); kept as a hook for future relabeling.
function hwDisplay(hw) {
    return hw;
}

// ── Agent status label (removed — steps list shows current active state) ─────
function updateAgentStatus(component, hardware, tps, modelName) {}
function clearAgentStatus() {}

// ── Activity log toggle ───────────────────────────────────────
let _logVisible = false;
function toggleLog() {
    _logVisible = !_logVisible;
    progressLog.classList.toggle("hidden", !_logVisible);
    if (logToggleIcon) logToggleIcon.innerHTML = _logVisible ? "&#9650;" : "&#9660;";
    if (_logVisible) progressLog.scrollTop = progressLog.scrollHeight;
}

// ── Hardware summary line ─────────────────────────────────────
function showHwSummary(summary) {
    if (!summary || !Object.keys(summary).length) return;
    const lines = Object.entries(summary).map(([hw, s]) => {
        const inFmt  = s.in_tokens.toLocaleString();
        const outFmt = s.out_tokens.toLocaleString();
        const tps    = s.tps > 0 ? `${s.tps} tok/s` : "";
        const time   = s.elapsed_s > 0 ? `${s.elapsed_s}s` : "";
        const e2e     = s.tps > 0 ? `e2e ${s.tps} tok/s` : "";
        const prefill = s.prefill_tps != null ? `prefill ${s.prefill_tps} tok/s` : "";
        // decode_tps from ITL if available; else fall back to overall tps (structured calls)
        const decodeTps = s.decode_tps != null ? s.decode_tps : (s.tps > 0 ? s.tps : null);
        const decode  = decodeTps != null ? `decode ${decodeTps} tok/s` : "";
        const stats  = [time, e2e, prefill, decode].filter(Boolean).join(" \u00b7 ");
        const modelPart = s.model ? `<span class="hw-model">${escapeHtml(shortModelName(s.model))}</span>` : "";
        return `<div class="hw-entry"><span class="hw-label">${escapeHtml(hwDisplay(hw))}</span>${modelPart}`
             + `\u2193${inFmt} \u2191${outFmt} tok`
             + (stats ? `&ensp;\u00b7&ensp;${escapeHtml(stats)}` : "")
             + "</div>";
    });
    hwSummary.innerHTML = lines.join("");
    hwSummary.classList.remove("hidden");
    updateCostBar(summary);
}

// ── Supervisor banner ─────────────────────────────────────────
// A persistent first row in the steps list that pulses while the supervisor
// is working and accumulates its own token count. Deliberately NOT part of the
// step_start/step_done flow, so the sub-agent list doesn't grow per iteration.
let _supervisorBanner = null;
let _supInTok  = 0;
let _supOutTok = 0;

function _ensureSupervisorBanner(hw, model) {
    if (_supervisorBanner) return _supervisorBanner;
    stepsList.classList.remove("hidden");
    const el = document.createElement("div");
    el.className = "step-row supervisor-banner";
    const badge = hw
        ? `<span class="step-hw-badge ${escapeHtml(hw.toLowerCase())}">${escapeHtml(hwDisplay(hw))}</span>`
        : `<span class="step-hw-badge"></span>`;
    el.innerHTML =
        `<span class="step-bullet"></span>` +
        `<span class="step-component">Supervisor agent</span>` +
        badge +
        `<span class="step-model">${model ? escapeHtml(shortModelName(model)) : ""}</span>` +
        `<span class="step-tokens">—</span>`;
    // Always the first item in the list.
    stepsList.insertBefore(el, stepsList.firstChild);
    _supervisorBanner = el;
    return el;
}

function supervisorCallStart(hw, model) {
    const el = _ensureSupervisorBanner(hw, model);
    el.classList.add("step-running");   // pulse the bullet while thinking
}

function supervisorCallDone(hw, model, inTok, outTok) {
    const el = _ensureSupervisorBanner(hw, model);
    el.classList.remove("step-running");
    _supInTok  += inTok  || 0;
    _supOutTok += outTok || 0;
    if (hw) {
        const badgeEl = el.querySelector(".step-hw-badge");
        if (badgeEl) { badgeEl.textContent = hwDisplay(hw); badgeEl.className = "step-hw-badge " + hw.toLowerCase(); }
    }
    if (model) {
        const modelEl = el.querySelector(".step-model");
        if (modelEl) modelEl.textContent = shortModelName(model);
    }
    const tokEl = el.querySelector(".step-tokens");
    if (tokEl) {
        tokEl.textContent = (_supInTok || _supOutTok)
            ? `${_supInTok.toLocaleString()}↗${_supOutTok.toLocaleString()}`
            : "—";
    }
}

function resetSupervisorBanner() {
    _supervisorBanner = null;
    _supInTok = 0;
    _supOutTok = 0;
}

// ── Step list ───────────────────────────────────────────────
function appendStepRow(component, hw) {
    stepsList.classList.remove("hidden");
    const el = document.createElement("div");
    el.className = "step-row step-running";
    // Highlight the Xeon summarization step in green (matches "complete").
    if ((component || "").toLowerCase().includes("summariz")) el.classList.add("step-summarize");
    const hwHtml = hw
        ? `<span class="step-hw-badge ${escapeHtml(hw.toLowerCase())}">${escapeHtml(hwDisplay(hw))}</span>`
        : `<span class="step-hw"></span>`;
    el.innerHTML =
        `<span class="step-bullet"></span>` +
        `<span class="step-component">${escapeHtml(component)}</span>` +
        hwHtml +
        `<span class="step-model"></span>` +
        `<span class="step-tokens"></span>`;
    stepsList.appendChild(el);
    _pendingStepEl = el;
}

function finalizeStepRow(el, hw, model, inTok, outTok) {
    if (!el) return;
    el.classList.remove("step-running");
    const hwEl    = el.querySelector(".step-hw");
    const modelEl = el.querySelector(".step-model");
    const tokEl   = el.querySelector(".step-tokens");
    if (hw && hwEl) {
        hwEl.textContent = hwDisplay(hw);
        hwEl.className = "step-hw-badge " + hw.toLowerCase();
    }
    if (model && modelEl) {
        modelEl.textContent = shortModelName(model);
        modelEl.className = "step-model";
    }
    if (tokEl) {
        tokEl.textContent = (inTok || outTok) ? `${inTok}\u2197${outTok}` : "\u2014";
    }
}

// ── Helpers ───────────────────────────────────────────────────
function formatTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleTimeString();
}

function formatDate(isoString) {
    const d = new Date(isoString);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function addLogEntry(message, detail, timestamp) {
    if (!message || !message.trim()) return;
    const entry = document.createElement("div");
    entry.className = "log-entry";
    let html = `<span class="log-time">${formatTime(timestamp)}</span><span class="log-message">${escapeHtml(message)}</span>`;
    if (detail) {
        html += `<div class="log-detail">${escapeHtml(detail)}</div>`;
    }
    entry.innerHTML = html;
    progressLog.appendChild(entry);
    progressLog.scrollTop = progressLog.scrollHeight;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function setStatus(status) {
    progressStatus.textContent = status;
    progressStatus.className = `status-badge status-${status}`;
}

function showSection(section) { section.classList.remove("hidden"); }
function hideSection(section) { section.classList.add("hidden"); }

// ── Session history ───────────────────────────────────────────
async function clearHistory() {
    if (!confirm("Delete all session history? This cannot be undone.")) return;
    await fetch(`/api/sessions?client_id=${encodeURIComponent(getClientId())}`, {
        method: "DELETE",
        headers: apiHeaders(),
    });
    await loadSessionHistory();
}

async function loadSessionHistory() {
    try {
        const resp = await fetch(`/api/sessions?limit=30&client_id=${encodeURIComponent(getClientId())}`, {
            headers: { "X-Client-ID": getClientId() },
        });
        if (!resp.ok) return;
        const sessions = await resp.json();
        renderSessionList(sessions);
    } catch (err) {
        console.error("Failed to load sessions:", err);
    }
}

function renderSessionList(sessions) {
    if (!sessions.length) {
        sessionList.innerHTML = '<div class="session-empty">No past sessions</div>';
        return;
    }
    sessionList.innerHTML = sessions.map(s => `
        <div class="session-item session-${s.status}" onclick="loadSession('${s.id}')">
            <div class="session-query">${escapeHtml(s.query.substring(0, 80))}${s.query.length > 80 ? "…" : ""}</div>
            <div class="session-meta">
                <span class="session-status-dot status-${s.status}"></span>
                <span>${formatDate(s.created_at)}</span>
            </div>
        </div>
    `).join("");
}

async function loadSession(sessionId) {
    try {
        const resp = await fetch(`/api/research/${sessionId}`);
        if (!resp.ok) return;
        const session = await resp.json();

        hideSection(inputSection);
        hideSection(progressSection);
        hideSection(errorSection);
        hideSection(reportSection);

        if (session.status === "complete" && session.report_markdown) {
            showSection(reportSection);
            reportContent.innerHTML = marked.parse(session.report_markdown);
        } else if (session.status === "failed" && session.error) {
            showSection(errorSection);
            errorMessage.textContent = session.error;
        } else {
            showSection(inputSection);
            queryInput.value = session.query;
        }
    } catch (err) {
        console.error("Failed to load session:", err);
    }
}

function showNewResearch() {
    hideSection(progressSection);
    hideSection(errorSection);
    hideSection(reportSection);
    showSection(inputSection);
    queryInput.value = "";
    queryInput.focus();
}

// ── Research flow ─────────────────────────────────────────────
async function startResearch() {
    const query = queryInput.value.trim();
    if (!query) return;

    // Reset UI
    researchBtn.disabled = true;
    progressLog.innerHTML = "";
    progressBar.style.width = "0%";
    _currentProgress = 0;
    thinkingText.textContent = "";
    thinkingContainer.classList.add("hidden");
    thinkingActive = false;
    currentIteration = 0;
    hwSummary.classList.add("hidden");
    resetCostBar();
    clearAgentStatus();
    _logVisible = false;
    progressLog.classList.add("hidden");
    if (logToggleIcon) logToggleIcon.innerHTML = "&#9660;";
    stepsList.innerHTML = "";
    stepsList.classList.add("hidden");
    _pendingStepEl = null;
    resetSupervisorBanner();
    startWallClock();   // begin end-to-end timer

    // Show query text above status
    activeQueryEl.textContent = query;
    activeQueryEl.classList.remove("hidden");

    hideSection(errorSection);
    hideSection(reportSection);
    hideSection(inputSection);
    showSection(progressSection);
    setStatus("pending");

    try {
        const resp = await fetch("/api/research", {
            method: "POST",
            headers: apiHeaders(),
            body: JSON.stringify({ query }),
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || "Failed to start research");
        }

        const { research_id } = await resp.json();

        const evtSource = new EventSource(`/api/research/${research_id}/stream`);

        evtSource.onmessage = (event) => {
            const data = JSON.parse(event.data);

            // Debug: log step events to console
            if (data.step_start || data.step_done) {
                console.log("[step]", data.step_start ? "START" : "DONE",
                    data.component, "hw=", data.step_hardware,
                    "in=", data.step_input_tokens, "out=", data.step_output_tokens,
                    "pendingEl=", _pendingStepEl ? "set" : "null");
            }

            // Always update hw_summary if the event carries it
            if (data.hw_summary && Object.keys(data.hw_summary).length > 0) {
                showHwSummary(data.hw_summary);
            }

            // Supervisor banner — persistent top row, pulses while it works
            if (data.supervisor_call_start) {
                supervisorCallStart(data.hardware, data.model_name);
            }
            if (data.supervisor_call_done) {
                supervisorCallDone(data.hardware, data.model_name,
                                   data.step_input_tokens, data.step_output_tokens);
            }

            // Thinking token — fast path
            if (data.thinking_chunk) {
                handleThinkingChunk(data.thinking_chunk, data.hardware, data.tps, data.component);
                if (data.model_name && !agentModel.textContent) {
                    agentModel.textContent = shortModelName(data.model_name);
                    agentModel.style.display = "inline";
                }
                return;
            }

            // Non-thinking event: finalize active thinking session if any
            if (thinkingActive) {
                finalizeThinking();
            }

            setStatus(data.status);
            if (data.progress_pct > _currentProgress) {
                _currentProgress = data.progress_pct;
                progressBar.style.width = `${_currentProgress}%`;
            }

            if (data.message) {
                addLogEntry(data.message, data.detail, data.timestamp);
            }

            if (data.step_done) {
                // Tool step completed — finalize the row with hw, model, tokens
                finalizeStepRow(_pendingStepEl, data.step_hardware, data.step_model,
                                data.step_input_tokens, data.step_output_tokens);
                _pendingStepEl = null;
            } else if (data.step_start) {
                // New tool step starting — append a row with hw badge already visible
                appendStepRow(data.component, data.step_hardware);
            }

            if (data.component || data.hardware) {
                updateAgentStatus(data.component, data.hardware, data.tps, data.model_name);
            }

            if (data.status === "complete") {
                evtSource.close();
                stopWallClock();
                clearAgentStatus();
                fetchReport(research_id);
                researchBtn.disabled = false;
                loadSessionHistory();
            }

            if (data.status === "failed") {
                evtSource.close();
                stopWallClock();
                clearAgentStatus();
                showSection(errorSection);
                errorMessage.textContent = data.message;
                researchBtn.disabled = false;
                loadSessionHistory();
            }
        };

        evtSource.onerror = () => {
            evtSource.close();
            stopWallClock();
            if (thinkingActive) finalizeThinking();
            clearAgentStatus();
            fetchReport(research_id);
            researchBtn.disabled = false;
        };

    } catch (err) {
        showSection(errorSection);
        showSection(inputSection);
        hideSection(progressSection);
        errorMessage.textContent = err.message;
        researchBtn.disabled = false;
    }
}

async function fetchReport(researchId) {
    try {
        const resp = await fetch(`/api/research/${researchId}`);
        if (!resp.ok) {
            // Server restarted and lost the session — show a clear message
            addLogEntry("Session lost (server restarted). Please run the query again.", "", new Date().toISOString());
            showSection(errorSection);
            errorMessage.textContent = "The server restarted while your query was running. Please try again.";
            return;
        }
        const session = await resp.json();

        if (session.status === "complete" && session.report_markdown) {
            showSection(reportSection);
            reportContent.innerHTML = marked.parse(session.report_markdown);
        }

        if (session.status === "failed" && session.error) {
            showSection(errorSection);
            errorMessage.textContent = session.error;
        }
    } catch (err) {
        console.error("Failed to fetch report:", err);
    }
}

// ── Keyboard shortcuts ────────────────────────────────────────
queryInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        startResearch();
    }
});

// ── Init ──────────────────────────────────────────────────────
loadSessionHistory();

// ── vLLM status lights ────────────────────────────────────────
async function pollVllmStatus() {
    try {
        const data = await fetch("/api/system/status").then(r => r.json());
        const container = document.getElementById("vllm-status");
        if (!container) return;
        container.innerHTML = data.services.map(svc => {
            const cls = svc.status === "up" ? "up" : "down";
            const tip = svc.status === "up" ? svc.name + " healthy" : (svc.detail || svc.name + " unreachable");
            return `<span class="vllm-chip" title="${tip}">
                <span class="vllm-light ${cls}"></span>${svc.name}</span>`;
        }).join("");
    } catch (_) { /* silently ignore network errors */ }
}
pollVllmStatus();
setInterval(pollVllmStatus, 15000);
