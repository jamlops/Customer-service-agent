const state = {
  status: "waiting", tickets: [], selected: null, selectedData: null,
  search: "", view: "tickets", attachments: [], replyTo: null,
  messageCounts: {}, messageMap: {}, autoSuggest: true, poll: null,
  dashboardData: null, chartResizeTimer: null, aiSettings: null,
};
const $ = (selector) => document.querySelector(selector);
const icon = (name) => `<i data-lucide="${name}"></i>`;
const esc = (value) => { const node = document.createElement("div"); node.textContent = value ?? ""; return node.innerHTML; };
const icons = () => window.lucide?.createIcons({ attrs: { "aria-hidden": "true" } });
const time = (value) => new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value * 1000));
const emojis = ["😀", "😊", "🙂", "👍", "🙏", "🎉", "❤️", "👌", "🤝", "📦", "✅", "🌟", "💡", "😄", "🥳", "💬"];
const tagLabels = { high_risk: "高风险用户", violation: "有违规用户", trusted: "信誉良好用户", vip: "VIP用户", enterprise: "企业客户" };

function toast(message) { const node = $("#toast"); node.textContent = message; node.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("show"), 2200); }
async function request(url, options) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(data.detail || "请求失败"); return data; }
function playNotice() { try { const C = window.AudioContext || window.webkitAudioContext; const ctx = new C(); const osc = ctx.createOscillator(); const gain = ctx.createGain(); osc.frequency.setValueAtTime(660, ctx.currentTime); osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + .1); gain.gain.setValueAtTime(.0001, ctx.currentTime); gain.gain.exponentialRampToValueAtTime(.08, ctx.currentTime + .01); gain.gain.exponentialRampToValueAtTime(.0001, ctx.currentTime + .18); osc.connect(gain); gain.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + .2); } catch (_) {} }

async function loadTickets() {
  if (state.view !== "tickets") return;
  try {
    const statusQuery = state.search ? "" : `status=${state.status}&`;
    const data = await request(`/admin/api/tickets?${statusQuery}q=${encodeURIComponent(state.search)}`);
    state.tickets = data.tickets;
    $("#waitingCount").textContent = data.counts.waiting; $("#activeCount").textContent = data.counts.active; $("#closedCount").textContent = data.counts.closed; $("#navWaiting").textContent = data.counts.waiting;
    renderTickets(); if (state.selected) await openTicket(state.selected, false);
  } catch (error) { toast(error.message); }
}

function renderTickets() {
  const list = $("#ticketList");
  list.innerHTML = state.tickets.length ? state.tickets.map((ticket) => {
    const tags = ticket.customer?.tags || [];
    return `<button class="ticket-card ${ticket.ticket_id === state.selected ? "active" : ""}" data-id="${ticket.ticket_id}"><div class="ticket-card-head"><strong>${esc(ticket.title)}</strong><time>${time(ticket.updated_at)}</time></div><p>${esc(ticket.last_message)}</p><footer><span class="ticket-status ${ticket.status}">${ticket.status === "waiting" ? "Agent 托管" : ticket.status === "active" ? esc(ticket.assigned_to) : "已结束"}</span><span>${tags.map(tag => esc(tagLabels[tag])).join(" · ") || `${ticket.turns} 轮`}</span></footer></button>`;
  }).join("") : `<div class="admin-empty">${state.search ? "没有匹配的工单" : "当前队列暂无工单"}</div>`;
  list.querySelectorAll("[data-id]").forEach((button) => button.addEventListener("click", () => openTicket(button.dataset.id)));
}

function messageHtml(message, ticket) {
  const kind = message.role === "user" ? "user" : message.name === "human" ? "human" : "agent";
  const who = kind === "user" ? "客户" : kind === "human" ? (message.meta?.operator || ticket.assigned_to || "人工客服") : "智能 Agent";
  const quote = message.meta?.reply_to ? `<span class="message-quote">回复：${esc(message.meta.reply_to.content)}</span>` : "";
  const attachments = (message.meta?.attachments || []).map(item => `<a href="${esc(item.url)}" target="_blank"><img src="${esc(item.url)}" alt="${esc(item.name || "图片")}"></a>`).join("");
  state.messageMap[message.id] = message;
  return `<article class="admin-message ${kind}" data-message-id="${esc(message.id)}"><header>${esc(who)} · ${time(message.timestamp)}</header><div class="content">${quote}${esc(message.content)}${attachments ? `<div class="message-attachments">${attachments}</div>` : ""}</div></article>`;
}

async function openTicket(id, showMobile = true) {
  try {
    const data = await request(`/admin/api/tickets/${id}`); const previous = state.messageCounts[id]; const latest = data.session.messages.at(-1);
    const newCustomer = previous !== undefined && data.session.messages.length > previous && latest?.role === "user";
    state.messageCounts[id] = data.session.messages.length; state.selected = id; state.selectedData = data; state.messageMap = {}; renderTickets();
    const { ticket, session, customer } = data;
    $("#workbenchEmpty").hidden = true; $("#dashboardView").hidden = true; $("#workbenchContent").hidden = false; if (showMobile) $(".workbench").classList.add("open");
    $("#caseTitle").textContent = ticket.title; $("#caseMeta").textContent = `${ticket.ticket_id} · ${time(ticket.created_at)} · ${ticket.session_id}`;
    $("#casePriority").textContent = ticket.priority === "high" ? "高优先级" : "普通"; $("#casePriority").className = `priority ${ticket.priority === "high" ? "high" : ""}`;
    const active = ticket.status === "active"; $("#claimTicket").hidden = ticket.status !== "waiting"; $("#closeTicket").hidden = ticket.status === "closed"; $("#adminReply").disabled = !active; $("#adminSend").disabled = !active;
    $("#adminMessages").innerHTML = session.messages.map((message) => messageHtml(message, ticket)).join(""); $("#adminMessages").scrollTop = $("#adminMessages").scrollHeight;
    $("#caseDetails").innerHTML = `<div><dt>客户</dt><dd>${esc(ticket.user_id)}</dd></div><div><dt>状态</dt><dd>${ticket.status}</dd></div><div><dt>负责人</dt><dd>${esc(ticket.assigned_to || "待分配")}</dd></div><div><dt>对话轮次</dt><dd>${session.turns}</dd></div>`;
    $("#adminSlots").innerHTML = Object.entries(session.slots).map(([key, value]) => `<span class="slot">${esc(key)} · ${esc(value)}</span>`).join("") || '<span class="empty-detail">暂无槽位</span>';
    $("#adminSummary").textContent = session.summary || "尚未生成"; $("#customerNote").value = customer.note || "";
    $("#customerTags").innerHTML = Object.entries(tagLabels).map(([key, label]) => `<button class="customer-tag ${(customer.tags || []).includes(key) ? "active" : ""}" data-tag="${key}">${label}</button>`).join("");
    bindMessageActions(); bindTags(); icons(); pollPresence(); if (newCustomer) { playNotice(); if (state.autoSuggest) generateSuggestion(false); }
  } catch (error) { toast(error.message); }
}

function bindMessageActions() {
  document.querySelectorAll(".admin-message").forEach((node) => node.addEventListener("click", (event) => {
    event.stopPropagation(); document.querySelectorAll(".admin-message.selected").forEach(item => item.classList.remove("selected")); node.classList.add("selected");
    const rect = node.querySelector(".content").getBoundingClientRect(); const menu = $("#messageMenu"); menu.dataset.messageId = node.dataset.messageId; menu.hidden = false; menu.style.left = `${Math.min(rect.left, innerWidth - 130)}px`; menu.style.top = `${Math.max(8, rect.top - 38)}px`; icons();
  }));
}
function bindTags() { document.querySelectorAll("[data-tag]").forEach(button => button.addEventListener("click", () => button.classList.toggle("active"))); }

async function saveProfile() { if (!state.selectedData) return; const tags = [...document.querySelectorAll("[data-tag].active")].map(item => item.dataset.tag); try { await request(`/admin/api/customers/${encodeURIComponent(state.selectedData.ticket.user_id)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note: $("#customerNote").value, tags }) }); toast("客户备注与标记已保存"); await openTicket(state.selected, false); } catch (error) { toast(error.message); } }
async function claim() { try { const id = state.selected; await request(`/admin/api/tickets/${id}/claim`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operator: "客服 001" }) }); state.status = "active"; selectStatus("active"); await loadTickets(); await openTicket(id, false); toast("已接入客户会话"); } catch (error) { toast(error.message); } }

async function uploadFile(file) { if (file.size > 5 * 1024 * 1024) throw new Error("图片不能超过 5 MB"); const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); }); return request("/api/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: file.name, mime_type: file.type, data }) }); }
function renderAttachments() { $("#adminAttachmentPreview").innerHTML = state.attachments.map((item, index) => `<img src="${esc(item.url)}" alt="${esc(item.name)}"><span>${esc(item.name)}</span><button type="button" data-remove-attachment="${index}">移除</button>`).join(""); document.querySelectorAll("[data-remove-attachment]").forEach(button => button.addEventListener("click", () => { state.attachments.splice(Number(button.dataset.removeAttachment), 1); renderAttachments(); })); }
async function reply(event) { event.preventDefault(); const message = $("#adminReply").value.trim(); if (!message && !state.attachments.length) return; try { await request(`/admin/api/tickets/${state.selected}/reply`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, operator: "客服 001", attachments: state.attachments, reply_to: state.replyTo }) }); $("#adminReply").value = ""; state.attachments = []; state.replyTo = null; renderAttachments(); renderReplyReference(); setTyping(false); await openTicket(state.selected, false); } catch (error) { toast(error.message); } }
async function closeTicket() { try { await request(`/admin/api/tickets/${state.selected}/close`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operator: "客服 001" }) }); state.status = "closed"; selectStatus("closed"); await loadTickets(); toast("会话已结束并恢复 Agent 服务"); } catch (error) { toast(error.message); } }
function renderReplyReference() { const node = $("#adminReplyReference"); node.hidden = !state.replyTo; node.innerHTML = state.replyTo ? `<strong>回复 ${esc(state.replyTo.author)}</strong><span>${esc(state.replyTo.content)}</span><button type="button" id="cancelReply">取消</button>` : ""; $("#cancelReply")?.addEventListener("click", () => { state.replyTo = null; renderReplyReference(); }); }

async function generateSuggestion(showError = true) { if (!state.selected) return; const panel = $("#aiSuggestion"); panel.hidden = false; $("#aiSuggestionTitle").textContent = "AI 预回答"; $("#aiModel").textContent = ""; $("#aiSuggestionText").textContent = "AI 正在结合上下文生成…"; try { const data = await request(`/admin/api/tickets/${state.selected}/suggest`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instruction: "" }) }); $("#aiSuggestionText").textContent = data.suggestion; $("#aiSuggestionTitle").textContent = `${data.provider_name}预回答`; $("#aiModel").textContent = data.agent_name ? `${data.model} · ${data.agent_name}` : data.model; $("#aiModel").title = [data.route_reason, data.tools_used?.length ? `工具：${data.tools_used.join("、")}` : ""].filter(Boolean).join("\n"); } catch (error) { panel.hidden = true; if (showError) toast(error.message); } }
async function setTyping(typing = true) { if (!state.selectedData) return; fetch(`/presence/${state.selectedData.ticket.session_id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: "operator", typing }) }).catch(() => {}); }
async function pollPresence() { if (!state.selectedData) return; try { const data = await request(`/presence/${state.selectedData.ticket.session_id}`); $("#customerTyping").textContent = data.customer_typing ? "客户正在输入…" : ""; } catch (_) {} }

function chartScaleMax(value) {
  if (value <= 4) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = Math.max(1, Math.ceil(value / magnitude / 4) * magnitude);
  return Math.ceil(value / step) * step;
}

function renderWeeklyChart(series) {
  const maxValue = Math.max(0, ...series.flatMap(item => [item.created, item.completed]));
  const scaleMax = chartScaleMax(maxValue);
  $("#weeklyChart").innerHTML = `
    <div class="bar-scale" aria-hidden="true"><span>${scaleMax}</span><span>${Math.round(scaleMax / 2)}</span><span>0</span></div>
    <div class="bar-plot">
      ${series.map(item => `<div class="bar-group">
        <div class="bar-pair">
          <div class="chart-bar created" style="height:${item.created / scaleMax * 100}%" title="${esc(item.label)} 新增 ${item.created} 单"><span>${item.created || ""}</span></div>
          <div class="chart-bar completed" style="height:${item.completed / scaleMax * 100}%" title="${esc(item.label)} 完成 ${item.completed} 单"><span>${item.completed || ""}</span></div>
        </div>
        <span class="bar-label">${esc(item.label)}</span>
      </div>`).join("")}
      ${maxValue === 0 ? '<span class="chart-empty">最近 7 天暂无工单</span>' : ""}
    </div>`;
}

function renderLineChart(selector, series, labelKey, valueKey, ariaLabel, lineClass) {
  const container = $(selector);
  const width = Math.max(300, Math.floor(container.clientWidth || 520));
  const height = 218;
  const margin = { top: 20, right: 16, bottom: 38, left: 42 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maxValue = Math.max(0, ...series.map(item => Number(item[valueKey]) || 0));
  const scaleMax = chartScaleMax(maxValue);
  const x = index => margin.left + index * plotWidth / Math.max(1, series.length - 1);
  const y = value => margin.top + plotHeight - value / scaleMax * plotHeight;
  const points = series.map((item, index) => `${x(index)},${y(item[valueKey])}`).join(" ");
  const tickCount = width < 420 ? 4 : 6;
  const xTicks = [...new Set(Array.from({ length: tickCount }, (_, index) =>
    Math.round(index * (series.length - 1) / Math.max(1, tickCount - 1))))];
  const yTicks = Array.from({ length: 5 }, (_, index) => ({
    value: Math.round(scaleMax * (4 - index) / 4),
    y: margin.top + index * plotHeight / 4,
  }));
  const peakIndex = series.findIndex(item => item[valueKey] === maxValue);
  container.innerHTML = `<svg class="chart-svg ${lineClass}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(ariaLabel)}">
    <title>${esc(ariaLabel)}</title>
    <desc>最高值 ${maxValue} 单，共 ${series.length} 个统计点。</desc>
    <rect class="chart-frame" x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${plotHeight}"></rect>
    ${yTicks.map(tick => `<line class="grid-line" x1="${margin.left}" y1="${tick.y}" x2="${width - margin.right}" y2="${tick.y}"></line><text class="axis-tick" x="${margin.left - 9}" y="${tick.y + 4}" text-anchor="end">${tick.value}</text>`).join("")}
    ${xTicks.map(index => `<text class="axis-tick" x="${x(index)}" y="${height - 15}" text-anchor="middle">${esc(series[index][labelKey])}</text>`).join("")}
    <text class="axis-title" x="${margin.left + plotWidth / 2}" y="${height - 1}" text-anchor="middle">时间</text>
    <text class="axis-title" x="12" y="${margin.top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 12 ${margin.top + plotHeight / 2})">工单量</text>
    <polyline class="series-line" points="${points}"></polyline>
    ${series.map((item, index) => `<circle class="series-point ${index === peakIndex && maxValue > 0 ? "peak" : ""}" cx="${x(index)}" cy="${y(item[valueKey])}" r="${index === peakIndex && maxValue > 0 ? 4 : 2.5}"><title>${esc(item[labelKey])}：${item[valueKey]} 单</title></circle>`).join("")}
    ${maxValue === 0 ? `<text class="empty-line-label" x="${margin.left + plotWidth / 2}" y="${margin.top + plotHeight / 2}" text-anchor="middle">暂无工单数据</text>` : ""}
  </svg>`;
}

function renderDashboardCharts(data) {
  renderWeeklyChart(data.weekly_series);
  renderLineChart("#hourlyChart", data.hourly_series, "label", "count", "24 小时工单创建量折线图", "hourly-line");
  renderLineChart("#monthlyChart", data.monthly_series, "label", "count", "本月每日新增工单折线图", "monthly-line");
  $("#weeklyPeak").textContent = data.peaks.weekly_day.count ? `峰值 ${data.peaks.weekly_day.label} · ${data.peaks.weekly_day.count} 单` : "近 7 天暂无工单";
  $("#hourlyPeak").textContent = data.peaks.hour_range.count ? `高峰 ${data.peaks.hour_range.label} · ${data.peaks.hour_range.count} 单` : "暂无高峰";
  $("#monthlyPeak").textContent = data.peaks.monthly_day.count ? `峰值 ${data.peaks.monthly_day.label} · ${data.peaks.monthly_day.count} 单` : "本月暂无工单";
}

async function loadDashboard() {
  try {
    const data = await request("/admin/api/dashboard");
    state.dashboardData = data;
    const metrics = [["当前工单", data.waiting + data.active], ["待分配", data.waiting], ["处理中", data.active], ["累计完成", data.completed], ["今日完成", data.today_completed], ["完成率", `${data.completion_rate}%`], ["平均处理时长", `${data.average_handle_minutes} 分钟`]];
    $("#metricGrid").innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
    renderDashboardCharts(data);
    const max = Math.max(1, ...Object.values(data.tag_counts));
    $("#tagChart").innerHTML = Object.entries(data.tag_counts).map(([key, count]) => `<div class="tag-bar"><span>${esc(data.tag_labels[key])}</span><div class="tag-track"><div class="tag-fill" style="width:${count / max * 100}%"></div></div><strong>${count}</strong></div>`).join("");
  } catch (error) { toast(error.message); }
}

function renderAISettings(data) {
  state.aiSettings = data;
  $("#deepseekEnabled").checked = data.deepseek_enabled;
  $("#deepseekEnabled").disabled = false;
  $("#deepseekSwitchLabel").textContent = data.deepseek_enabled ? "已启用" : "已停用";
  $("#deepseekKeyStatus").textContent = data.deepseek_api_configured ? "DeepSeek API Key 已配置" : "DeepSeek API Key 尚未配置";
  $("#deepseekKeyStatus").classList.toggle("warning", !data.deepseek_api_configured);
  $("#suggestionProviders").innerHTML = data.providers.map(provider => `
    <label class="provider-option ${provider.id === data.suggestion_provider ? "selected" : ""}">
      <input type="radio" name="suggestionProvider" value="${esc(provider.id)}" ${provider.id === data.suggestion_provider ? "checked" : ""}>
      <span><strong>${esc(provider.name)}</strong><small>${esc(provider.description)}</small></span>
      <b>${esc(provider.model)}</b>
    </label>`).join("");
  document.querySelectorAll('[name="suggestionProvider"]').forEach(inputNode => inputNode.addEventListener("change", () => {
    if (inputNode.checked) saveAISettings({ suggestion_provider: inputNode.value });
  }));
  const effective = data.providers.find(provider => provider.id === data.effective_suggestion_provider);
  $("#effectiveProvider").textContent = !data.deepseek_enabled && data.suggestion_provider === "deepseek"
    ? `DeepSeek 总开关已关闭，当前预回答暂由本地模型 ${effective?.model || ""} 生成。`
    : `当前预回答使用 ${effective?.name || "所选模型"} · ${effective?.model || ""}`;
  icons();
}

async function loadAISettings() {
  try { renderAISettings(await request("/admin/api/ai-settings")); }
  catch (error) { toast(error.message); }
}

async function saveAISettings(changes) {
  if (!state.aiSettings) return;
  const previous = state.aiSettings;
  const payload = {
    deepseek_enabled: changes.deepseek_enabled ?? previous.deepseek_enabled,
    suggestion_provider: changes.suggestion_provider ?? previous.suggestion_provider,
  };
  $("#deepseekEnabled").disabled = true;
  document.querySelectorAll('[name="suggestionProvider"]').forEach(inputNode => { inputNode.disabled = true; });
  $("#settingsSaveState").textContent = "正在保存…";
  try {
    const updated = await request("/admin/api/ai-settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    renderAISettings(updated);
    $("#settingsSaveState").textContent = "设置已保存并立即生效";
    toast("AI 设置已更新");
  } catch (error) {
    renderAISettings(previous);
    $("#settingsSaveState").textContent = "保存失败";
    toast(error.message);
  }
}

function selectStatus(status) { state.status = status; document.querySelectorAll("[data-status]").forEach(button => button.classList.toggle("active", button.dataset.status === status)); }
function showView(view) { state.view = view; const dashboard = view === "dashboard"; const settings = view === "settings"; const fullView = dashboard || settings; $(".admin-shell").classList.toggle("dashboard-mode", fullView); $("#dashboardNav").classList.toggle("active", dashboard); $("#aiSettingsNav").classList.toggle("active", settings); $("#ticketsNav").classList.toggle("active", view === "tickets"); $("#dashboardView").hidden = !dashboard; $("#aiSettingsView").hidden = !settings; $("#workbenchContent").hidden = fullView || !state.selected; $("#workbenchEmpty").hidden = fullView || Boolean(state.selected); if (dashboard) loadDashboard(); if (settings) loadAISettings(); }

$("#adminEmojiPicker").innerHTML = emojis.map(emoji => `<button type="button">${emoji}</button>`).join(""); $("#adminEmojiPicker").querySelectorAll("button").forEach(button => button.addEventListener("click", () => { $("#adminReply").value += button.textContent; $("#adminEmojiPicker").hidden = true; $("#adminReply").focus(); setTyping(); }));
document.querySelectorAll("[data-status]").forEach(button => button.addEventListener("click", () => { showView("tickets"); selectStatus(button.dataset.status); state.selected = null; $("#workbenchContent").hidden = true; $("#workbenchEmpty").hidden = false; loadTickets(); }));
$("#ticketSearch").addEventListener("input", () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => { state.search = $("#ticketSearch").value; loadTickets(); }, 250); });
$("#dashboardNav").addEventListener("click", () => showView("dashboard")); $("#ticketsNav").addEventListener("click", () => showView("tickets")); $("#aiSettingsNav").addEventListener("click", () => showView("settings")); $("#refreshDashboard").addEventListener("click", loadDashboard); $("#refreshAISettings").addEventListener("click", loadAISettings); $("#refreshTickets").addEventListener("click", loadTickets); $("#claimTicket").addEventListener("click", claim); $("#closeTicket").addEventListener("click", closeTicket); $("#adminReplyForm").addEventListener("submit", reply); $("#saveProfile").addEventListener("click", saveProfile);
$("#deepseekEnabled").addEventListener("change", () => saveAISettings({ deepseek_enabled: $("#deepseekEnabled").checked }));
$("#adminImage").addEventListener("click", () => $("#adminFileInput").click()); $("#adminFileInput").addEventListener("change", async () => { const file = $("#adminFileInput").files[0]; if (!file) return; try { state.attachments.push(await uploadFile(file)); renderAttachments(); } catch (error) { toast(error.message); } $("#adminFileInput").value = ""; }); $("#adminEmoji").addEventListener("click", () => $("#adminEmojiPicker").hidden = !$("#adminEmojiPicker").hidden); $("#requestAI").addEventListener("click", () => generateSuggestion(true));
$("#useSuggestion").addEventListener("click", () => { $("#adminReply").value = $("#aiSuggestionText").textContent; $("#aiSuggestion").hidden = true; $("#adminReply").focus(); }); $("#discardSuggestion").addEventListener("click", () => $("#aiSuggestion").hidden = true); $("#adminReply").addEventListener("input", () => { clearTimeout(state.typingTimer); setTyping(true); state.typingTimer = setTimeout(() => setTyping(false), 1600); });
$("#replyMessage").addEventListener("click", () => { const message = state.messageMap[$("#messageMenu").dataset.messageId]; if (message) { state.replyTo = { id: message.id, content: message.content.slice(0, 120), author: message.role === "user" ? "客户" : "客服" }; renderReplyReference(); $("#adminReply").focus(); } $("#messageMenu").hidden = true; }); $("#copyMessage").addEventListener("click", async () => { const message = state.messageMap[$("#messageMenu").dataset.messageId]; if (message) { await navigator.clipboard.writeText(message.content); toast("消息已复制"); } $("#messageMenu").hidden = true; }); document.addEventListener("click", (event) => { if (!$("#messageMenu").contains(event.target) && !event.target.closest(".admin-message")) $("#messageMenu").hidden = true; });

window.addEventListener("resize", () => {
  clearTimeout(state.chartResizeTimer);
  state.chartResizeTimer = setTimeout(() => {
    if (state.view === "dashboard" && state.dashboardData) renderDashboardCharts(state.dashboardData);
  }, 120);
});

icons(); request("/health").then(data => { state.autoSuggest = data.local_llm?.auto_suggest !== false; }).catch(() => {}); loadTickets(); state.poll = setInterval(loadTickets, 3000); state.presencePoll = setInterval(pollPresence, 1000);
