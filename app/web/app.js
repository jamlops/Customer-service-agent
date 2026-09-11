const state = {
  sessionId: null,
  selectedAgent: "auto",
  agents: [],
  sessions: JSON.parse(localStorage.getItem("customer-agent-sessions") || "[]"),
  busy: false,
  supportStatus: null,
  pollTimer: null,
  attachments: [],
  userId: localStorage.getItem("customer-agent-user-id") || `customer-${crypto.randomUUID().slice(0, 8)}`,
};
localStorage.setItem("customer-agent-user-id", state.userId);

const agentMeta = {
  auto: { icon: "sparkles", label: "智能调度" },
  orchestrator: { icon: "network", label: "智能调度" },
  order: { icon: "package-search", label: "订单售后" },
  faq: { icon: "book-open", label: "政策顾问" },
  human: { icon: "user-round", label: "人工服务" },
  support: { icon: "bot", label: "排队托管 Agent" },
  clarify: { icon: "message-circle-question", label: "智能客服" },
};

const $ = (selector) => document.querySelector(selector);
const messages = $("#messages");
const input = $("#messageInput");

function renderIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
}

function icon(name) {
  return `<i data-lucide="${name}"></i>`;
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function shortTime(timestamp = Date.now() / 1000) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
    .format(new Date(timestamp * 1000));
}

function saveSessions() {
  localStorage.setItem("customer-agent-sessions", JSON.stringify(state.sessions.slice(0, 20)));
}

function renderSessions() {
  const list = $("#sessionList");
  if (!state.sessions.length) {
    list.innerHTML = '<div class="empty-detail" style="padding:10px">暂无会话</div>';
    return;
  }
  list.innerHTML = state.sessions.map((session) => `
    <button class="session-item ${session.id === state.sessionId ? "active" : ""}" data-session="${escapeHtml(session.id)}">
      ${icon("message-square")}
      <span><strong>${escapeHtml(session.title || "新会话")}</strong><small>${escapeHtml(session.preview || "等待消息")}</small></span>
    </button>
  `).join("");
  list.querySelectorAll("[data-session]").forEach((button) => {
    button.addEventListener("click", () => loadSession(button.dataset.session));
  });
  renderIcons();
}

function updateSession(message, reply) {
  let session = state.sessions.find((item) => item.id === state.sessionId);
  if (!session) {
    session = { id: state.sessionId, title: message.slice(0, 18), preview: (reply || message).slice(0, 28) };
    state.sessions.unshift(session);
  } else {
    session.preview = (reply || message).slice(0, 28);
    state.sessions = [session, ...state.sessions.filter((item) => item.id !== session.id)];
  }
  saveSessions();
  renderSessions();
}

function renderAgentSwitcher() {
  $("#agentSwitcher").innerHTML = state.agents.map((agent) => {
    const meta = agentMeta[agent.id] || agentMeta.auto;
    return `<button class="agent-chip ${agent.id === state.selectedAgent ? "active" : ""}" data-agent="${agent.id}" title="${escapeHtml(agent.description)}">${icon(meta.icon)}<span>${escapeHtml(agent.name)}</span></button>`;
  }).join("");
  document.querySelectorAll("[data-agent]").forEach((button) => {
    button.addEventListener("click", () => selectAgent(button.dataset.agent));
  });
  renderIcons();
}

function selectAgent(id) {
  state.selectedAgent = id;
  renderAgentSwitcher();
  const meta = agentMeta[id] || agentMeta.auto;
  $("#agentName").textContent = meta.label;
  $("#agentAvatar").innerHTML = icon(meta.icon);
  renderIcons();
}

function addMessage(role, content, timestamp, options = {}) {
  $("#welcome")?.remove();
  const row = document.createElement("article");
  row.className = `message-row ${role}`;
  if (options.typing) row.classList.add("typing");
  const meta = agentMeta[options.agent] || agentMeta.auto;
  const quote = options.meta?.reply_to ? `<span class="message-quote">回复：${escapeHtml(options.meta.reply_to.content)}</span>` : "";
  const attachmentHtml = (options.meta?.attachments || []).map(item => `<a href="${escapeHtml(item.url)}" target="_blank"><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.name || "图片")}"></a>`).join("");
  const bubble = options.typing ? "<i></i><i></i><i></i>" : `${quote}${escapeHtml(content)}${attachmentHtml ? `<span class="message-attachments">${attachmentHtml}</span>` : ""}`;
  row.innerHTML = role === "user"
    ? `<div class="message-body"><div class="message-meta"><span>${shortTime(timestamp)}</span><strong>您</strong></div><div class="bubble">${bubble}</div></div>`
    : `<span class="message-avatar">${icon(meta.icon)}</span><div class="message-body"><div class="message-meta"><strong>${escapeHtml(meta.label)}</strong><span>${shortTime(timestamp)}</span></div><div class="bubble">${bubble}</div></div>`;
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
  renderIcons();
  return row;
}

function resetConversation() {
  state.sessionId = null;
  state.supportStatus = null;
  messages.innerHTML = `
    <div class="welcome" id="welcome">
      <span class="welcome-mark">${icon("headset")}</span>
      <h1>您好，需要处理什么问题？</h1>
      <p>我会为您连接合适的客服专员。</p>
      <div class="quick-actions">
        <button data-prompt="查询订单 SO20260905001 的物流">查询物流</button>
        <button data-prompt="我想申请退款">申请退款</button>
        <button data-prompt="你们支持7天无理由退货吗？">退货政策</button>
        <button data-prompt="我要转人工客服">转人工</button>
      </div>
    </div>`;
  bindQuickActions();
  updateInspector({ agent: "auto", route_reason: "尚未执行调度", tools_used: [], slots: {}, turns: 0, has_summary: false });
  updateSupportStatus({});
  renderSessions();
  input.focus();
  renderIcons();
}

async function loadSession(id) {
  try {
    const response = await fetch(`/sessions/${encodeURIComponent(id)}`);
    if (!response.ok) throw new Error("读取会话失败");
    const data = await response.json();
    state.sessionId = id;
    messages.innerHTML = "";
    data.messages.forEach((item) => addMessage(item.role, item.content, item.timestamp, { agent: item.name === "human" ? "human" : "auto", meta: item.meta }));
    if (!data.messages.length) resetConversation();
    updateInspector({ agent: "auto", route_reason: "已恢复历史会话", tools_used: [], slots: data.slots, turns: data.turns, has_summary: Boolean(data.summary) });
    updateSupportStatus(data);
    renderSessions();
    closeOverlays();
  } catch (error) {
    showToast(error.message);
  }
}

function updateInspector(data) {
  const meta = agentMeta[data.agent] || agentMeta.auto;
  $("#routeIcon").innerHTML = icon(meta.icon);
  $("#routeAgent").textContent = meta.label;
  $("#routeReason").textContent = data.route_reason;
  $("#turnCount").textContent = data.turns ?? 0;
  $("#summaryStatus").textContent = data.has_summary ? "已生成" : "未生成";
  $("#toolList").innerHTML = data.tools_used?.length
    ? data.tools_used.map((tool) => `<div class="tool-item">${icon("circle-check")}<span>${escapeHtml(tool)}</span></div>`).join("")
    : '<div class="empty-detail">本轮暂无调用</div>';
  const slots = Object.entries(data.slots || {});
  $("#slotList").innerHTML = slots.map(([key, value]) => `<span class="slot">${escapeHtml(key)} · ${escapeHtml(value)}</span>`).join("");
  $("#agentName").textContent = meta.label;
  $("#agentAvatar").innerHTML = icon(meta.icon);
  renderIcons();
}

function updateSupportStatus(data) {
  state.supportStatus = data.support_status || null;
  const banner = $("#handoffBanner");
  banner.className = "handoff-banner";
  if (state.supportStatus === "waiting") {
    banner.textContent = `人工工单 ${data.ticket_id || ""} 正在排队，当前由智能 Agent 临时响应。`;
    banner.classList.add("show");
    $("#serviceStatus").textContent = "Agent 托管中";
  } else if (state.supportStatus === "active") {
    banner.textContent = `${data.assigned_to || "人工客服"} 已接入，会话消息将直接发送给人工客服。`;
    banner.classList.add("show", "active");
    $("#serviceStatus").textContent = "人工服务中";
  } else {
    $("#serviceStatus").textContent = "在线";
  }
}

async function sendMessage(value) {
  const text = value.trim();
  if ((!text && !state.attachments.length) || state.busy) return;
  state.busy = true;
  input.value = "";
  resizeInput();
  const sentAttachments = [...state.attachments];
  addMessage("user", text || "[图片]", undefined, { meta: { attachments: sentAttachments } });
  state.attachments = [];
  renderCustomerAttachments();
  const typing = addMessage("assistant", "", undefined, { agent: state.selectedAgent, typing: true });
  $("#sendButton").disabled = true;

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: state.sessionId, agent: state.selectedAgent, user_id: state.userId, attachments: sentAttachments }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "请求失败");
    typing.remove();
    state.sessionId = data.session_id;
    if (data.reply) addMessage("assistant", data.reply, undefined, { agent: data.agent });
    updateInspector(data);
    updateSupportStatus(data);
    updateSession(text, data.reply);
  } catch (error) {
    typing.remove();
    addMessage("assistant", `服务暂时不可用：${error.message}`, undefined, { agent: "clarify" });
    showToast("发送失败，请检查服务状态");
  } finally {
    state.busy = false;
    $("#sendButton").disabled = false;
    input.focus();
  }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 128)}px`;
  $("#charCount").textContent = `${input.value.length} / 2000`;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2200);
}

function bindQuickActions() {
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => sendMessage(button.dataset.prompt));
  });
}

function closeOverlays() {
  $("#sidebar").classList.remove("open");
  $("#inspector").classList.remove("open");
  $("#scrim").classList.remove("show");
}

async function initialize() {
  renderIcons();
  bindQuickActions();
  renderSessions();
  try {
    const [configResponse, healthResponse] = await Promise.all([fetch("/config"), fetch("/health")]);
    if (!configResponse.ok || !healthResponse.ok) throw new Error("服务不可用");
    const config = await configResponse.json();
    const health = await healthResponse.json();
    state.agents = config.agents;
    renderAgentSwitcher();
    $("#modelBadge").textContent = config.deepseek_enabled ? config.model : "离线规则模式";
    $("#modelName").textContent = config.deepseek_enabled ? config.model : "离线规则兜底";
    $("#modeName").textContent = config.deepseek_enabled ? `${config.mode} · DeepSeek fallback` : `${config.mode} · DeepSeek 已停用`;
    $("#connectionText").textContent = "服务连接正常";
    $("#statusDot").classList.remove("offline");
    if (config.deepseek_enabled && !health.api_key_configured) showToast("尚未配置 DeepSeek API Key");
    state.pollTimer = setInterval(pollSession, 2500);
  } catch (_) {
    $("#connectionText").textContent = "服务连接中断";
    $("#statusDot").classList.add("offline");
    state.agents = [{ id: "auto", name: "智能调度", description: "自动调度" }];
    renderAgentSwitcher();
  }
}

async function pollSession() {
  if (!state.sessionId || state.busy) return;
  try {
    const response = await fetch(`/sessions/${encodeURIComponent(state.sessionId)}`);
    if (!response.ok) return;
    const data = await response.json();
    const rendered = messages.querySelectorAll(".message-row:not(.typing)").length;
    if (data.messages.length > rendered) {
      data.messages.slice(rendered).forEach((item) => addMessage(
        item.role, item.content, item.timestamp,
        { agent: item.name === "human" ? "human" : "auto", meta: item.meta },
      ));
      if (data.messages.slice(rendered).some(item => item.name === "human")) playNotice();
    }
    updateSupportStatus(data);
    const presence = await fetch(`/presence/${encodeURIComponent(state.sessionId)}`).then(res => res.json());
    $("#operatorTyping").textContent = presence.operator_typing ? "客服正在输入…" : "";
  } catch (_) {
    // 下一轮轮询自动恢复。
  }
}

function playNotice() {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioContext(); const osc = ctx.createOscillator(); const gain = ctx.createGain();
    osc.frequency.value = 760; gain.gain.setValueAtTime(.06, ctx.currentTime); gain.gain.exponentialRampToValueAtTime(.0001, ctx.currentTime + .16);
    osc.connect(gain); gain.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + .17);
  } catch (_) {}
}

async function uploadCustomerFile(file) {
  if (file.size > 5 * 1024 * 1024) throw new Error("图片不能超过 5 MB");
  const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
  const response = await fetch("/api/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: file.name, mime_type: file.type, data }) });
  const result = await response.json(); if (!response.ok) throw new Error(result.detail || "上传失败"); return result;
}

function renderCustomerAttachments() {
  $("#customerAttachmentPreview").innerHTML = state.attachments.map((item, index) => `<img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.name)}"><span>${escapeHtml(item.name)}</span><button type="button" data-remove-customer="${index}">移除</button>`).join("");
  $("#attachmentCount").textContent = state.attachments.length;
  $("#attachmentCount").hidden = state.attachments.length === 0;
  $("#customerImage").classList.toggle("has-attachment", state.attachments.length > 0);
  document.querySelectorAll("[data-remove-customer]").forEach(button => button.addEventListener("click", () => { state.attachments.splice(Number(button.dataset.removeCustomer), 1); renderCustomerAttachments(); }));
}

function toggleCustomerEmoji(open) {
  const shouldOpen = open ?? $("#customerEmojiPicker").hidden;
  $("#customerEmojiPicker").hidden = !shouldOpen;
  $("#customerEmoji").classList.toggle("active", shouldOpen);
  $("#customerEmoji").setAttribute("aria-expanded", String(shouldOpen));
}

$("#chatForm").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value);
});
input.addEventListener("input", resizeInput);
input.addEventListener("input", () => {
  if (!state.sessionId) return;
  clearTimeout(state.typingTimer);
  fetch(`/presence/${encodeURIComponent(state.sessionId)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: "customer", typing: true }) }).catch(() => {});
  state.typingTimer = setTimeout(() => fetch(`/presence/${encodeURIComponent(state.sessionId)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: "customer", typing: false }) }).catch(() => {}), 1600);
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage(input.value);
  }
});
$("#newChat").addEventListener("click", () => { resetConversation(); closeOverlays(); });
$("#toggleInspector").addEventListener("click", () => {
  $("#inspector").classList.toggle("open");
  $("#scrim").classList.toggle("show", $("#inspector").classList.contains("open"));
});
$("#closeInspector").addEventListener("click", closeOverlays);
$("#openSidebar").addEventListener("click", () => { $("#sidebar").classList.add("open"); $("#scrim").classList.add("show"); });
$("#closeSidebar").addEventListener("click", closeOverlays);
$("#scrim").addEventListener("click", closeOverlays);
$("#customerImage").addEventListener("click", () => $("#customerFileInput").click());
$("#customerFileInput").addEventListener("change", async () => {
  const file = $("#customerFileInput").files[0];
  if (!file) return;
  try { state.attachments.push(await uploadCustomerFile(file)); renderCustomerAttachments(); }
  catch (error) { showToast(error.message); }
  $("#customerFileInput").value = "";
});
const customerEmojis = ["😀", "😄", "😊", "🙂", "🥳", "😍", "🤔", "😅", "👍", "👌", "🙏", "🤝", "👏", "🎉", "❤️", "🌟", "✅", "💡", "📦", "💬"];
$("#customerEmojiGrid").innerHTML = customerEmojis.map(emoji => `<button type="button" aria-label="插入 ${emoji}">${emoji}</button>`).join("");
$("#customerEmojiGrid").querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
  input.value += button.textContent; resizeInput(); toggleCustomerEmoji(false); input.focus();
}));
$("#customerEmoji").addEventListener("click", () => toggleCustomerEmoji());
$("#closeCustomerEmoji").addEventListener("click", () => toggleCustomerEmoji(false));
document.addEventListener("click", event => {
  if (!$("#customerEmojiPicker").hidden && !event.target.closest("#customerEmojiPicker") && !event.target.closest("#customerEmoji")) toggleCustomerEmoji(false);
});
document.addEventListener("keydown", event => { if (event.key === "Escape") toggleCustomerEmoji(false); });

initialize();
