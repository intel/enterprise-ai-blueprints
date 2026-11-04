// Split-screen summarization A/B — runs the same query twice (summarize on/off)
// and shows two live panels with a Bedrock cost comparison.

// Claude Sonnet 4 on Bedrock, USD per 1M tokens (no prompt caching → exact).
const BEDROCK_PRICE_IN  = 3.00;
const BEDROCK_PRICE_OUT = 15.00;
const BEDROCK_HW = new Set(["bedrock"]);
const COST_ANIM_MS = 500;

const researchBtn = document.getElementById("research-btn");
const queryInput  = document.getElementById("query-input");
const verdictEl   = document.getElementById("verdict");

function bedrockCost(summary) {
    let inTok = 0, outTok = 0;
    for (const [hw, s] of Object.entries(summary || {})) {
        if (BEDROCK_HW.has((hw || "").toLowerCase())) {
            inTok  += s.in_tokens  || 0;
            outTok += s.out_tokens || 0;
        }
    }
    return { cost: inTok / 1e6 * BEDROCK_PRICE_IN + outTok / 1e6 * BEDROCK_PRICE_OUT, inTok, outTok };
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : text;
    return div.innerHTML;
}

function shortModelName(name) {
    if (!name) return "";
    const slash = name.lastIndexOf("/");
    let short = slash >= 0 ? name.slice(slash + 1) : name;
    return short.replace(/^bedrock-/i, "");
}

// Display label for a hardware key. Currently a passthrough (the backend already
// labels the cloud slot "Bedrock"); kept as a hook for future relabeling.
function hwDisplay(hw) {
    return hw;
}

// ── Panel: encapsulates one run's DOM + animated cost ─────────────
class Panel {
    constructor(rootId) {
        this.root      = document.getElementById(rootId);
        this.costEl    = this.root.querySelector(".panel-cost");
        this.tokensEl  = this.root.querySelector(".panel-tokens");
        this.statusEl  = this.root.querySelector(".panel-status");
        this.progressEl= this.root.querySelector(".panel-progress");
        this.stepsEl   = this.root.querySelector(".panel-steps");
        this.reportEl  = this.root.querySelector(".panel-report");
        this.thinkEl   = this.root.querySelector(".panel-thinking");
        this.thinkText = this.root.querySelector(".panel-think-text");
        this.thinkTitle= this.root.querySelector(".panel-think-title");
        this.thinkSpin = this.root.querySelector(".panel-think-spinner");
        this.finalCost = 0;
        this.reset();
    }

    reset() {
        this.costShown = 0; this.costTarget = 0; this.costFrom = 0;
        this.animT0 = null; this.rafId = null; this.finalCost = 0;
        this.progress = 0;
        this.supRow = null;          // persistent supervisor row
        this.supIn = 0; this.supOut = 0;  // accumulated supervisor tokens
        this.pendingRow = null;      // tool row awaiting its step_done
        this.thinkActive = false;    // whether the thinking box is mid-stream
        this.costEl.textContent = "$0.000";
        this.tokensEl.textContent = "";
        this.stepsEl.innerHTML = "";
        this.stepsEl.classList.add("hidden");
        this.thinkText.textContent = "";
        this.thinkEl.classList.add("hidden");
        this.reportEl.innerHTML = "";
        this.reportEl.classList.add("hidden");
        this.progressEl.style.width = "0%";
        this.setStatus("idle");
    }

    setStatus(s) {
        this.statusEl.textContent = s;
        this.statusEl.className = "panel-status status-badge status-" + s;
    }

    setProgress(pct) {
        if (pct > this.progress) {
            this.progress = pct;
            this.progressEl.style.width = pct + "%";
        }
    }

    _makeRow(component) {
        const row = document.createElement("div");
        row.className = "step-row step-running";
        // Highlight the Xeon summarization step in green (matches "complete").
        if ((component || "").toLowerCase().includes("summariz")) row.classList.add("step-summarize");
        row.innerHTML =
            `<span class="step-bullet"></span>` +
            `<span class="step-component">${escapeHtml(component)}</span>` +
            `<span class="step-hw-badge"></span>` +
            `<span class="step-model"></span>` +
            `<span class="step-tokens"></span>`;
        return row;
    }
    _fill(row, hw, model, inTok, outTok, done) {
        if (hw) {
            const b = row.querySelector(".step-hw-badge");
            b.textContent = hwDisplay(hw); b.className = "step-hw-badge " + hw.toLowerCase();
        }
        if (model) row.querySelector(".step-model").textContent = shortModelName(model);
        if (done) {
            row.classList.remove("step-running");
            const t = row.querySelector(".step-tokens");
            if (inTok || outTok) t.textContent = `${(inTok||0).toLocaleString()}↗${(outTok||0).toLocaleString()}`;
        }
    }

    // Supervisor agent fires every iteration → keep ONE persistent row (like the
    // single view's banner), always first in the list. It ACCUMULATES tokens
    // across all iterations so the row reflects the supervisor's full Bedrock
    // contribution (otherwise ~all-but-the-last iteration is invisible and the
    // per-step numbers don't sum to the cost-bar total).
    supervisorStep(hw, model, inTok, outTok, done) {
        this.stepsEl.classList.remove("hidden");
        if (!this.supRow) {
            this.supRow = this._makeRow("Supervisor agent");
            this.supRow.classList.add("supervisor-banner");
            this.stepsEl.insertBefore(this.supRow, this.stepsEl.firstChild);
            this.supIn = 0; this.supOut = 0;
        }
        if (hw) {
            const b = this.supRow.querySelector(".step-hw-badge");
            b.textContent = hwDisplay(hw); b.className = "step-hw-badge " + hw.toLowerCase();
        }
        if (model) this.supRow.querySelector(".step-model").textContent = shortModelName(model);
        if (done) {
            this.supIn += inTok || 0; this.supOut += outTok || 0;
            this.supRow.querySelector(".step-tokens").textContent =
                `${this.supIn.toLocaleString()}↗${this.supOut.toLocaleString()}`;
            this.supRow.classList.remove("step-running");
        } else {
            this.supRow.classList.add("step-running");
        }
    }

    // Tool steps: APPEND a fresh row per occurrence so both cycles are visible
    // (web_search → evaluate → gap → web_search → evaluate → ...).
    toolStep(component, hw, model, inTok, outTok, done) {
        if (!component) return;
        this.stepsEl.classList.remove("hidden");
        if (!done) {
            // step_start → new row, remembered as pending
            this.pendingRow = this._makeRow(component);
            this.stepsEl.appendChild(this.pendingRow);
            this._fill(this.pendingRow, hw, model, "", "", false);
        } else {
            // step_done → finalize the pending row (or make one if we missed the start)
            const row = this.pendingRow || (() => { const r = this._makeRow(component); this.stepsEl.appendChild(r); return r; })();
            this._fill(row, hw, model, inTok, outTok, true);
            this.pendingRow = null;
        }
    }

    // Animated (jackpot) cost climb, matching the single-view feel.
    animateCost(target) {
        if (target <= this.costShown + 1e-9) { this.costTarget = this.costShown = target; this.render(); return; }
        this.costFrom = this.costShown;
        this.costTarget = target;
        this.animT0 = null;
        this.costEl.classList.add("bump");
        if (this.rafId === null) this.rafId = requestAnimationFrame((ts) => this.tick(ts));
    }
    tick(ts) {
        if (this.animT0 === null) this.animT0 = ts;
        const t = Math.min(1, (ts - this.animT0) / COST_ANIM_MS);
        this.costShown = this.costFrom + (this.costTarget - this.costFrom) * t;
        this.render();
        if (t < 1) { this.rafId = requestAnimationFrame((ts2) => this.tick(ts2)); }
        else { this.costShown = this.costTarget; this.render(); this.rafId = null; this.costEl.classList.remove("bump"); }
    }
    render() { this.costEl.textContent = "$" + this.costShown.toFixed(3); }

    setTokens(inTok, outTok) {
        this.tokensEl.textContent = (inTok || outTok)
            ? `↓${inTok.toLocaleString()} ↑${outTok.toLocaleString()} tok` : "";
    }

    showReport(md) {
        this.reportEl.classList.remove("hidden");
        this.reportEl.innerHTML = md ? marked.parse(md) : "<em>(no report)</em>";
    }

    // Live agent output-token stream — appends supervisor tokens as they arrive,
    // with an "--- <component> ---" separator between iterations.
    thinkingChunk(chunk, component) {
        if (!chunk) return;
        if (!this.thinkActive) {
            this.thinkActive = true;
            this.thinkEl.classList.remove("hidden");
            this.thinkEl.classList.add("thinking-active");
            this.thinkTitle.textContent = "Thinking…";
            this.thinkSpin.style.display = "inline-block";
            if (this.thinkText.textContent) {
                this.thinkText.textContent += "\n\n--- " + (component || "Thinking") + " ---\n";
            }
        }
        this.thinkText.textContent += chunk;
        const body = this.thinkText.parentElement;
        body.scrollTop = body.scrollHeight;   // keep latest tokens in view
    }
    thinkingDone() {
        if (!this.thinkActive) return;
        this.thinkActive = false;
        this.thinkSpin.style.display = "none";
        this.thinkEl.classList.remove("thinking-active");
    }
}

const panelWith    = new Panel("panel-with");
const panelWithout = new Panel("panel-without");

// ── Run one side ──────────────────────────────────────────────
function runSide(query, summarize, panel) {
    return new Promise(async (resolve) => {
        panel.reset();
        panel.setStatus("pending");
        try {
            const resp = await fetch("/api/research", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ query, summarize }),
            });
            if (!resp.ok) throw new Error((await resp.json()).detail || "Failed to start");
            const { research_id } = await resp.json();

            const es = new EventSource(`/api/research/${research_id}/stream`);
            es.onmessage = (event) => {
                const data = JSON.parse(event.data);

                // Live output-token stream (fast path, like the single view).
                if (data.thinking_chunk) {
                    panel.thinkingChunk(data.thinking_chunk, data.component);
                    return;
                }
                // Any non-thinking event ends the current thinking phase.
                panel.thinkingDone();

                if (data.hw_summary && Object.keys(data.hw_summary).length > 0) {
                    const { cost, inTok, outTok } = bedrockCost(data.hw_summary);
                    panel.animateCost(cost);
                    panel.setTokens(inTok, outTok);
                    panel.finalCost = cost;
                }
                if (data.supervisor_call_start) panel.supervisorStep(data.hardware, data.model_name, 0, 0, false);
                if (data.supervisor_call_done)  panel.supervisorStep(data.hardware, data.model_name, data.step_input_tokens, data.step_output_tokens, true);
                // On the OFF side, summarize_sources runs as an instant no-op — hide
                // its step so the panel honestly shows no summarization occurred.
                const isSummarizeStep = (data.component || "").toLowerCase().includes("summariz");
                const suppress = summarize === false && isSummarizeStep;
                if (!suppress) {
                    if (data.step_start) panel.toolStep(data.component, data.step_hardware, data.step_model, 0, 0, false);
                    if (data.step_done)  panel.toolStep(data.component, data.step_hardware, data.step_model, data.step_input_tokens, data.step_output_tokens, true);
                }

                if (data.status) panel.setStatus(data.status);
                if (data.progress_pct) panel.setProgress(data.progress_pct);

                if (data.status === "complete" || data.status === "failed") {
                    es.close();
                    fetch(`/api/research/${research_id}`)
                        .then(r => r.ok ? r.json() : null)
                        .then(s => { if (s && s.report_markdown) panel.showReport(s.report_markdown); })
                        .finally(() => resolve(panel.finalCost));
                }
            };
            es.onerror = () => { es.close(); panel.setStatus("failed"); resolve(panel.finalCost); };
        } catch (err) {
            panel.setStatus("failed");
            panel.reportEl.classList.remove("hidden");
            panel.reportEl.innerHTML = `<em>Error: ${escapeHtml(err.message)}</em>`;
            resolve(0);
        }
    });
}

async function startCompare() {
    const query = queryInput.value.trim();
    if (!query) return;
    researchBtn.disabled = true;
    verdictEl.classList.add("hidden");

    // Launch both concurrently (backend isolates them via ContextVar).
    const [costWith, costWithout] = await Promise.all([
        runSide(query, true,  panelWith),
        runSide(query, false, panelWithout),
    ]);

    // Verdict — dollars shown with 2 decimals ($0.89), percent is the headline.
    const fmt$ = (v) => "$" + v.toFixed(3);
    document.getElementById("verdict-with").textContent    = fmt$(costWith);
    document.getElementById("verdict-without").textContent = fmt$(costWithout);
    const delta = costWithout - costWith;
    const pct = costWithout > 0 ? (delta / costWithout * 100) : 0;
    document.getElementById("verdict-pct").textContent   = costWithout > 0 ? `${pct.toFixed(0)}%` : "—";
    document.getElementById("verdict-delta").textContent = (delta >= 0 ? "" : "-") + fmt$(Math.abs(delta)) + " saved";
    verdictEl.classList.remove("hidden");

    researchBtn.disabled = false;
}

queryInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); startCompare(); }
});
