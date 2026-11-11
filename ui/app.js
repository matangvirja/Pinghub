/* ==========================================================================
   PingHub UI logic — vanilla JS, no framework, no build step.
   Talks to the gateway through nginx at /api and to the ops service at /ops.
   ========================================================================== */

const API = "/api";
const OPS = "/ops";
const TOKEN_KEY = "pinghub.session.token.v1";

const state = {
  me: null,
  authTab: "login",
  view: "chat",
  conversations: [],
  activePeer: null,
  messages: [],
  lastStatuses: {},
  unreadReady: false,
  lastUnread: {},
  markingRead: false,
  lastMarkReadAt: 0,
  sending: false,
  history: null,
  system: null,
  loadtest: null,
  queueSeries: [],
  pollTimer: null,
  systemTimer: null,
  share: null,
  confirm: null,
};

let sessionExpiredHandled = false;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/* --------------------------------------------------------------------------
   Small utilities
   -------------------------------------------------------------------------- */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function initials(name) {
  const parts = String(name || "?").trim().split(/[\s_.-]+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function hueFor(value) {
  let hash = 0;
  for (const ch of String(value || "pinghub")) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return 150 + (hash % 120); // teal -> blue -> violet range
}

function avatarMarkup(user, extra = "") {
  const name = user?.display_name || user?.username || "?";
  return `<span class="avatar ${extra}" style="--hue:${hueFor(user?.username || name)}">${esc(initials(name))}</span>`;
}

function paintAvatar(element, user) {
  if (!element) return;
  const name = user?.display_name || user?.username || "?";
  element.style.setProperty("--hue", hueFor(user?.username || name));
  element.textContent = initials(name);
}

function displayName(user) {
  return user?.display_name || user?.username || "Unknown";
}

function icon(name, cls = "") {
  return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}

function parseDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function fmtTime(value) {
  const date = parseDate(value);
  if (!date) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDay(value) {
  const date = parseDate(value);
  if (!date) return "";
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function fmtDateTime(value) {
  const date = parseDate(value);
  if (!date) return "—";
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })} · ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function timeAgo(value) {
  const date = parseDate(value);
  if (!date) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function statusPill(status) {
  return `<span class="status-pill st-${esc(status)}">${esc(status)}</span>`;
}

async function api(path, options = {}, base = API) {
  const { method = "GET", body, headers = {}, skipAuthRedirect = false } = options;
  const init = { method, headers: { ...headers } };
  const token = getToken();
  if (token) init.headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(base + path, init);
  } catch (error) {
    const wrapped = new Error("Cannot reach the server. Is the stack running?");
    wrapped.cause = error;
    throw wrapped;
  }

  const text = await response.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }

  if (!response.ok) {
    const message = data.detail || data.message || `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.data = data;
    if (response.status === 401 && state.me && !skipAuthRedirect) {
      handleSessionExpired(message);
    }
    throw error;
  }
  return { data, headers: response.headers, status: response.status };
}

function friendlyError(error) {
  if (error.status === 503 || /message-api unavailable/i.test(error.message || "")) {
    return "The backend is still starting up (message-api is not ready yet). "
      + "Wait 15–20 seconds and try again. If it keeps happening, run "
      + "\"docker compose ps\" and make sure message-api is healthy.";
  }
  return error.message;
}

/* --------------------------------------------------------------------------
   Session storage. The bearer token lives in sessionStorage so each browser
   tab can be a different employee (the two-person read-receipt demo). Ticking
   "Remember me" moves it to localStorage for this device.
   -------------------------------------------------------------------------- */
function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY);
}

function setToken(token, remember) {
  if (remember) {
    localStorage.setItem(TOKEN_KEY, token);
    sessionStorage.removeItem(TOKEN_KEY);
  } else {
    sessionStorage.setItem(TOKEN_KEY, token);
    localStorage.removeItem(TOKEN_KEY);
  }
}

function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_KEY);
}

/* --------------------------------------------------------------------------
   Boot
   -------------------------------------------------------------------------- */
document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  if (!getToken()) {
    showOnboarding();
    return;
  }
  try {
    const response = await api("/auth/me", { skipAuthRedirect: true });
    await enterApp(response.data.user);
  } catch {
    clearToken();
    showOnboarding();
  }
});

function showOnboarding(message) {
  state.me = null;
  sessionExpiredHandled = false;
  $("#app").classList.add("hidden");
  $("#onboarding").classList.remove("hidden");
  // A sign-in failure or an expired session always belongs on the Sign in tab.
  const tab = message || state.authTab !== "register" ? "login" : "register";
  switchAuthTab(tab);
  if (message) {
    const box = $("#loginError");
    box.textContent = message;
    box.classList.remove("hidden");
  }
  const target = tab === "register" ? $("#registerUsername") : $("#loginUsername");
  setTimeout(() => target?.focus(), 60);
}

function handleSessionExpired(message) {
  if (sessionExpiredHandled) return;
  sessionExpiredHandled = true;
  clearToken();
  state.me = null;
  state.pollTimer && clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.systemTimer && clearInterval(state.systemTimer);
  state.systemTimer = null;
  showOnboarding(message || "Your session expired. Sign in again.");
}

async function enterApp(user) {
  state.me = user;
  state.authTab = "login";
  sessionExpiredHandled = false;
  $("#onboarding").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#loginForm")?.reset();
  $("#registerForm")?.reset();
  renderMe();
  switchView("chat");
  await loadConversations({ silent: true }).catch(() => {});
  ensurePolling();
}

function renderMe() {
  paintAvatar($("#railAvatar"), state.me);
  $("#myPublicId").textContent = state.me.public_id || "—";
  $("#myUsername").textContent = `@${state.me.username}`;
  $("#emptyMyId").textContent = state.me.public_id || "—";
  const alertsBtn = $("#enableAlertsBtn");
  if ("Notification" in window && Notification.permission === "granted") {
    alertsBtn.innerHTML = `${icon("bell")} Desktop alerts enabled`;
  }
}

function ensurePolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(() => pollTick(), 2600);
  window.addEventListener("focus", () => pollTick());
}

async function pollTick() {
  if (!state.me || document.hidden) return;
  try { await loadConversations({ silent: true }); } catch { /* transient */ }
  if (state.view === "chat" && state.activePeer) {
    try { await loadThread({ silent: true }); } catch { /* transient */ }
  }
}

/* --------------------------------------------------------------------------
   Onboarding: sign in / activate
   -------------------------------------------------------------------------- */
function switchAuthTab(name) {
  state.authTab = name;
  $$("[data-auth-tab]").forEach((tab) => {
    const active = tab.dataset.authTab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#loginForm").classList.toggle("hidden", name !== "login");
  $("#registerForm").classList.toggle("hidden", name !== "register");
  $("#loginError").classList.add("hidden");
  $("#registerError").classList.add("hidden");
  setTimeout(() => {
    const target = name === "register" ? $("#registerUsername") : $("#loginUsername");
    target?.focus();
  }, 40);
}

async function handleLogin(event) {
  event.preventDefault();
  const username = $("#loginUsername").value.trim();
  const password = $("#loginPassword").value;
  const remember = $("#loginRemember").checked;
  const errorBox = $("#loginError");
  const button = $("#loginBtn");

  errorBox.classList.add("hidden");
  button.disabled = true;
  try {
    const response = await api("/auth/login", {
      method: "POST",
      body: { username, password, remember },
      skipAuthRedirect: true,
    });
    setToken(response.data.token, remember);
    $("#loginForm").reset();
    await enterApp(response.data.user);
    toast("Signed in", `Welcome back, ${displayName(response.data.user)}.`, "success");
  } catch (error) {
    errorBox.textContent = friendlyError(error);
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
}

async function handleRegister(event) {
  event.preventDefault();
  const username = $("#registerUsername").value.trim();
  const displayNameValue = $("#registerDisplay").value.trim();
  const password = $("#registerPassword").value;
  const signupCode = $("#registerCode").value;
  const remember = $("#registerRemember").checked;
  const errorBox = $("#registerError");
  const button = $("#registerBtn");

  errorBox.classList.add("hidden");
  button.disabled = true;
  try {
    const response = await api("/auth/register", {
      method: "POST",
      body: {
        username,
        password,
        signup_code: signupCode,
        display_name: displayNameValue || null,
        remember,
      },
      skipAuthRedirect: true,
    });
    setToken(response.data.token, remember);
    $("#registerForm").reset();
    await enterApp(response.data.user);
    const message = response.data.activated
      ? "Your existing profile is now protected by your new password. Your chats are still here."
      : "Your employee account is ready.";
    toast("Account ready", message, "success");
  } catch (error) {
    errorBox.textContent = friendlyError(error);
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------------------
   Conversations
   -------------------------------------------------------------------------- */
async function loadConversations({ silent = false } = {}) {
  if (!state.me) return;
  const response = await api(`/users/${encodeURIComponent(state.me.username)}/conversations`);
  const conversations = response.data.conversations || [];
  detectIncoming(conversations);
  state.conversations = conversations;
  renderConversations();
  if (silent) return;
}

function detectIncoming(conversations) {
  if (!state.unreadReady) {
    conversations.forEach((c) => { state.lastUnread[c.peer.username] = c.unread_count; });
    state.unreadReady = true;
    return;
  }

  let newCount = 0;
  let from = null;

  for (const conversation of conversations) {
    const previous = state.lastUnread[conversation.peer.username] || 0;
    const isOpen = state.activePeer?.username === conversation.peer.username;

    if (conversation.unread_count > previous && !isOpen) {
      newCount += conversation.unread_count - previous;
      from = conversation.peer;
    }
    state.lastUnread[conversation.peer.username] = conversation.unread_count;
  }

  if (newCount > 0 && from) {
    toast("New message", `${displayName(from)} sent you ${newCount} message${newCount > 1 ? "s" : ""}.`, "info");
    browserNotify(`PingHub · ${displayName(from)}`, `${newCount} new message${newCount > 1 ? "s" : ""}`);
  }
}

function renderConversations() {
  const list = $("#conversationList");
  const query = ($("#conversationSearch").value || "").toLowerCase().trim();
  const conversations = state.conversations.filter((c) =>
    !query ||
    displayName(c.peer).toLowerCase().includes(query) ||
    c.peer.username.toLowerCase().includes(query) ||
    (c.peer.public_id || "").toLowerCase().includes(query)
  );

  $("#conversationCount").textContent = String(state.conversations.length);

  if (!conversations.length) {
    list.innerHTML = `
      <div class="empty-list">
        <strong>${state.conversations.length ? "No matches" : "No conversations yet"}</strong>
        ${state.conversations.length ? "Try another search." : "Share your ID or start a chat with someone else's ID."}
      </div>`;
    return;
  }

  list.innerHTML = conversations.map((conversation) => {
    const peer = conversation.peer;
    const last = conversation.last_message;
    const preview = `${last.from_me ? '<span class="you">You: </span>' : ""}${esc(last.body || "")}`;
    const active = state.activePeer?.username === peer.username ? "active" : "";

    return `
      <div class="conversation ${active}" data-username="${esc(peer.username)}" role="button" tabindex="0"
           aria-label="Open chat with ${esc(displayName(peer))}">
        ${avatarMarkup(peer)}
        <span class="conv-body">
          <span class="conv-top">
            <strong>${esc(displayName(peer))}</strong>
            <time>${esc(timeAgo(last.created_at))}</time>
          </span>
          <span class="conv-preview">${preview}</span>
        </span>
        ${conversation.unread_count > 0 ? `<span class="unread-badge">${conversation.unread_count}</span>` : ""}
        <button class="conv-delete" type="button" data-delete-chat="${esc(peer.username)}"
                title="Delete chat" aria-label="Delete chat with ${esc(displayName(peer))}">${icon("trash")}</button>
      </div>`;
  }).join("");

  $$(".conversation", list).forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("[data-delete-chat]")) return;
      openChat(row.dataset.username);
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openChat(row.dataset.username);
      }
    });
  });

  $$("[data-delete-chat]", list).forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const peer = state.conversations.find(
        (c) => c.peer.username === button.dataset.deleteChat
      )?.peer;
      if (peer) askDeleteChat(peer);
    });
  });
}

/* --------------------------------------------------------------------------
   Delete a whole chat (per-user soft delete)
   -------------------------------------------------------------------------- */
function askDeleteChat(peer) {
  if (!peer) return;
  state.confirm = { action: "delete-chat", peer };
  $("#confirmTitle").textContent = `Delete chat with ${displayName(peer)}?`;
  $("#confirmText").textContent =
    "The conversation disappears from your Chats list. The other person keeps "
    + "their copy. If they message you again, the chat comes back.";
  $("#confirmOkBtn").textContent = "Delete chat";
  openModal("confirmModal");
  setTimeout(() => $("#confirmOkBtn").focus(), 50);
}

async function deleteChat(peer) {
  if (!state.me || !peer) return;
  const path = `/users/${encodeURIComponent(state.me.username)}/conversations/${encodeURIComponent(peer.username)}`;
  try {
    await api(path, { method: "DELETE" });

    state.conversations = state.conversations.filter(
      (conversation) => conversation.peer.username !== peer.username
    );
    delete state.lastUnread[peer.username];

    if (state.activePeer?.username === peer.username) {
      state.activePeer = null;
      state.messages = [];
      state.lastStatuses = {};
      renderMessages();
      $("#threadPanel").classList.add("hidden");
      $("#chatEmpty").classList.remove("hidden");
      $("#view-chat").classList.remove("mobile-thread");
    }

    renderConversations();
    toast(
      "Chat deleted",
      `${displayName(peer)} is no longer in your Chats list. They still have their copy.`,
      "success"
    );
  } catch (error) {
    toast("Could not delete chat", error.message, "error");
  }
}

/* --------------------------------------------------------------------------
   Chat thread
   -------------------------------------------------------------------------- */
async function lookupUser(identifier) {
  const response = await api(`/users/${encodeURIComponent(identifier)}`);
  return response.data;
}

async function openChat(username) {
  try {
    let peer = state.conversations.find((c) => c.peer.username === username)?.peer;
    if (!peer) peer = await lookupUser(username);

    state.activePeer = peer;
    state.messages = [];
    state.lastStatuses = {};
    state.lastMarkReadAt = 0;

    $("#chatEmpty").classList.add("hidden");
    $("#threadPanel").classList.remove("hidden");
    $("#view-chat").classList.add("mobile-thread");
    renderThreadHeader();

    await loadThread({ initial: true, forceScroll: true });
  } catch (error) {
    toast("Could not open chat", error.message, "error");
  }
}

function renderThreadHeader() {
  if (!state.activePeer) return;
  paintAvatar($("#threadAvatar"), state.activePeer);
  $("#threadName").textContent = displayName(state.activePeer);
  $("#threadId").textContent = state.activePeer.public_id || state.activePeer.username;
}

async function loadThread({ silent = false, initial = false, forceScroll = false } = {}) {
  if (!state.me || !state.activePeer) return;

  const response = await api(
    `/users/${encodeURIComponent(state.me.username)}/conversations/${encodeURIComponent(state.activePeer.username)}`
  );
  const data = response.data;

  let newlyRead = 0;
  const nextStatuses = {};

  for (const message of data.messages) {
    const previous = state.lastStatuses[message.id];
    if (!initial && message.is_mine && message.status === "READ" && previous && previous !== "READ") {
      newlyRead += 1;
    }
    nextStatuses[message.id] = message.status;
  }

  state.activePeer = data.peer;
  state.messages = data.messages;
  state.lastStatuses = nextStatuses;

  renderThreadHeader();
  renderMessages({ forceScroll: forceScroll || initial });

  if (newlyRead > 0) {
    toast(
      "Read receipt",
      `${displayName(state.activePeer)} read ${newlyRead} of your message${newlyRead > 1 ? "s" : ""}.`,
      "read"
    );
    browserNotify("PingHub · read receipt", `${displayName(state.activePeer)} read your message`);
  }

  const unreadIncoming = data.messages.some(
    (message) => !message.is_mine && ["QUEUED", "PROCESSING", "DELIVERED"].includes(message.status)
  );
  if (unreadIncoming && document.visibilityState === "visible") {
    markRead();
  } else if (!silent && !data.messages.length) {
    renderMessages();
  }
}

function tickMarkup(message) {
  switch (message.status) {
    case "QUEUED":
      return `${icon("clock", "tick queued")}<span>Queued</span>`;
    case "PROCESSING":
      return `${icon("clock", "tick processing spin")}<span>Sending</span>`;
    case "DELIVERED":
      return `${icon("check", "tick delivered")}<span>Delivered</span>`;
    case "READ":
      return `${icon("checkcheck", "tick read")}<span>Read${message.read_at ? ` · ${fmtTime(message.read_at)}` : ""}</span>`;
    case "FAILED":
      return `${icon("alert", "tick failed")}<span>Failed</span>`;
    default:
      return `<span>${esc(message.status || "")}</span>`;
  }
}

function renderMessages({ forceScroll = false } = {}) {
  const list = $("#messageList");
  if (!list) return;

  if (!state.messages.length) {
    list.innerHTML = `
      <div class="no-messages">
        <strong>No messages yet</strong>
        Say hello. Your message goes through the gateway and queue before a worker delivers it.
      </div>`;
    return;
  }

  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 140;
  let html = "";
  let currentDay = null;
  const byId = new Map();

  for (const message of state.messages) {
    byId.set(message.id, message);
    const day = fmtDay(message.created_at);
    if (day !== currentDay) {
      html += `<div class="day-sep">${esc(day)}</div>`;
      currentDay = day;
    }

    const failed = message.status === "FAILED";
    const errorText = message.error ? esc(message.error) : "The notification provider rejected this message after retries.";

    html += `
      <div class="msg ${message.is_mine ? "mine" : "theirs"}" data-id="${esc(message.id)}">
        <div>
          <div class="bubble">
            ${esc(message.body)}
            <div class="msg-meta">
              <span>${esc(fmtTime(message.created_at))}</span>
              ${message.is_mine ? tickMarkup(message) : ""}
            </div>
          </div>
          ${failed ? `
            <div class="msg-error">
              <span>${errorText}</span>
              <button type="button" data-resend="${esc(message.id)}">Resend</button>
            </div>` : ""}
        </div>
      </div>`;
  }

  list.innerHTML = html;
  list._messagesById = byId;

  if (forceScroll || nearBottom) {
    requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
  }

  $$("[data-resend]", list).forEach((button) => {
    button.addEventListener("click", () => {
      const message = byId.get(button.dataset.resend);
      if (message) {
        $("#messageInput").value = message.body;
        updateComposer();
        $("#messageInput").focus();
        sendMessage(message.body);
      }
    });
  });
}

async function markRead() {
  if (!state.me || !state.activePeer || state.markingRead) return;
  const now = Date.now();
  if (now - state.lastMarkReadAt < 1500) return;

  state.markingRead = true;
  state.lastMarkReadAt = now;

  try {
    await api(
      `/users/${encodeURIComponent(state.me.username)}/conversations/${encodeURIComponent(state.activePeer.username)}/read`,
      { method: "POST" }
    );

    const stamp = new Date().toISOString();
    state.messages = state.messages.map((message) => {
      if (!message.is_mine && message.status === "DELIVERED") {
        return { ...message, status: "READ", read_at: stamp };
      }
      return message;
    });
    state.messages.forEach((message) => { state.lastStatuses[message.id] = message.status; });
    renderMessages();
    await loadConversations({ silent: true });
  } catch {
    /* read receipts are best-effort; the next poll retries */
  } finally {
    state.markingRead = false;
  }
}

async function sendMessage(text) {
  if (!state.activePeer || !text.trim() || state.sending) return;
  const body = text.trim();
  const peer = state.activePeer;
  const input = $("#messageInput");
  state.sending = true;

  const temp = {
    id: `temp-${Date.now()}`,
    body,
    status: "QUEUED",
    created_at: new Date().toISOString(),
    is_mine: true,
    sender: state.me.username,
    receiver: peer.username,
  };
  state.messages.push(temp);
  input.value = "";
  updateComposer();
  renderMessages({ forceScroll: true });

  try {
    const response = await api("/messages", {
      method: "POST",
      body: { sender: state.me.username, receiver: peer.username, body },
    });

    const remaining = response.headers.get("X-RateLimit-Remaining");
    if (remaining !== null) showRateHint(Number(remaining));

    await new Promise((resolve) => setTimeout(resolve, 220));
    state.messages = state.messages.filter((message) => message.id !== temp.id);
    await loadThread({ silent: true, forceScroll: true });
    await loadConversations({ silent: true });
  } catch (error) {
    state.messages = state.messages.filter((message) => message.id !== temp.id);
    renderMessages();
    input.value = body;
    updateComposer();

    if (error.status === 429) {
      const wait = error.data?.retry_after_seconds || 1;
      $("#rateHint").textContent = `Rate limit reached. This protects the system — try again in ${wait}s.`;
      $("#rateHint").classList.remove("hidden");
      toast("Slow down for a second", `Your bucket is empty. Retry in about ${wait}s.`, "error");
      setTimeout(() => $("#rateHint").classList.add("hidden"), Math.max(2200, wait * 1000));
    } else {
      toast("Message not sent", error.message, "error");
    }
  } finally {
    state.sending = false;
  }
}

function showRateHint(remaining) {
  const hint = $("#rateHint");
  if (remaining <= 3) {
    hint.textContent = `⚡ ${remaining} request${remaining === 1 ? "" : "s"} left in your rate-limit bucket.`;
    hint.classList.remove("hidden");
    setTimeout(() => hint.classList.add("hidden"), 2600);
  }
}

function updateComposer() {
  const input = $("#messageInput");
  const length = input.value.length;
  $("#charCount").textContent = `${length} / 4000`;
  $("#sendBtn").disabled = length === 0 || !state.activePeer;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

/* --------------------------------------------------------------------------
   History
   -------------------------------------------------------------------------- */
async function loadHistory() {
  if (!state.me) return;
  const target = state.me.username;

  try {
    const response = await api(`/history?user=${encodeURIComponent(target)}&limit=50`);
    state.history = {
      target: state.me.username,
      messages: response.data.messages || [],
      cache: response.headers.get("X-Cache") || "—",
    };
    renderHistory();
  } catch (error) {
    toast("Could not load history", error.message, "error");
  }
}

function renderHistory() {
  if (!state.history) return;
  const perspective = state.history.target;
  const messages = state.history.messages;
  const meName = perspective;

  const total = messages.length;
  const delivered = messages.filter((m) => ["DELIVERED", "READ"].includes(m.status)).length;
  const read = messages.filter((m) => m.status === "READ").length;
  const failed = messages.filter((m) => m.status === "FAILED").length;

  $("#historyStats").innerHTML = `
    <div class="stat-card"><div class="stat-label">Messages</div><div class="stat-value">${total}</div><div class="stat-foot">latest 50 for @${esc(meName)}</div></div>
    <div class="stat-card accent"><div class="stat-label">Delivered</div><div class="stat-value">${delivered}</div><div class="stat-foot">reached the provider</div></div>
    <div class="stat-card blue"><div class="stat-label">Read</div><div class="stat-value">${read}</div><div class="stat-foot">opened by the recipient</div></div>
    <div class="stat-card red"><div class="stat-label">Failed</div><div class="stat-value">${failed}</div><div class="stat-foot">after all retries</div></div>
  `;

  const cacheBadge = $("#cacheBadge");
  cacheBadge.classList.remove("hidden", "hit", "miss");
  cacheBadge.textContent = `redis cache: ${state.history.cache}`;
  if (state.history.cache === "HIT") cacheBadge.classList.add("hit");
  if (state.history.cache === "MISS") cacheBadge.classList.add("miss");

  const query = $("#historySearch").value.toLowerCase().trim();
  const statusFilter = $("#historyStatusFilter").value;
  const directionFilter = $("#historyDirectionFilter").value;

  const filtered = messages.filter((message) => {
    const sent = message.sender === meName;
    const direction = sent ? "SENT" : "RECEIVED";
    if (directionFilter !== "ALL" && direction !== directionFilter) return false;
    if (statusFilter !== "ALL" && message.status !== statusFilter) return false;
    if (!query) return true;
    return (
      String(message.body || "").toLowerCase().includes(query) ||
      String(message.sender || "").toLowerCase().includes(query) ||
      String(message.receiver || "").toLowerCase().includes(query)
    );
  });

  const wrap = $("#historyTableWrap");
  if (!filtered.length) {
    wrap.innerHTML = `<div class="table-empty">No messages match this view.</div>`;
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Time</th><th>Direction</th><th>Person</th><th>Message</th><th>Status</th><th></th>
        </tr>
      </thead>
      <tbody>
        ${filtered.map((message) => {
          const sent = message.sender === meName;
          const peer = sent ? message.receiver : message.sender;
          return `
            <tr>
              <td class="muted" style="white-space:nowrap">${esc(fmtDateTime(message.created_at))}</td>
              <td><span class="dir-pill ${sent ? "sent" : "received"}">${sent ? "SENT" : "RECEIVED"}</span></td>
              <td><strong>${esc(peer)}</strong></td>
              <td class="msg-cell" title="${esc(message.body)}">${esc(message.body)}</td>
              <td>${statusPill(message.status)}</td>
              <td><button class="btn ghost small" data-open-peer="${esc(peer)}">Open</button></td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;

  $$("[data-open-peer]", wrap).forEach((button) => {
    button.addEventListener("click", async () => {
      switchView("chat");
      await openChat(button.dataset.openPeer);
    });
  });
}

/* --------------------------------------------------------------------------
   System & Load Lab
   -------------------------------------------------------------------------- */
async function loadSystem() {
  if (!state.me || state.view !== "system") return;
  try {
    const response = await api("/overview", {}, OPS);
    state.system = response.data;
    state.loadtest = response.data.loadtest;

    const queueDepth = response.data.queue?.messages ?? 0;
    state.queueSeries.push(queueDepth);
    if (state.queueSeries.length > 60) state.queueSeries.shift();

    renderSystem();
  } catch (error) {
    $("#systemBannerTitle").textContent = "Ops API unavailable";
    $("#systemBannerText").textContent = error.message;
    $("#systemBanner").className = "system-banner down";
  }
}

function renderSystem() {
  const data = state.system;
  if (!data) return;
  const health = data.health || {};
  const nodes = health.nodes || {};

  const down = Object.entries(nodes).filter(([, node]) => node.status !== "up");
  const banner = $("#systemBanner");
  if (health.status === "ok" && !down.length) {
    banner.className = "system-banner";
    $("#systemBannerTitle").textContent = "All systems operational";
    $("#systemBannerText").textContent = "Gateway, queue, workers, database and cache are healthy.";
  } else if (health.status === "down") {
    banner.className = "system-banner down";
    $("#systemBannerTitle").textContent = "System unavailable";
    $("#systemBannerText").textContent = health.error || "The gateway did not respond.";
  } else {
    banner.className = "system-banner degraded";
    $("#systemBannerTitle").textContent = "Degraded";
    $("#systemBannerText").textContent = `Not reporting up: ${down.map(([name]) => name).join(", ") || "unknown"}.`;
  }

  const order = ["gateway", "postgres", "redis", "rabbitmq", "message-api", "notification-mock", "worker"];
  $("#nodeGrid").innerHTML = order.map((name) => {
    const node = nodes[name] || { status: "down" };
    const up = node.status === "up";
    const detail = name === "worker"
      ? `${node.replicas ?? 0} replica${node.replicas === 1 ? "" : "s"}`
      : (up ? "healthy" : "down");
    return `
      <div class="node-card">
        <span class="node-dot ${up ? "up" : "down"}"></span>
        <div><div class="node-name">${esc(name)}</div><div class="node-detail">${esc(detail)}</div></div>
      </div>`;
  }).join("");

  const db = data.database || {};
  const messages = db.messages || {};
  const queue = data.queue || {};
  const delivered = (messages.DELIVERED || 0) + (messages.READ || 0);

  $("#metricGrid").innerHTML = `
    ${metricCard("Queue depth", queue.messages ?? 0, `${queue.ready ?? 0} ready · ${queue.unacked ?? 0} in flight`)}
    ${metricCard("Consumers", queue.consumers ?? 0, "workers attached to queue")}
    ${metricCard("Messages", db.total_messages ?? 0, `${db.users ?? 0} profiles`)}
    ${metricCard("Delivered", delivered, "incl. read")}
    ${metricCard("Read", messages.READ || 0, "read receipts")}
    ${metricCard("Failed", messages.FAILED || 0, "after retries")}
  `;
  $("#queueNow").textContent = `${queue.messages ?? 0} now`;

  renderProvider(data.provider || {});
  renderRecentTasks(db.recent || []);
  renderLoadLab(state.loadtest || { status: "idle" });

  drawSimpleChart($("#queueChart"), state.queueSeries, "#2dd4bf");
}

function metricCard(label, value, sub) {
  return `
    <div class="metric-card">
      <div class="metric-label">${esc(label)}</div>
      <div class="metric-value">${esc(value)}</div>
      <div class="metric-sub">${esc(sub)}</div>
    </div>`;
}

function renderProvider(provider) {
  const mode = provider.mode || "unknown";
  const badge = $("#providerModeBadge");
  badge.textContent = mode.replaceAll("_", " ");
  badge.className = `badge mode-${mode}`;

  const attempted = provider.attempted ?? 0;
  const succeeded = provider.succeeded ?? 0;
  const failed = provider.failed ?? 0;

  $("#providerStats").innerHTML = `
    <div class="provider-stat"><span>attempted</span><strong>${attempted}</strong></div>
    <div class="provider-stat"><span>succeeded</span><strong>${succeeded}</strong></div>
    <div class="provider-stat"><span>failed</span><strong>${failed}</strong></div>
  `;
}

function renderRecentTasks(tasks) {
  const wrap = $("#recentTasks");
  if (!tasks.length) {
    wrap.innerHTML = `<div class="table-empty">No tasks yet. Send a message and it will appear here.</div>`;
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead><tr><th>Task</th><th>Route</th><th>Body</th><th>Status</th><th>Try</th><th>Updated</th></tr></thead>
      <tbody>
        ${tasks.map((task) => `
          <tr>
            <td class="mono tiny">${esc(String(task.task_id).slice(0, 8))}</td>
            <td class="tiny">${esc(task.sender || "?")} → ${esc(task.receiver || "?")}</td>
            <td class="msg-cell tiny">${esc(task.body || "")}</td>
            <td>${statusPill(task.status)}</td>
            <td class="tiny">${task.attempts ?? 0}</td>
            <td class="tiny muted">${esc(timeAgo(task.updated_at))}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderLoadLab(load) {
  const running = ["running", "draining"].includes(load.status);
  const phase = load.status || "idle";

  $("#loadPhase").textContent = phase;
  $("#startLoadBtn").classList.toggle("hidden", running);
  $("#stopLoadBtn").classList.toggle("hidden", !running);
  $("#loadUsers").disabled = running;
  $("#loadDuration").disabled = running;

  const verdictBox = $("#loadVerdict");
  if (load.verdict) {
    verdictBox.className = `verdict ${load.verdict.toLowerCase()}`;
    verdictBox.classList.remove("hidden");
    const iconChar = load.verdict === "PASS" ? "✓" : load.verdict === "FAIL" ? "!" : "~";
    const title = load.verdict === "PASS"
      ? "PASS — robust under this load"
      : load.verdict === "FAIL"
        ? "FAIL — the system dropped requests"
        : "WARN — the queue did not settle";
    verdictBox.innerHTML = `
      <div class="verdict-icon">${iconChar}</div>
      <div><strong>${title}</strong><p>${esc(load.verdict_reason || "")}</p></div>`;
  } else if (load.status === "running") {
    verdictBox.className = "verdict warn";
    verdictBox.classList.remove("hidden");
    verdictBox.innerHTML = `
      <div class="verdict-icon">◎</div>
      <div><strong>Load test running</strong><p>${load.params?.users || 0} virtual users are hitting the gateway. The verdict appears after the queue drains.</p></div>`;
  } else if (load.status === "draining") {
    verdictBox.className = "verdict warn";
    verdictBox.classList.remove("hidden");
    verdictBox.innerHTML = `
      <div class="verdict-icon">↓</div>
      <div><strong>Traffic stopped — draining the queue</strong><p>Workers are finishing the backlog. This is the number that proves the queue absorbed the spike.</p></div>`;
  } else {
    verdictBox.classList.add("hidden");
  }

  const db = load.db || {};
  const pending = (db.QUEUED || 0) + (db.PROCESSING || 0);
  const delivered = (db.DELIVERED || 0) + (db.READ || 0);
  const latency = load.send_latency_ms || {};

  $("#loadStats").innerHTML = `
    ${loadStat("requests", load.total_requests ?? 0, `${load.rps ?? 0} req/s`)}
    ${loadStat("accepted", load.accepted ?? 0, "HTTP 202")}
    ${loadStat("rate limited", load.rate_limited ?? 0, "HTTP 429 — expected")}
    ${loadStat("server errors", load.server_errors ?? 0, "5xx / network")}
    ${loadStat("p50 send", latency.p50 ?? "—", "milliseconds")}
    ${loadStat("p95 send", latency.p95 ?? "—", "milliseconds")}
    ${loadStat("p99 send", latency.p99 ?? "—", "milliseconds")}
    ${loadStat("queue peak", load.queue_peak ?? 0, "messages buffered")}
    ${loadStat("drain time", load.drain_seconds ?? "—", "seconds")}
    ${loadStat("delivered", delivered, "incl. read")}
    ${loadStat("failed", db.FAILED ?? 0, "after retries")}
    ${loadStat("pending", pending, "queued/processing")}
  `;

  drawDualChart($("#loadChart"), load.samples || []);
}

function loadStat(label, value, sub) {
  return `
    <div class="load-stat">
      <span>${esc(label)}</span>
      <strong>${esc(value)}</strong>
      <small>${esc(sub)}</small>
    </div>`;
}

async function startLoadTest() {
  const users = Math.min(500, Math.max(1, Number($("#loadUsers").value) || 100));
  const duration = Math.min(120, Math.max(5, Number($("#loadDuration").value) || 20));

  try {
    await api("/loadtest", {
      method: "POST",
      body: { users, duration_seconds: duration },
    }, OPS);
    toast("Load test started", `${users} virtual users for ${duration}s. Watch the queue and latency.`, "success");
    await loadSystem();
  } catch (error) {
    toast("Could not start load test", error.message, "error");
  }
}

async function stopLoadTest() {
  try {
    await api("/loadtest/stop", { method: "POST" }, OPS);
    toast("Stopping load test", "Traffic is stopping; the queue will drain before the verdict.", "info");
    await loadSystem();
  } catch (error) {
    toast("Could not stop load test", error.message, "error");
  }
}

async function setProvider(mode, rate) {
  const body = { mode };
  if (mode === "random") body.fail_rate = rate ?? 0.3;
  try {
    await api("/provider", { method: "POST", body }, OPS);
    toast("Provider updated", `Notification provider is now in ${mode.replaceAll("_", " ")} mode.`, "success");
    await loadSystem();
  } catch (error) {
    toast("Could not update provider", error.message, "error");
  }
}

/* --------------------------------------------------------------------------
   Canvas charts
   -------------------------------------------------------------------------- */
function setupCanvas(canvas) {
  if (!canvas) return null;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(20, Math.floor(rect.width));
  const height = Math.max(20, Math.floor(rect.height));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function drawGrid(ctx, width, height) {
  ctx.strokeStyle = "rgba(255,255,255,.055)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i += 1) {
    const y = (height / 5) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
}

function drawEmpty(ctx, width, height, text) {
  ctx.fillStyle = "rgba(141,156,180,.55)";
  ctx.font = '11px "Segoe UI", system-ui, sans-serif';
  ctx.textAlign = "center";
  ctx.fillText(text, width / 2, height / 2);
}

function drawSeries(ctx, values, width, height, color, { fill = false, padding = 12 } = {}) {
  if (values.length < 2) return;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const innerWidth = width - padding * 2;
  const innerHeight = height - padding * 2;

  const points = values.map((value, index) => ({
    x: padding + (index / (values.length - 1)) * innerWidth,
    y: padding + innerHeight - ((value - min) / span) * innerHeight,
  }));

  if (fill) {
    ctx.beginPath();
    ctx.moveTo(points[0].x, height - padding);
    points.forEach((point) => ctx.lineTo(point.x, point.y));
    ctx.lineTo(points[points.length - 1].x, height - padding);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, padding, 0, height);
    gradient.addColorStop(0, `${color}33`);
    gradient.addColorStop(1, `${color}00`);
    ctx.fillStyle = gradient;
    ctx.fill();
  }

  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  const last = points[points.length - 1];
  ctx.beginPath();
  ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

function drawSimpleChart(canvas, values, color) {
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  drawGrid(ctx, width, height);
  if (values.length < 2) {
    drawEmpty(ctx, width, height, "Waiting for queue samples…");
    return;
  }
  drawSeries(ctx, values, width, height, color, { fill: true });
}

function drawDualChart(canvas, samples) {
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  drawGrid(ctx, width, height);
  if (!samples || samples.length < 2) {
    drawEmpty(ctx, width, height, "Start a load test to see throughput and queue depth.");
    return;
  }
  drawSeries(ctx, samples.map((sample) => sample.queue || 0), width, height, "#2dd4bf", { fill: true });
  drawSeries(ctx, samples.map((sample) => sample.rps || 0), width, height, "#60a5fa");
}

/* --------------------------------------------------------------------------
   Directory / new chat
   -------------------------------------------------------------------------- */
async function openNewChat() {
  $("#lookupChatError").classList.add("hidden");
  $("#lookupChatInput").value = "";
  openModal("newChatModal");
  switchTab("id");

  const hint = $("#demoHintText");
  if (hint) {
    hint.textContent = isLocalOrigin()
      ? "On another device, open the Wi-Fi address shown under \"Use on another device\" below your ID. Sign in there (or activate an account with the company code), then share unique IDs."
      : `On another device, open ${location.origin} on the same Wi-Fi, sign in or activate an account there, then share unique IDs.`;
  }

  await loadDirectory("");
}

function switchTab(name) {
  const modal = $("#newChatModal");
  $$(".tab", modal).forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $$(".tab-panel", modal).forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (name === "directory") setTimeout(() => $("#directorySearch").focus(), 50);
  if (name === "id") setTimeout(() => $("#lookupChatInput").focus(), 50);
}

let directoryTimer = null;
async function loadDirectory(query) {
  const list = $("#directoryList");
  list.innerHTML = `<div class="empty-list">Loading directory…</div>`;
  try {
    const suffix = query ? `?q=${encodeURIComponent(query)}&limit=50` : "?limit=50";
    const response = await api(`/users${suffix}`);
    const users = (response.data.users || []).filter((user) => user.username !== state.me.username);

    if (!users.length) {
      list.innerHTML = `<div class="empty-list"><strong>No colleagues found</strong>Ask a teammate to sign in or activate an account, then come back.</div>`;
      return;
    }

    list.innerHTML = users.map((user) => `
      <button class="directory-item" data-username="${esc(user.username)}">
        ${avatarMarkup(user, "tiny")}
        <span class="dir-meta">
          <strong>${esc(displayName(user))}</strong>
          <small>${esc(user.public_id || "")} · @${esc(user.username)}</small>
        </span>
        ${icon("arrow")}
      </button>`).join("");

    $$(".directory-item", list).forEach((button) => {
      button.addEventListener("click", async () => {
        closeModal("newChatModal");
        switchView("chat");
        await openChat(button.dataset.username);
      });
    });
  } catch (error) {
    list.innerHTML = `<div class="empty-list">Could not load directory.<br />${esc(error.message)}</div>`;
  }
}

/* --------------------------------------------------------------------------
   Profile modal
   -------------------------------------------------------------------------- */
function openProfile() {
  renderProfileModal();
  openModal("profileModal");
}

function renderProfileModal() {
  const card = $("#profileCard");
  card.innerHTML = `
    ${avatarMarkup(state.me, "large")}
    <div class="pc-body">
      <h4>${esc(displayName(state.me))}</h4>
      <div class="pc-id">
        <span>${esc(state.me.public_id || "—")}</span>
        <button class="copy-mini" id="copyProfileIdBtn" title="Copy ID">${icon("copy")}</button>
      </div>
      <p class="muted small">@${esc(state.me.username)}</p>
    </div>`;
  $("#copyProfileIdBtn").addEventListener("click", () => copyText(state.me.public_id, "Your ID copied"));
  $("#shareProfileBtn")?.addEventListener("click", () => {
    closeModal("profileModal");
    openShareModal();
  });
  $("#passwordForm").reset();
  $("#passwordError").classList.add("hidden");
}

async function handlePasswordChange(event) {
  event.preventDefault();
  const currentPassword = $("#currentPassword").value;
  const newPassword = $("#newPassword").value;
  const errorBox = $("#passwordError");
  const button = $("#passwordBtn");

  errorBox.classList.add("hidden");
  button.disabled = true;
  try {
    await api("/auth/password", {
      method: "POST",
      body: { current_password: currentPassword, new_password: newPassword },
    });
    $("#passwordForm").reset();
    toast(
      "Password updated",
      "Every other device was signed out. This tab stays signed in.",
      "success"
    );
  } catch (error) {
    errorBox.textContent = friendlyError(error);
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
}

async function signOut() {
  try {
    await api("/auth/logout", { method: "POST", skipAuthRedirect: true });
  } catch { /* best effort: local sign-out still works */ }

  clearToken();
  state.me = null;
  closeModal("profileModal");
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
  if (state.systemTimer) clearInterval(state.systemTimer);
  state.systemTimer = null;
  showOnboarding();
  toast("Signed out", "This tab is signed out. Other devices stay signed in.", "info");
}

/* --------------------------------------------------------------------------
   Multi-device sharing
   -------------------------------------------------------------------------- */
function isLocalOrigin() {
  return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(location.hostname);
}

async function deviceInfo() {
  let network = null;
  try {
    network = (await api("/network", {}, OPS)).data;
  } catch { /* ops-api can be down; fall back to the current origin */ }

  const uiPort = network?.ui_port || 3000;
  let url = location.origin;
  let note = "Other devices on the same Wi-Fi use this address.";

  if (isLocalOrigin()) {
    if (network?.ui_url) {
      url = network.ui_url;
      note = "localhost only works on this computer. Use the address above on your phone or another laptop.";
    } else if (network?.lan_ip) {
      url = `http://${network.lan_ip}:${uiPort}`;
      note = "localhost only works on this computer. Use the address above on your phone or another laptop.";
    } else {
      url = `http://<this-computer-ip>:${uiPort}`;
      note = "Find this computer's Wi-Fi IPv4 address (run ipconfig or scripts\\start.ps1), then open http://<that-ip>:"
        + `${uiPort} on the other device. Running scripts\\start.ps1 prints the exact address.`;
    }
  }

  return { url, note, network, local: isLocalOrigin() };
}

async function openShareModal() {
  if (!state.me) return;
  openModal("shareModal");
  $("#shareId").textContent = state.me.public_id || "—";

  const info = await deviceInfo();
  const invite = `Join me on PingHub.\nAddress: ${info.url}\nMy unique ID: ${state.me.public_id}\n`
    + "Open the address on the same Wi-Fi, sign in (or activate an account with the company code), then choose New chat and paste my ID.";
  state.share = { ...info, invite };

  $("#shareUrl").textContent = info.url;
  $("#shareUrlNote").textContent = info.note;
  $("#shareMessage").textContent = invite;
}

/* --------------------------------------------------------------------------
   Modals, toasts, notifications
   -------------------------------------------------------------------------- */
function openModal(id) {
  $(`#${id}`).classList.remove("hidden");
}
function closeModal(id) {
  $(`#${id}`).classList.add("hidden");
}

function toast(title, message, kind = "info") {
  const container = $("#toasts");
  if (container.children.length >= 4) container.firstElementChild.remove();

  const iconName = kind === "success" ? "check" : kind === "error" ? "alert" : kind === "read" ? "checkcheck" : "bell";
  const element = document.createElement("div");
  element.className = `toast ${kind}`;
  element.innerHTML = `
    <span class="t-ico">${icon(iconName)}</span>
    <span class="t-body"><strong>${esc(title)}</strong><p>${esc(message)}</p></span>`;
  container.appendChild(element);

  setTimeout(() => {
    element.classList.add("leaving");
    setTimeout(() => element.remove(), 200);
  }, 4200);
}

function browserNotify(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (!document.hidden) return;
  try { new Notification(title, { body }); } catch { /* ignore */ }
}

async function enableAlerts() {
  if (!("Notification" in window)) {
    toast("Not supported", "This browser does not expose desktop notifications.", "error");
    return;
  }
  const permission = await Notification.requestPermission();
  if (permission === "granted") {
    toast("Desktop alerts on", "You will get a notification when a new message arrives in a background tab.", "success");
    renderMe();
  } else {
    toast("Alerts not enabled", "You can allow notifications later from your browser settings.", "info");
  }
}

async function copyText(text, label) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied", label || "Copied to clipboard.", "success");
  } catch {
    const input = document.createElement("textarea");
    input.value = text;
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
    toast("Copied", label || "Copied to clipboard.", "success");
  }
}

/* --------------------------------------------------------------------------
   Views and event binding
   -------------------------------------------------------------------------- */
function switchView(view) {
  state.view = view;
  $$(".rail-btn").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));

  if (view === "chat") {
    loadConversations({ silent: true }).catch(() => {});
  }
  if (view === "history") {
    loadHistory();
  }
  if (view === "system") {
    loadSystem();
    if (!state.systemTimer) {
      state.systemTimer = setInterval(() => {
        if (state.view === "system" && !document.hidden) loadSystem();
      }, 2000);
    }
  }
}

function bindEvents() {
  // Onboarding: sign in / activate
  $("#loginForm").addEventListener("submit", handleLogin);
  $("#registerForm").addEventListener("submit", handleRegister);
  $$("[data-auth-tab]").forEach((tab) => {
    tab.addEventListener("click", () => switchAuthTab(tab.dataset.authTab));
  });

  // Navigation
  $$(".rail-btn").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });

  // Chat sidebar
  $("#newChatBtn").addEventListener("click", openNewChat);
  $("#emptyNewChatBtn").addEventListener("click", openNewChat);
  $("#conversationSearch").addEventListener("input", renderConversations);
  $("#copyIdBtn").addEventListener("click", () => copyText(state.me?.public_id, "Your ID copied"));
  $("#emptyCopyIdBtn").addEventListener("click", () => copyText(state.me?.public_id, "Your ID copied"));
  $("#shareDeviceBtn").addEventListener("click", openShareModal);
  $("#copyShareUrlBtn").addEventListener("click", () => copyText(state.share?.url || $("#shareUrl").textContent, "Address copied"));
  $("#copyShareIdBtn").addEventListener("click", () => copyText(state.me?.public_id, "Your ID copied"));
  $("#copyShareMessageBtn").addEventListener("click", () => copyText(state.share?.invite, "Invite copied"));
  $("#enableAlertsBtn").addEventListener("click", enableAlerts);
  $("#railProfileBtn").addEventListener("click", openProfile);

  // Thread
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage($("#messageInput").value);
  });
  $("#messageInput").addEventListener("input", updateComposer);
  $("#messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage($("#messageInput").value);
    }
  });
  $("#threadRefreshBtn").addEventListener("click", () => loadThread({ forceScroll: true }));
  $("#threadDeleteBtn").addEventListener("click", () => askDeleteChat(state.activePeer));
  $("#mobileBackBtn").addEventListener("click", () => {
    $("#view-chat").classList.remove("mobile-thread");
  });

  // History (always the signed-in employee)
  $("#refreshHistoryBtn").addEventListener("click", () => loadHistory());
  $("#historySearch").addEventListener("input", renderHistory);
  $("#historyStatusFilter").addEventListener("change", renderHistory);
  $("#historyDirectionFilter").addEventListener("change", renderHistory);
  $("#exportHistoryBtn").addEventListener("click", () => {
    if (!state.history) return;
    const blob = new Blob([JSON.stringify(state.history.messages, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `pinghub-history-${state.history.target}.json`;
    link.click();
    URL.revokeObjectURL(url);
    toast("History exported", "The JSON file was downloaded.", "success");
  });

  // System
  $("#refreshSystemBtn").addEventListener("click", loadSystem);
  $("#startLoadBtn").addEventListener("click", startLoadTest);
  $("#stopLoadBtn").addEventListener("click", stopLoadTest);
  $("#resetProviderBtn").addEventListener("click", async () => {
    try {
      await api("/provider/reset", { method: "POST" }, OPS);
      toast("Provider counters reset", "The mock provider is back to zero attempts.", "success");
      await loadSystem();
    } catch (error) {
      toast("Could not reset", error.message, "error");
    }
  });
  $$(".provider-btn").forEach((button) => {
    button.addEventListener("click", () => setProvider(button.dataset.mode, Number(button.dataset.rate)));
  });

  // New chat modal
  $$("#newChatModal .tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
  $("#lookupChatForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const identifier = $("#lookupChatInput").value.trim();
    const errorBox = $("#lookupChatError");
    errorBox.classList.add("hidden");

    if (!identifier) return;
    try {
      const user = await lookupUser(identifier);
      if (user.username === state.me.username) {
        errorBox.textContent = "That is your own ID. Enter someone else's.";
        errorBox.classList.remove("hidden");
        return;
      }
      closeModal("newChatModal");
      switchView("chat");
      await openChat(user.username);
    } catch (error) {
      errorBox.textContent = error.status === 404 ? "No colleague matches that ID or username." : error.message;
      errorBox.classList.remove("hidden");
    }
  });
  $("#directorySearch").addEventListener("input", () => {
    clearTimeout(directoryTimer);
    directoryTimer = setTimeout(() => loadDirectory($("#directorySearch").value.trim()), 240);
  });

  // Profile modal
  $("#passwordForm").addEventListener("submit", handlePasswordChange);
  $("#signOutBtn").addEventListener("click", signOut);
  $("#confirmCancelBtn").addEventListener("click", () => {
    state.confirm = null;
    closeModal("confirmModal");
  });
  $("#confirmOkBtn").addEventListener("click", async () => {
    const pending = state.confirm;
    state.confirm = null;
    closeModal("confirmModal");
    if (pending?.action === "delete-chat") await deleteChat(pending.peer);
  });

  // Generic modal close / backdrop
  $$(".modal-close").forEach((button) => {
    button.addEventListener("click", () => closeModal(button.dataset.close));
  });
  $$(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) closeModal(backdrop.id);
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      $$(".modal-backdrop:not(.hidden)").forEach((modal) => closeModal(modal.id));
    }
  });
}
