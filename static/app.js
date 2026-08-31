/* Agent-Ready Merchant - frontend logic
   -------------------------------------
   Responsibilities:
   1. Chat: POST /chat, render bubbles with rich product cards + action feedback
   2. Live panels: poll /audit-log?after=N and /metrics every ~1.2s
   3. Revenue chart: animated canvas chart with gradient fills + glow
   4. Buyer simulation: multi-agent negotiation with styled log rendering
   5. Audit export: download trail as JSON/CSV
*/

let currentTheme = "dark";
const $ = (id) => document.getElementById(id);
let sessionId = localStorage.getItem("arm_session_id") || null;
let seenEvents = 0;
const renderedIds = new Set();

/* ================= helpers ================= */

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

function formatPrice(n) {
  return "\u20B9" + Number(n).toLocaleString("en-IN");
}

/* ================= chat ================= */

function addMsg(text, who, isError = false) {
  const div = document.createElement("div");
  div.className = `msg ${who}` + (isError ? " error" : "");
  div.textContent = text;
  $("chatWindow").appendChild(div);
  $("chatWindow").scrollTop = $("chatWindow").scrollHeight;
  return div;
}

function addRichMsg(text, products, actions, isError = false) {
  const div = document.createElement("div");
  div.className = "msg agent" + (isError ? " error" : "");

  // Text part
  if (text) {
    const textSpan = document.createElement("div");
    textSpan.className = "msg-text";
    textSpan.textContent = text;
    div.appendChild(textSpan);
  }

  // Product cards
  if (products && products.length > 0) {
    const row = document.createElement("div");
    row.className = "product-card-row";
    products.forEach(p => {
      const card = document.createElement("div");
      card.className = "product-card-mini";

      const name = document.createElement("div");
      name.className = "product-card-mini-name";
      name.textContent = p.name;
      card.appendChild(name);

      const price = document.createElement("div");
      price.className = "product-card-mini-price";
      price.textContent = formatPrice(p.price);
      card.appendChild(price);

      const stock = document.createElement("div");
      stock.className = "product-card-mini-stock";
      stock.textContent = p.stock + " in stock";
      card.appendChild(stock);

      if (p.category) {
        const cat = document.createElement("div");
        cat.className = "product-card-mini-cat";
        cat.textContent = p.category;
        card.appendChild(cat);
      }

      if (p.source === "upsell" && p.reason) {
        const reason = document.createElement("div");
        reason.className = "product-card-mini-reason";
        reason.textContent = p.reason;
        card.appendChild(reason);
      }

      row.appendChild(card);
    });
    div.appendChild(row);
  }

  // Action feedback (blocked / purchased)
  if (actions && actions.length > 0) {
    actions.forEach(a => {
      const actionDiv = document.createElement("div");
      actionDiv.className = "action-feedback";

      if (a.action === "blocked") {
        actionDiv.classList.add("action-blocked");
        actionDiv.innerHTML =
          '<span class="action-icon">\u274C</span>' +
          '<span class="action-text"><strong>' + escapeHtml(a.product_name) +
          '</strong> blocked \u2014 ' + escapeHtml(a.reason) + '</span>';
      } else if (a.action === "purchased") {
        actionDiv.classList.add("action-purchased");
        let detail = formatPrice(a.price);
        if (a.discount) detail += ' (saved ' + formatPrice(a.discount) + ')';
        actionDiv.innerHTML =
          '<span class="action-icon">\u2705</span>' +
          '<span class="action-text"><strong>' + escapeHtml(a.product_name) +
          '</strong> purchased for ' + detail + '</span>';
      }

      div.appendChild(actionDiv);
    });
  }

  $("chatWindow").appendChild(div);
  $("chatWindow").scrollTop = $("chatWindow").scrollHeight;
  return div;
}

function showThinking() {
  const div = document.createElement("div");
  div.className = "msg agent thinking-indicator";
  div.id = "thinkingIndicator";
  div.innerHTML =
    '<div class="thinking-dot"></div>' +
    '<div class="thinking-dot"></div>' +
    '<div class="thinking-dot"></div>' +
    '<span class="thinking-label">Agent is thinking...</span>';
  $("chatWindow").appendChild(div);
  $("chatWindow").scrollTop = $("chatWindow").scrollHeight;
}

async function sendMessage(text) {
  if (!text.trim()) return;
  addMsg(text, "user");
  $("chatInput").value = "";
  $("sendBtn").disabled = true;
  showThinking();

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    const data = await res.json();
    sessionId = data.session_id;
    localStorage.setItem("arm_session_id", sessionId);

    $("thinkingIndicator")?.remove();
    const isError = /handled gracefully|failed/i.test(data.reply || "");
    const hasCards = (data.products && data.products.length > 0) ||
                     (data.actions && data.actions.length > 0);
    if (hasCards) {
      addRichMsg(data.reply || "", data.products || [], data.actions || [], isError);
    } else {
      addMsg(data.reply || "(empty response)", "agent", isError);
    }
  } catch (err) {
    $("thinkingIndicator")?.remove();
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

document.querySelectorAll(".chip[data-msg]").forEach((chip) =>
  chip.addEventListener("click", () => sendMessage(chip.dataset.msg))
);

$("resetBtn").addEventListener("click", async () => {
  await fetch("/session/reset", { method: "POST" });
  localStorage.removeItem("arm_session_id");
  sessionId = null;
  seenEvents = 0;
  renderedIds.clear();
  $("chatWindow").innerHTML = "";
  $("auditList").innerHTML =
    '<div class="empty-hint">Trail cleared. Send a message to the agent \u2014<br>every policy decision will appear here live.</div>';
  revenueData = [];
  chartAnimProgress = 0;
  refreshMetrics(true);
  drawRevenueChart();
});

/* ================= audit trail ================= */

const EVENT_STYLES = {
  policy_decision_allowed: ["allowed", "ALLOWED"],
  policy_decision_blocked: ["blocked", "BLOCKED"],
  policy_decision_confirm: ["confirm", "CONFIRM REQUIRED"],
  purchase_executed: ["executed", "PURCHASED"],
  purchase_failed: ["failed", "EXEC FAILED"],
  agent_reasoning: ["neutral", "AGENT INTENT"],
  agent_to_agent: ["agent-to-agent", "A2A NEGOTIATE"],
};

const A2A_SUB_STYLES = {
  buyer_init: "BUYER START",
  browse_catalog: "BUYER BROWSE",
  item_selection: "BUYER SELECT",
  purchase_request: "BUYER REQUEST",
  purchase_blocked: "MERCHANT BLOCKED",
  policy_approved: "MERCHANT APPROVED",
  purchase_executed: "BUYER PURCHASED",
  purchase_failed: "BUYER FAILED",
  upsell_considered: "UPSELL OFFERED",
  budget_exhausted: "BUDGET OUT",
  buyer_complete: "BUYER DONE",
  no_items_found: "NO ITEMS",
};

function classify(event) {
  if (event.event_type === "agent_to_agent") {
    return ["agent-to-agent", A2A_SUB_STYLES[event.sub_type] || "A2A NEGOTIATE"];
  }
  if (event.event_type === "policy_decision") {
    if (!event.allowed && event.requires_confirmation) return EVENT_STYLES.policy_decision_confirm;
    if (!event.allowed) return EVENT_STYLES.policy_decision_blocked;
    return EVENT_STYLES.policy_decision_allowed;
  }
  return EVENT_STYLES[event.event_type] || ["neutral", event.event_type.replace(/_/g, " ").toUpperCase()];
}

function titleFor(ev) {
  if (ev.event_type === "agent_to_agent") {
    return ev.message || ev.sub_type || "Agent-to-Agent";
  }
  switch (ev.event_type) {
    case "policy_decision":
      return `${ev.product_name} -- ${formatPrice(ev.price)}` +
             (ev.discount_amount ? ` (was ${formatPrice(ev.original_price)})` : "");
    case "purchase_executed":
      return `${ev.product_name} -- charged ${formatPrice(ev.amount)}` +
             (ev.discount_amount ? ` . saved ${formatPrice(ev.discount_amount)}` : "");
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
  if (ev.event_type === "agent_to_agent") return "";
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
  return `Ceiling: ${formatPrice(m.max_amount)} | Confirm above: ${formatPrice(m.confirmation_required_above)} | Categories: ${cats} | Session limit: ${m.session_limit}`;
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
  if (ev.event_type === "agent_to_agent") t += ' <span class="tag-upsell" style="background:rgba(34,211,238,0.08);border-color:rgba(34,211,238,0.15);color:var(--accent-secondary)">A2A</span>';
  return t;
}

function renderEvent(ev) {
  const key = ev.timestamp + ev.event_type + (ev.sub_type || "") + (ev.product_id || "") + JSON.stringify(ev.reason || ev.message || "");
  if (renderedIds.has(key)) return;
  renderedIds.add(key);

  const [cls, label] = classify(ev);
  const time = new Date(ev.timestamp).toLocaleTimeString([], { hour12: false });

  const row = document.createElement("div");
  row.className = `audit-row ${cls}`;

  const badgeCell = document.createElement("div");
  const badge = document.createElement("span");
  badge.className = `badge ${cls}`;
  badge.textContent = label;
  badgeCell.appendChild(badge);

  const mid = document.createElement("div");
  const title = document.createElement("div");
  title.className = "audit-title";
  title.innerHTML = titleFor(ev) + tagsFor(ev);
  mid.appendChild(title);

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
  reason.textContent = reasonFor(ev);
  right.appendChild(meta);
  right.appendChild(reason);

  const mandateText = mandateFor(ev);
  if (mandateText) {
    const mandateDiv = document.createElement("div");
    mandateDiv.className = "audit-mandate";
    mandateDiv.textContent = mandateText;
    right.appendChild(mandateDiv);
  }

  row.append(badgeCell, mid, right);
  $("auditList").prepend(row);
}

async function pollAudit() {
  try {
    const res = await fetch(`/audit-log?after=${seenEvents}`);
    const data = await res.json();
    data.events.forEach(renderEvent);
    seenEvents = data.total;
  } catch (_) {}
}

/* ================= metrics with delta indicators ================= */

let prevMetrics = null;

function animateNumber(el, endValue, prefix = '') {
  const startValue = parseInt(el.dataset.rawValue || '0', 10);
  if (startValue === endValue) return;
  const duration = 600;
  const startTime = performance.now();
  function tick(now) {
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = Math.round(startValue + (endValue - startValue) * eased);
    el.textContent = prefix + current.toLocaleString("en-IN");
    if (progress < 1) requestAnimationFrame(tick);
    else el.dataset.rawValue = endValue;
  }
  requestAnimationFrame(tick);
}

function showDelta(el, delta, prefix) {
  if (delta <= 0) return;
  const existing = el.parentElement?.querySelector(".metric-delta");
  if (existing) existing.remove();

  const d = document.createElement("span");
  d.className = "metric-delta";
  d.textContent = "+" + prefix + delta.toLocaleString("en-IN");
  el.parentElement?.appendChild(d);
  setTimeout(() => d.remove(), 2100);
}

function setStat(id, text, cardId, isMetricNum = false, rawVal = 0, prefix = "") {
  const el = $(id);
  if (isMetricNum) {
    const prev = parseInt(el.dataset.rawValue || "0", 10);
    if (prev !== rawVal) {
      animateNumber(el, rawVal, prefix);
      showDelta(el, rawVal - prev, prefix);
      if (cardId) {
        el.classList.remove("bump");
        void el.offsetWidth;
        el.classList.add("bump");
      }
    }
  } else {
    if (el.textContent !== text) {
      el.textContent = text;
      if (cardId) {
        el.classList.remove("bump");
        void el.offsetWidth;
        el.classList.add("bump");
      }
    }
  }
}

async function refreshMetrics(force = false) {
  try {
    const res = await fetch("/metrics");
    const m = await res.json();
    const changed = force || JSON.stringify(m) !== JSON.stringify(prevMetrics);
    if (!changed) return;

    setStat("m-total-revenue", "", "card-sales", true, m.total_sales_revenue, "\u20B9");
    setStat("m-total-count", `${m.total_sales} orders`);
    setStat("m-upsell", "", "card-upsell", true, m.upsell_revenue, "\u20B9");
    setStat("m-upsell-count", `${m.upsell_sales} accepted`);
    setStat("m-campaign", "", "card-campaign", true, m.campaign_revenue, "\u20B9");
    setStat("m-campaign-sub", `\u20B9${m.discount_given.toLocaleString("en-IN")} discounts given`);
    setStat("m-blocked", "", "card-blocked", true, m.blocked_attempts, "");
    setStat("m-confirm-sub", `${m.confirmation_required} needed confirmation`);

    revenueData.push({
      time: Date.now(),
      total: m.total_sales_revenue,
      upsell: m.upsell_revenue,
      campaign: m.campaign_revenue,
    });
    if (revenueData.length > 30) revenueData = revenueData.slice(-30);

    // Trigger chart animation on new data
    if (prevMetrics && m.total_sales_revenue !== prevMetrics.total_sales_revenue) {
      chartAnimStart = performance.now();
    }
    drawRevenueChart();

    prevMetrics = m;
  } catch (_) {}
}

/* ================= revenue chart (upgraded) ================= */

let revenueData = [];
let chartAnimStart = 0;
const CHART_ANIM_DURATION = 500;

function formatRupeeAxis(val) {
  if (val >= 1000) return "\u20B9" + (val / 1000).toFixed(val % 1000 === 0 ? 0 : 1) + "k";
  return "\u20B9" + val;
}

function drawRevenueChart() {
  const canvas = $("revenueChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width;
  const h = rect.height;

  ctx.clearRect(0, 0, w, h);

  if (revenueData.length < 2) {
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--text-muted").trim();
    ctx.font = "12px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for purchase data...", w / 2, h / 2);
    return;
  }

  const pad = { top: 22, right: 20, bottom: 24, left: 52 };
  const chartW = w - pad.left - pad.right;
  const chartH = h - pad.top - pad.bottom;
  const maxVal = Math.max(...revenueData.map(d => d.total), 1) * 1.15;

  const style = getComputedStyle(document.documentElement);
  const accentColor = style.getPropertyValue("--accent-primary").trim();
  const successColor = style.getPropertyValue("--success").trim();
  const warningColor = style.getPropertyValue("--warning").trim();
  const mutedColor = style.getPropertyValue("--text-muted").trim();
  const gridColor = style.getPropertyValue("--border-subtle").trim();

  // --- Grid lines ---
  ctx.strokeStyle = gridColor;
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (chartH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();

    ctx.fillStyle = mutedColor;
    ctx.font = "10px 'Space Grotesk', monospace";
    ctx.textAlign = "right";
    const val = Math.round(maxVal - (maxVal / 4) * i);
    ctx.fillText(formatRupeeAxis(val), pad.left - 6, y + 3);
  }

  // --- X-axis labels ---
  ctx.textAlign = "center";
  ctx.font = "9px Inter, sans-serif";
  const step = Math.max(1, Math.floor(revenueData.length / 5));
  for (let i = 0; i < revenueData.length; i += step) {
    const x = pad.left + (chartW / (revenueData.length - 1)) * i;
    const t = new Date(revenueData[i].time).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    ctx.fillText(t, x, h - 4);
  }

  // --- Animated draw progress ---
  const elapsed = performance.now() - chartAnimStart;
  const drawProgress = Math.min(elapsed / CHART_ANIM_DURATION, 1);
  const easedProgress = 1 - Math.pow(1 - drawProgress, 3);
  const visiblePoints = Math.ceil(revenueData.length * easedProgress);
  const visibleData = revenueData.slice(0, visiblePoints);
  if (visibleData.length < 2) return;

  function drawLine(data, key, color, withGlow, withFill, withDots) {
    // Glow
    if (withGlow) {
      ctx.shadowColor = color;
      ctx.shadowBlur = 10;
    }

    // Stroke
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = pad.left + (chartW / (revenueData.length - 1)) * i;
      const y = pad.top + chartH - (data[i][key] / maxVal) * chartH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Gradient fill
    if (withFill) {
      const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + chartH);
      grad.addColorStop(0, color.replace(")", ", 0.30)").replace("rgb", "rgba").replace("#", ""));
      // Handle hex colors
      const r = parseInt(color.slice(1, 3), 16) || 99;
      const g = parseInt(color.slice(3, 5), 16) || 102;
      const b = parseInt(color.slice(5, 7), 16) || 241;
      const fillGrad = ctx.createLinearGradient(0, pad.top, 0, pad.top + chartH);
      fillGrad.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.28)`);
      fillGrad.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0.02)`);

      ctx.fillStyle = fillGrad;
      ctx.beginPath();
      for (let i = 0; i < data.length; i++) {
        const x = pad.left + (chartW / (revenueData.length - 1)) * i;
        const y = pad.top + chartH - (data[i][key] / maxVal) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      const lastX = pad.left + (chartW / (revenueData.length - 1)) * (data.length - 1);
      ctx.lineTo(lastX, pad.top + chartH);
      ctx.lineTo(pad.left, pad.top + chartH);
      ctx.closePath();
      ctx.fill();
    }

    // Data point dots
    if (withDots) {
      for (let i = 0; i < data.length; i++) {
        const x = pad.left + (chartW / (revenueData.length - 1)) * i;
        const y = pad.top + chartH - (data[i][key] / maxVal) * chartH;
        // White center
        ctx.beginPath();
        ctx.arc(x, y, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, 1.5, 0, Math.PI * 2);
        ctx.fillStyle = "#fff";
        ctx.fill();
      }
    }
  }

  drawLine(visibleData, "total", accentColor, true, true, true);
  drawLine(visibleData, "upsell", successColor, false, false, true);
  if (revenueData.some(d => d.campaign > 0)) {
    drawLine(visibleData, "campaign", warningColor, false, false, true);
  }

  // Legend
  const legends = [
    { label: "Total", color: accentColor },
    { label: "Upsell", color: successColor },
  ];
  if (revenueData.some(d => d.campaign > 0)) legends.push({ label: "Campaign", color: warningColor });
  let lx = pad.left + 4;
  ctx.font = "10px Inter, sans-serif";
  ctx.textAlign = "left";
  legends.forEach(l => {
    ctx.fillStyle = l.color;
    ctx.fillRect(lx, 4, 12, 3);
    ctx.fillStyle = mutedColor;
    ctx.fillText(l.label, lx + 16, 9);
    lx += ctx.measureText(l.label).width + 30;
  });
}

// Animate chart on load
function animateChart() {
  const elapsed = performance.now() - chartAnimStart;
  if (elapsed < CHART_ANIM_DURATION) {
    drawRevenueChart();
    requestAnimationFrame(animateChart);
  }
}

window.addEventListener("resize", drawRevenueChart);

/* ================= simulate AI buyer ================= */

let simulating = false;

function renderNegotiationLog(logLines) {
  if (!logLines || logLines.length === 0) return;

  const div = document.createElement("div");
  div.className = "msg agent negotiation-log";

  const header = document.createElement("div");
  header.className = "negotiation-header";
  header.textContent = "AI Buyer Agent \u2014 Negotiation Log";
  div.appendChild(header);

  logLines.forEach(line => {
    const row = document.createElement("div");
    row.className = "neg-row";

    let icon = "\uD83D\uDD35"; // blue circle default
    let cls = "neg-info";
    if (line.includes("BLOCKED") || line.includes("blocked")) {
      icon = "\u274C"; cls = "neg-blocked";
    } else if (line.includes("purchased") || line.includes("SUCCESS")) {
      icon = "\u2705"; cls = "neg-success";
    } else if (line.includes("REQUEST") || line.includes("requests")) {
      icon = "\uD83D\uDCE8"; cls = "neg-request";
    } else if (line.includes("selected") || line.includes("SELECT")) {
      icon = "\uD83C\uDFAF"; cls = "neg-select";
    } else if (line.includes("budget") || line.includes("BUDGET")) {
      icon = "\uD83D\uDCB0"; cls = "neg-budget";
    } else if (line.includes("activated") || line.includes("started")) {
      icon = "\uD83D\uDE80"; cls = "neg-start";
    } else if (line.includes("complete") || line.includes("DONE")) {
      icon = "\uD83C\uDFC6"; cls = "neg-complete";
    } else if (line.includes("upsell") || line.includes("UPSELL")) {
      icon = "\uD83D\uDCA1"; cls = "neg-upsell";
    }

    row.classList.add(cls);
    row.innerHTML = '<span class="neg-icon">' + icon + '</span><span class="neg-text">' + escapeHtml(line) + '</span>';
    div.appendChild(row);
  });

  $("chatWindow").appendChild(div);
  $("chatWindow").scrollTop = $("chatWindow").scrollHeight;
}

async function simulateBuyer() {
  if (simulating) return;
  simulating = true;
  const btn = $("simulateBtn");
  if (btn) btn.textContent = "Running...";

  addMsg("Starting AI buyer agent simulation (budget: Rs.2000)...", "user");
  showThinking();

  try {
    const res = await fetch("/simulate-negotiation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ budget: 2000, goal: "buy a complete outfit" }),
    });
    const data = await res.json();
    $("thinkingIndicator")?.remove();

    if (data.status === "complete") {
      const s = data.summary;
      addMsg(
        `AI Buyer completed: ${s.items_purchased} items purchased for ${formatPrice(s.total_spent)} ` +
        `of ${formatPrice(s.budget)} budget. ${s.items_rejected} items were blocked by merchant policy.`,
        "agent"
      );
      // Render the negotiation log as styled rows
      if (data.log && data.log.length > 0) {
        renderNegotiationLog(data.log);
      }
    } else {
      addMsg("Simulation completed with unexpected status.", "agent", true);
    }
  } catch (err) {
    $("thinkingIndicator")?.remove();
    addMsg("Simulation failed: could not reach the server.", "agent", true);
  } finally {
    simulating = false;
    if (btn) btn.textContent = "Simulate AI Buyer";
    refreshMetrics(true);
  }
}

const simBtn = $("simulateBtn");
if (simBtn) simBtn.addEventListener("click", simulateBuyer);

/* ================= export audit log ================= */

async function exportAuditLog() {
  try {
    const res = await fetch("/audit-log/export?fmt=json");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "audit_trail.json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (err) {
    addMsg("Export failed: could not download audit log.", "agent", true);
  }
}

const exportBtn = $("exportBtn");
if (exportBtn) exportBtn.addEventListener("click", exportAuditLog);

/* ================= theme toggle ================= */

const themeToggleBtn = $("themeToggleBtn");
if (themeToggleBtn) {
  themeToggleBtn.addEventListener("click", () => {
    currentTheme = currentTheme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", currentTheme);
    setTimeout(drawRevenueChart, 50);
  });
}

/* ================= boot ================= */

chartAnimStart = performance.now();
pollAudit();
refreshMetrics(true);
setInterval(pollAudit, 1200);
setInterval(refreshMetrics, 1200);
requestAnimationFrame(animateChart);
addMsg(
  "Hi! I'm your AI shopping agent. I can browse the catalog, buy items for you " +
  "(every purchase passes the merchant's policy gate), and find you clearance deals. What are we shopping for?",
  "agent"
);
