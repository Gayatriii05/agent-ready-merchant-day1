/* Agent-Ready Merchant - frontend logic
   -------------------------------------
   Two responsibilities:
   1. Chat: POST /chat, render bubbles (agent replies can take a few seconds
      because of model round-trips -> show a typing indicator meanwhile).
   2. Live panels: poll /audit-log?after=N and /metrics every ~1.2s.
      Polling (rather than websockets) keeps the demo simple and robust.

   Audit row color coding: green = allowed/executed, red = blocked,
   yellow = needs confirmation, orange = execution failure, grey = neutral.
*/

// Apply light theme immediately if stored, to prevent dark flash
if (localStorage.getItem("arm_theme") === "light") {
  document.body.classList.add("theme-light");
}

const $ = (id) => document.getElementById(id);

let sessionId = localStorage.getItem("arm_session_id") || null;
let seenEvents = 0;          // how many audit events we've already rendered
const renderedIds = new Set(); // de-dupe guard for poll races

/* ================= chat ================= */

function addMsg(text, who, isError = false) {
  const div = document.createElement("div");
  div.className = `msg ${who}` + (isError ? " error" : "");
  div.textContent = text; // textContent => agent output is never injected as HTML
  $("chatWindow").appendChild(div);
  $("chatWindow").scrollTop = $("chatWindow").scrollHeight;
  return div;
}

function showTyping() {
  const div = document.createElement("div");
  div.className = "msg agent typing";
  div.id = "typingIndicator";
  div.innerHTML = "<span></span><span></span><span></span>";
  $("chatWindow").appendChild(div);
  $("chatWindow").scrollTop = $("chatWindow").scrollHeight;
}

async function sendMessage(text) {
  if (!text.trim()) return;
  addMsg(text, "user");
  $("chatInput").value = "";
  $("sendBtn").disabled = true;
  showTyping();

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    const data = await res.json();
    sessionId = data.session_id;
    localStorage.setItem("arm_session_id", sessionId);

    $("typingIndicator").remove();
    // The agent may legitimately report an error it handled gracefully
    const isError = /handled gracefully|failed/i.test(data.reply || "");
    addMsg(data.reply || "(empty response)", "agent", isError);
  } catch (err) {
    $("typingIndicator")?.remove();
    addMsg("Network error: could not reach the merchant API.", "agent", true);
  } finally {
    $("sendBtn").disabled = false;
    $("chatInput").focus();
  }
}

$("chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage($("chatInput").value);
});

// One-click demo prompts
document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => sendMessage(chip.dataset.msg))
);

// Fresh start for a new demo take: new session + clear local view
$("resetBtn").addEventListener("click", async () => {
  await fetch("/session/reset", { method: "POST" });
  localStorage.removeItem("arm_session_id");
  sessionId = null;
  seenEvents = 0;
  renderedIds.clear();
  $("chatWindow").innerHTML = "";
  $("auditList").innerHTML =
    '<div class="empty-hint">Trail cleared. Send a message to the agent —<br>every policy decision will appear here live.</div>';
  refreshMetrics(true);
});

/* ================= audit trail ================= */

// Map each backend event type to [cssClass, badgeLabel]
const EVENT_STYLES = {
  policy_decision_allowed: ["allowed", "ALLOWED"],
  policy_decision_blocked: ["blocked", "BLOCKED"],
  policy_decision_confirm: ["confirm", "CONFIRM REQUIRED"],
  purchase_executed: ["executed", "PURCHASED"],
  purchase_failed: ["failed", "EXEC FAILED"],
  agent_reasoning: ["neutral", "AGENT INTENT"],
};

function classify(event) {
  if (event.event_type === "policy_decision") {
    if (!event.allowed && event.requires_confirmation) return EVENT_STYLES.policy_decision_confirm;
    if (!event.allowed) return EVENT_STYLES.policy_decision_blocked;
    return EVENT_STYLES.policy_decision_allowed;
  }
  return EVENT_STYLES[event.event_type] || ["neutral", event.event_type.replace(/_/g, " ").toUpperCase()];
}

function titleFor(ev) {
  switch (ev.event_type) {
    case "policy_decision":
      return `${ev.product_name} -- Rs.${ev.price}` +
             (ev.discount_amount ? ` (was Rs.${ev.original_price})` : "");
    case "purchase_executed":
      return `${ev.product_name} -- charged Rs.${ev.amount}` +
             (ev.discount_amount ? ` . saved Rs.${ev.discount_amount}` : "");
    case "upsell_suggested":
      return ev.suggested_product_id ? `Suggested ${ev.suggested_product_id}` : "No upsell found";
    case "campaign_offer_surfaced":
      return `${ev.offers_count} clearance offer(s) surfaced`;
    case "agent_reasoning":
      return ev.tool_name ? `Calling ${ev.tool_name}` : "Agent reasoning";
    default:
      return ev.product_name || ev.user_message?.slice(0, 60) || "";
  }
}

function reasonFor(ev) {
  if (ev.reason) return ev.reason;
  if (ev.event_type === "purchase_executed")
    return `Order ${ev.order?.order_id || ""} . stock left: ${ev.stock_left}`;
  if (ev.event_type === "browse_catalog")
    return `filters: ${JSON.stringify(ev.filters)} -> ${ev.results_count} results`;
  if (ev.event_type === "agent_reasoning")
    return ev.reasoning ? `Tool: ${ev.tool_name} | Reasoning: ${ev.reasoning}` : `Tool: ${ev.tool_name}`;
  if (ev.error) return ev.error;
  return "";
}

function mandateFor(ev) {
  const m = ev.mandate;
  if (!m) return "";
  const cats = Array.isArray(m.allowed_categories) ? m.allowed_categories.join(", ") : "";
  return `Ceiling: Rs.${m.max_amount} | Confirm above: Rs.${m.confirmation_required_above} | Categories: ${cats} | Session limit: ${m.session_limit}`;
}

function agentReasoningFor(ev) {
  if (ev.event_type !== "agent_reasoning") return "";
  if (!ev.reasoning) return "";
  return ev.reasoning;
}

function tagsFor(ev) {
  let t = "";
  if (ev.upsell) t += ' <span class="tag-upsell">UPSELL</span>';
  if (ev.campaign) t += ' <span class="tag-campaign">CAMPAIGN</span>';
  return t;
}

function renderEvent(ev) {
  const key = ev.timestamp + ev.event_type + (ev.product_id || "") + JSON.stringify(ev.reason || "");
  if (renderedIds.has(key)) return;
  renderedIds.add(key);

  const [cls, label] = classify(ev);
  const time = new Date(ev.timestamp).toLocaleTimeString([], { hour12: false });

  const row = document.createElement("div");
  row.className = `audit-row ${cls}`;

  // Build with DOM APIs + textContent so nothing from the log is parsed as HTML
  const badgeCell = document.createElement("div");
  const badge = document.createElement("span");
  badge.className = `badge ${cls}`;
  badge.textContent = label;
  badgeCell.appendChild(badge);

  const mid = document.createElement("div");
  const title = document.createElement("div");
  title.className = "audit-title";
  title.innerHTML = titleFor(ev) + tagsFor(ev); // titleFor/tagsFor are ours only, no raw log text
  mid.appendChild(title);

  // Agent reasoning layer (for agent_reasoning events)
  const agentReasoning = agentReasoningFor(ev);
  if (agentReasoning) {
    const arDiv = document.createElement("div");
    arDiv.className = "audit-agent-reasoning";
    arDiv.textContent = agentReasoning;
    mid.appendChild(arDiv);
  }

  const right = document.createElement("div");
  const meta = document.createElement("div");
  meta.className = "audit-meta";
  meta.textContent = time;
  const reason = document.createElement("div");
  reason.className = "audit-reason";
  reason.textContent = reasonFor(ev); // raw text from log stays textContent
  right.appendChild(meta);
  right.appendChild(reason);

  // Mandate layer (for policy_decision events)
  const mandateText = mandateFor(ev);
  if (mandateText) {
    const mandateDiv = document.createElement("div");
    mandateDiv.className = "audit-mandate";
    mandateDiv.textContent = mandateText;
    right.appendChild(mandateDiv);
  }

  row.append(badgeCell, mid, right);

  // newest first
  const list = $("auditList");
  list.prepend(row);
}

async function pollAudit() {
  try {
    const res = await fetch(`/audit-log?after=${seenEvents}`);
    const data = await res.json();
    data.events.forEach(renderEvent);
    seenEvents = data.total;
  } catch (_) { /* server briefly unavailable during reload - just retry next tick */ }
}

/* ================= metrics ================= */

let prevMetrics = null;

function animateNumericText(el, targetText) {
  // Extract number from targetText (e.g. "₹1,200" -> 1200, "15" -> 15)
  const targetNum = parseInt(targetText.replace(/[^\d]/g, ""), 10) || 0;
  const isCurrency = targetText.includes("₹");
  
  if (el._currentValue === undefined) {
    el._currentValue = targetNum;
    el.textContent = targetText;
    return;
  }
  
  const startNum = el._currentValue;
  if (startNum === targetNum) {
    el.textContent = targetText;
    return;
  }
  
  el._currentValue = targetNum;
  
  const duration = 800; // 0.8 seconds
  const startTime = performance.now();
  
  function update(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / duration, 1);
    
    // Easing out quad
    const easeProgress = progress * (2 - progress);
    
    const currentNum = Math.floor(startNum + (targetNum - startNum) * easeProgress);
    
    if (isCurrency) {
      el.textContent = `₹${currentNum.toLocaleString("en-IN")}`;
    } else {
      el.textContent = String(currentNum);
    }
    
    if (progress < 1) {
      requestAnimationFrame(update);
    } else {
      el.textContent = targetText;
    }
  }
  
  requestAnimationFrame(update);
}

function setStat(id, text, cardId) {
  const el = $(id);
  if (el.textContent !== text) {
    if (cardId && el.classList.contains("stat-value")) {
      animateNumericText(el, text);
      el.classList.remove("bump");
      void el.offsetWidth; // restart animation
      el.classList.add("bump");
    } else {
      el.textContent = text;
    }
  }
}

async function refreshMetrics(force = false) {
  try {
    const res = await fetch("/metrics");
    const m = await res.json();
    const changed = force || JSON.stringify(m) !== JSON.stringify(prevMetrics);
    if (!changed) return;

    setStat("m-total-revenue", `₹${m.total_sales_revenue.toLocaleString("en-IN")}`, "card-sales");
    setStat("m-total-count", `${m.total_sales} orders`);
    setStat("m-upsell", `₹${m.upsell_revenue.toLocaleString("en-IN")}`, "card-upsell");
    setStat("m-upsell-count", `${m.upsell_sales} accepted`);
    setStat("m-campaign", `₹${m.campaign_revenue.toLocaleString("en-IN")}`, "card-campaign");
    setStat("m-campaign-sub", `₹${m.discount_given.toLocaleString("en-IN")} discounts given`);
    setStat("m-blocked", String(m.blocked_attempts), "card-blocked");
    setStat("m-confirm-sub", `${m.confirmation_required} needed confirmation`);

    prevMetrics = m;
  } catch (_) { /* retry next tick */ }
}

/* ================= boot ================= */

// Hook up theme toggle listener
const themeToggleBtn = $("themeToggleBtn");
if (themeToggleBtn) {
  themeToggleBtn.addEventListener("click", () => {
    document.body.classList.toggle("theme-light");
    const newTheme = document.body.classList.contains("theme-light") ? "light" : "dark";
    localStorage.setItem("arm_theme", newTheme);
  });
}

pollAudit();          // backfill any events already in the log
refreshMetrics(true);
setInterval(pollAudit, 1200);
setInterval(refreshMetrics, 1200);
addMsg(
  "Hi! I'm your AI shopping agent. I can browse the catalog, buy items for you " +
  "(every purchase passes the merchant's policy gate), and find you clearance deals. What are we shopping for?",
  "agent"
);
