/* JARVIS interface. Every indicator is driven by backend state received over
   the WebSocket; nothing is shown as active without evidence from the server.
   All dynamic text is inserted with textContent (never innerHTML). */
(function () {
  "use strict";

  const TOKEN = document.querySelector('meta[name="jarvis-token"]').content;
  const $ = (id) => document.getElementById(id);
  const orb = new window.JarvisOrb($("orb"));

  const ui = {
    state: null,
    wsOnline: false,
    busy: false,
    speaking: false,
    renderedTurns: new Set(),
    tools: new Map(),
  };

  // ---------- helpers ----------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-Jarvis-Token": TOKEN, ...(opts.headers || {}) },
    });
    let body = null;
    try { body = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) {
      const msg = (body && (body.detail || body.error)) || `HTTP ${res.status}`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return body;
  }

  function toast(message, kind) {
    const t = el("div", "toast" + (kind === "err" ? " err" : ""), message);
    $("toasts").appendChild(t);
    setTimeout(() => t.remove(), 6000);
  }

  const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  // ---------- clock ----------
  function tick() {
    const d = new Date();
    $("clock-time").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    $("clock-date").textContent = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  }
  tick();
  setInterval(tick, 10000);

  // ---------- core state ----------
  function updateCore() {
    const s = ui.state;
    let state = "standby", label = "Standby", sub = "", cls = "";
    if (!ui.wsOnline || !s) {
      state = "offline"; label = "Offline"; sub = "No connection to the JARVIS core"; cls = "off";
    } else if (s.pending_confirmations && s.pending_confirmations.length) {
      state = "awaiting"; label = "Awaiting authorization"; sub = "Review the request on the right"; cls = "warn";
    } else if (s.ai.status === "processing") {
      state = "processing"; label = "Processing"; sub = "Reasoning and executing tools";
    } else if (ui.speaking) {
      state = "speaking"; label = "Speaking"; sub = "Reading reply aloud";
    } else if (s.ai.status === "error") {
      state = "error"; label = "Fault"; sub = s.ai.last_error || "The last request failed"; cls = "err";
    } else {
      const avail = s.ai.providers.filter((p) => p.available);
      const real = avail.find((p) => p.kind !== "dev");
      sub = real ? `Ready · ${real.name}` : "Ready · development fallback only";
    }
    orb.setState(state);
    const cs = $("core-state");
    cs.textContent = label;
    cs.className = "core-state" + (cls ? " " + cls : "");
    $("core-sub").textContent = sub;
    $("btn-send").disabled = ui.busy || !ui.wsOnline;
    $("btn-cancel").hidden = !ui.busy;
  }

  // ---------- systems panel ----------
  function statusRow(dot, name, detail, value, valueCls) {
    const li = el("li", "status-item");
    li.appendChild(el("span", "dot " + dot));
    const mid = el("div");
    mid.appendChild(el("div", "status-name", name));
    if (detail) mid.appendChild(el("div", "status-detail", detail));
    li.appendChild(mid);
    li.appendChild(el("span", "status-value " + (valueCls || ""), value));
    return li;
  }

  function renderSystems(s) {
    const list = $("status-list");
    list.replaceChildren();
    list.appendChild(statusRow(ui.wsOnline ? "ok" : "err", "Core", `v${s.core.version}`, ui.wsOnline ? "Online" : "Offline", ui.wsOnline ? "ok" : "err"));

    const providers = s.ai.providers;
    const cloud = providers.find((p) => p.kind === "cloud");
    const local = providers.find((p) => p.kind === "local");
    const st = s.settings;
    if (cloud) {
      let dot = cloud.available ? "ok" : "off", val = cloud.available ? "Ready" : "Not set";
      let detail = cloud.available ? `${s.ai.primary.model || cloud.name}` : cloud.detail;
      if (st.local_only) { dot = "off"; val = "Blocked"; detail = "Local-only privacy mode is on"; }
      else if (s.ai.usage.budget_exceeded) { dot = "warn"; val = "Budget"; detail = "Monthly budget reached; cloud paused"; }
      list.appendChild(statusRow(dot, "Cloud AI · Claude", detail, val, dot === "ok" ? "ok" : dot === "warn" ? "warn" : ""));
    }
    if (local) {
      const starting = !local.available && (local.detail || "").startsWith("Ollama startar");
      list.appendChild(statusRow(local.available ? "ok" : starting ? "warn" : "off", "Local AI · Ollama", local.detail, local.available ? "Ready" : starting ? "Startar…" : "Offline", local.available ? "ok" : starting ? "warn" : ""));
    }
    if (!providers.some((p) => p.available && p.kind !== "dev")) {
      list.appendChild(statusRow("warn", "Dev fallback", "Rule-based stand-in; not an AI model", "Active", "warn"));
    }

    const ha = s.integrations.homeassistant;
    const haMap = { connected: ["ok", "Online", "ok"], not_configured: ["off", "Not set", ""], disabled: ["off", "Off", ""], auth_failed: ["err", "Auth", "err"], unreachable: ["err", "No link", "err"], error: ["err", "Error", "err"] };
    const [hd, hv, hc] = haMap[ha.state] || ["warn", ha.state, "warn"];
    list.appendChild(statusRow(hd, "Home Assistant", ha.state === "connected" ? `${ha.location_name || "Connected"} · ${ha.version || ""}` : ha.detail, hv, hc));

    const so = s.integrations.sonos;
    const soMap = { ok: ["ok", `${so.speakers.length} found`, "ok"], no_speakers: ["warn", "None", "warn"], unavailable: ["off", "—", ""], error: ["err", "Error", "err"] };
    const [sd, sv, sc] = soMap[so.state] || ["warn", so.state, "warn"];
    list.appendChild(statusRow(sd, "Sonos", so.detail, sv, sc));

    const v = s.voice;
    const micDetail = v.mic_state === "not_implemented" ? "No voice engine installed (Phase 2)" : `Master switch ${v.master_switch}`;
    list.appendChild(statusRow(v.mic_state === "not_implemented" ? "na" : "off", "Microphone", micDetail, v.mic_state === "not_implemented" ? "Off" : v.mic_state.replace("_", " ")));
    list.appendChild(statusRow("na", "Camera", "No camera module installed (Phase 3)", "Off"));

    const mon = s.monitoring;
    if (mon && mon.restricted) {
      list.appendChild(statusRow("off", "Bevakning", mon.detail, "Bara ägaren", ""));
    } else if (mon) {
      const active = Object.values(mon.sources || {}).filter((v) => v.state === "active").length;
      const [mdot, mval, mcls] = mon.paused ? ["warn", "Pausad", "warn"] : mon.muted ? ["warn", "Tystad", "warn"] : ["ok", "Aktiv", "ok"];
      list.appendChild(statusRow(mdot, "Bevakning", `${active} källa/källor · kanaler: ${(mon.channels || []).map((c) => c.name).join(", ") || "inga"}`, mval, mcls));
    }

    const c = s.computer;
    let cd = "ok", cv = "Live", cc = "ok", cdetail = `${c.allowed_apps.length} approved apps`;
    if (!c.enabled) { cd = "off"; cv = "Off"; cc = ""; cdetail = c.platform_supported ? "Disabled in configuration" : "Not available on this platform"; }
    else if (!c.platform_supported) { cdetail = "Windows-only; dry-run available"; cd = "off"; cv = "N/A"; cc = ""; }
    if (c.enabled && c.dry_run) { cd = "warn"; cv = "Dry-run"; cc = "warn"; }
    list.appendChild(statusRow(cd, "Computer control", cdetail, cv, cc));
  }

  function renderZones(s) {
    const list = $("zone-list");
    list.replaceChildren();
    const sp = s.integrations.sonos.speakers;
    $("zones-meta").textContent = sp.length ? `${sp.length}` : "";
    if (!sp.length) {
      list.appendChild(el("p", "zone-empty", s.integrations.sonos.state === "unavailable"
        ? "Connect Home Assistant to discover Sonos speakers."
        : s.integrations.sonos.detail));
      return;
    }
    for (const z of sp) {
      const li = el("li", "zone");
      const left = el("div");
      left.appendChild(el("div", "zone-name", z.name));
      left.appendChild(el("div", "zone-area", z.media_title ? `${z.area || "No room"} · ${z.media_title}` : (z.area || "No room")));
      li.appendChild(left);
      li.appendChild(el("div", "zone-state " + (z.state === "playing" ? "playing" : ""), z.volume != null ? `${z.state} · ${z.volume}%` : z.state));
      const bar = el("div", "zone-vol");
      const fill = el("div");
      fill.style.width = `${z.volume || 0}%`;
      bar.appendChild(fill);
      li.appendChild(bar);
      list.appendChild(li);
    }
  }

  function renderUsage(s) {
    const u = s.ai.usage;
    $("usage-month").textContent = u.month;
    $("usage-cost").textContent = `$${u.estimated_cost_usd.toFixed(2)}`;
    $("usage-budget").textContent = u.budget_usd ? `of $${u.budget_usd.toFixed(2)}` : "no budget cap";
    const pct = u.budget_usd ? Math.min(100, (u.estimated_cost_usd / u.budget_usd) * 100) : 0;
    const fill = $("usage-fill");
    fill.style.width = `${pct}%`;
    fill.className = "meter-fill" + (pct >= 80 ? " warn" : "");
    $("usage-note").textContent = `${u.requests} model request${u.requests === 1 ? "" : "s"} · estimated from token counts`;
  }

  function renderHeader(s) {
    $("version").textContent = `· v${s.core.version}`;
    const ap = s.active_profile;
    $("profile-chip").textContent = !ap ? "" : ap.name.toLowerCase() === ap.role ? ap.name : `${ap.name} · ${ap.role}`;
    const mic = s.voice;
    $("mic-hint").textContent = mic.mic_state === "not_implemented"
      ? "Microphone off · voice input arrives in Phase 2"
      : `Microphone ${mic.mic_state.replace("_", " ")}`;
  }

  function renderSettings(s) {
    const st = s.settings;
    const sel = $("set-profile");
    if (sel.options.length !== s.profiles.length) {
      sel.replaceChildren(...s.profiles.map((p) => { const o = el("option", null, `${p.name} (${p.role})`); o.value = p.id; return o; }));
    }
    sel.value = st.active_profile;
    $("set-dry-computer").checked = st.dry_run_computer;
    $("set-dry-home").checked = st.dry_run_smart_home;
    $("set-local-only").checked = st.local_only;
    $("set-sensitivity").value = st.cloud_max_sensitivity;
    $("set-mic").checked = st.microphone_enabled;
    $("set-camera").checked = st.camera_enabled;
    $("set-speak").checked = st.speak_replies;
    $("set-advanced").checked = st.advanced_mode;
  }

  function renderConfirmations(s) {
    const pending = s.pending_confirmations || [];
    $("panel-confirm").hidden = pending.length === 0;
    const box = $("confirm-list");
    box.replaceChildren();
    for (const c of pending) {
      const card = el("div", "confirm");
      card.appendChild(el("p", "confirm-summary", c.summary));
      const left = Math.max(0, Math.round(c.expires_at - Date.now() / 1000));
      card.appendChild(el("div", "confirm-meta", `${c.tool} · ${c.risk} risk · expires in ${left}s`));
      const row = el("div", "btn-row");
      const yes = el("button", "btn btn-approve", "Approve");
      const no = el("button", "btn btn-deny", "Deny");
      yes.type = no.type = "button";
      yes.addEventListener("click", () => resolveConfirm(c.id, true, yes, no));
      no.addEventListener("click", () => resolveConfirm(c.id, false, yes, no));
      row.append(yes, no);
      card.appendChild(row);
      box.appendChild(card);
    }
  }

  async function resolveConfirm(id, approve, ...buttons) {
    buttons.forEach((b) => (b.disabled = true));
    try {
      await api(`/api/confirmations/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ approve }) });
    } catch (e) {
      toast(e.message, "err");
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  // ---------- monitoring / notifications ----------
  const PRIO_LABEL = { urgent: "AKUT", high: "Viktig", normal: "Normal", low: "Låg" };
  const DELIVERY_LABEL = { immediate: "meddelad direkt", passive: "visad här", summary: "sparad till sammanfattningen" };

  function renderMonitoring(m) {
    if (!m) return;
    const st = $("mon-state");
    // Other profiles than the owner see no notifications, summary or controls.
    document.querySelectorAll(".mon-controls .btn").forEach((b) => (b.hidden = !!m.restricted));
    if (m.restricted) {
      st.textContent = "Bevakning: bara ägaren";
      st.className = "mon-state off";
      $("mon-meta").textContent = m.detail;
      $("notify-list").replaceChildren();
      $("notify-empty").hidden = true;
      renderSummary(null);
      return;
    }
    const gaming = m.gaming && m.gaming.active;
    st.textContent = m.paused ? "Bevakning: pausad" : m.muted ? "Bevakning: aktiv (tystad)" : gaming ? "Bevakning: aktiv · Spelläge: på" : "Bevakning: aktiv";
    st.className = "mon-state" + (m.paused || m.muted || gaming ? " paused" : "");
    $("btn-mon-pause").textContent = m.paused ? "Återuppta" : "Pausa";
    $("btn-mon-mute").textContent = m.muted ? "Sluta tysta" : "Tysta 1 h";
    const watching = Object.entries(m.sources || {}).filter(([, v]) => v.state === "active").map(([k]) => k);
    const parts = [
      `Bevakar: ${watching.length ? watching.join(", ") : "inget ännu"}`,
      `Tysta timmar ${m.quiet_hours}${m.quiet_now ? " (nu)" : ""}`,
      `Sammanfattning ${m.morning_summary_time}`,
    ];
    if (m.waiting_for_summary) parts.push(`${m.waiting_for_summary} väntar`);
    const g = m.gaming || {};
    if (g.enabled) parts.unshift(g.active ? `Spelläge: på (${g.reason})` : "Spelläge: av");
    if (m.muted && m.muted_until) parts.push(`tystad till ${new Date(m.muted_until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
    $("mon-meta").textContent = parts.join(" · ");
    const list = $("notify-list");
    list.replaceChildren();
    for (const n of m.recent || []) {
      const li = el("li", "notify" + (n.acknowledged || n.delivery === "summary" ? "" : " unread"));
      const head = el("div", "notify-head");
      head.appendChild(el("span", "prio " + n.priority, PRIO_LABEL[n.priority] || n.priority));
      head.appendChild(el("span", null, fmtTime(n.created_at)));
      head.appendChild(el("span", null, DELIVERY_LABEL[n.delivery] || n.delivery));
      li.appendChild(head);
      li.appendChild(el("div", "notify-title", (n.priority === "urgent" ? "🚨 " : "") + n.title));
      if (n.detail) li.appendChild(el("div", "notify-detail", n.detail));
      li.title = `${n.reason}${n.channels.length ? " · kanaler: " + n.channels.join(", ") : ""}${n.acknowledged ? "" : " · klicka för att kvittera"}`;
      if (!n.acknowledged) li.addEventListener("click", () => api("/api/notifications/ack", { method: "POST", body: JSON.stringify({ id: n.id }) }).catch((e) => toast(e.message, "err")));
      list.appendChild(li);
    }
    $("notify-empty").hidden = (m.recent || []).length > 0;
    renderSummary(m.summary);
  }

  // The latest summary stays visible until it is acknowledged.
  function renderSummary(s) {
    const card = $("summary-card");
    card.replaceChildren();
    if (!s || s.acknowledged) { card.hidden = true; return; }
    card.hidden = false;
    const head = el("div", "summary-head");
    head.appendChild(el("strong", null, s.title));
    head.appendChild(el("span", null, fmtTime(s.created_at)));
    card.appendChild(head);
    const section = (label, lines) => {
      if (!lines.length) return;
      card.appendChild(el("div", "summary-label", label));
      const ul = el("ul", "summary-list");
      for (const line of lines) ul.appendChild(el("li", null, line));
      card.appendChild(ul);
    };
    section("Viktiga mejl", s.mail || []);
    const files = s.files || { count: 0, top: [] };
    if (files.count) section(`Nya filer (${files.count})`, files.top || []);
    section("Det som väntat", (s.waited || []).map((w) => `${w.time} ${w.title}`));
    const ack = el("button", "btn", "Kvittera");
    ack.type = "button";
    ack.addEventListener("click", () => api("/api/notifications/ack", { method: "POST", body: JSON.stringify({ id: s.notification_id }) }).catch((e) => toast(e.message, "err")));
    if (s.notification_id) card.appendChild(ack);
  }

  $("btn-mon-pause").addEventListener("click", () => {
    const paused = !(ui.state && ui.state.monitoring && ui.state.monitoring.paused);
    api("/api/monitoring/pause", { method: "POST", body: JSON.stringify({ paused }) }).then((m) => { if (ui.state) { ui.state.monitoring = m; renderMonitoring(m); } }).catch((e) => toast(e.message, "err"));
  });
  $("btn-mon-mute").addEventListener("click", () => {
    const muted = ui.state && ui.state.monitoring && ui.state.monitoring.muted;
    api("/api/notifications/mute", { method: "POST", body: JSON.stringify({ minutes: muted ? 0 : 60 }) }).then((m) => { if (ui.state) { ui.state.monitoring = m; renderMonitoring(m); } }).catch((e) => toast(e.message, "err"));
  });
  $("btn-mon-ack").addEventListener("click", () => api("/api/notifications/ack", { method: "POST", body: JSON.stringify({}) }).catch((e) => toast(e.message, "err")));
  $("btn-mon-summary").addEventListener("click", () => {
    const b = $("btn-mon-summary");
    b.disabled = true;
    api("/api/monitoring/summary", { method: "POST", body: JSON.stringify({}) })
      .then((m) => { if (ui.state) { ui.state.monitoring = m; renderMonitoring(m); } })
      .catch((e) => toast(e.message, "err"))
      .finally(() => { b.disabled = false; });
  });

  function renderAll() {
    const s = ui.state;
    if (!s) return;
    renderMonitoring(s.monitoring);
    renderHeader(s);
    renderSystems(s);
    renderZones(s);
    renderUsage(s);
    renderSettings(s);
    renderConfirmations(s);
    updateCore();
  }

  // ---------- conversation ----------
  function hideEmpty() { $("empty-state").hidden = true; }
  function scrollMessages() { const m = $("messages"); m.scrollTop = m.scrollHeight; }

  function addUser(text) {
    hideEmpty();
    const m = el("div", "msg msg-user");
    m.appendChild(el("div", "msg-body", text));
    $("messages").appendChild(m);
    scrollMessages();
  }

  function addThinking() {
    removeThinking();
    const t = el("div", "msg msg-assistant");
    t.id = "thinking";
    const dots = el("div", "thinking");
    dots.append(el("span"), el("span"), el("span"));
    t.appendChild(dots);
    $("messages").appendChild(t);
    scrollMessages();
  }
  function removeThinking() { const t = $("thinking"); if (t) t.remove(); }

  function addAssistant(result) {
    removeThinking();
    hideEmpty();
    const m = el("div", "msg msg-assistant");
    m.appendChild(el("div", "msg-body", result.text));
    const meta = el("div", "msg-meta");
    if (result.is_fallback) meta.appendChild(el("span", "badge fallback", result.provider === "dev" ? "Dev fallback" : "Fallback"));
    const last = (result.providers || []).slice(-1)[0];
    if (last && last.kind === "local") meta.appendChild(el("span", "badge local", "Local"));
    if (result.model) meta.appendChild(el("span", null, result.model));
    if (result.estimated_cost_usd) meta.appendChild(el("span", null, `~$${result.estimated_cost_usd.toFixed(4)}`));
    if (result.duration_s != null) meta.appendChild(el("span", null, `${result.duration_s}s`));
    m.appendChild(meta);
    $("messages").appendChild(m);
    scrollMessages();
    if (ui.state && ui.state.settings.speak_replies) speak(result.text.replace(/^\[Dev fallback[^\]]*\]\s*/, ""));
  }

  function addError(text) {
    removeThinking();
    hideEmpty();
    const m = el("div", "msg msg-assistant msg-error");
    m.appendChild(el("div", "msg-body", text));
    $("messages").appendChild(m);
    scrollMessages();
  }

  function clearMessages() {
    const box = $("messages");
    [...box.querySelectorAll(".msg")].forEach((n) => n.remove());
    $("empty-state").hidden = false;
  }

  // ---------- tool history ----------
  const STATUS_LABEL = {
    pending: "Pending", awaiting_confirmation: "Awaiting", running: "Running", succeeded: "Done",
    failed: "Failed", denied: "Denied", cancelled: "Cancelled",
  };

  function renderTool(t) {
    let li = ui.tools.get(t.id);
    const fresh = !li;
    if (fresh) {
      li = el("li", "tool");
      ui.tools.set(t.id, li);
    }
    li.replaceChildren();
    const head = el("div", "tool-head");
    head.appendChild(el("span", "pill " + t.status, STATUS_LABEL[t.status] || t.status));
    if (t.dry_run) head.appendChild(el("span", "pill dry", "Dry-run"));
    head.appendChild(el("span", "tool-name", t.tool));
    head.appendChild(el("span", "tool-time", fmtTime(t.started_at)));
    li.appendChild(head);
    if (t.summary) li.appendChild(el("p", "tool-summary", t.summary));
    if (t.error) li.appendChild(el("p", "tool-error", t.error));
    if (t.verified === true) li.appendChild(el("div", "tool-verify", "✓ Effect verified from device state"));
    if (t.verified === false) li.appendChild(el("div", "tool-verify", "Effect could not be verified"));
    const d = el("details");
    d.appendChild(el("summary", null, "Details"));
    const info = { arguments: t.arguments, risk: t.risk, decision: t.decision, result: t.result };
    d.appendChild(el("pre", null, JSON.stringify(info, null, 2)));
    li.appendChild(d);
    if (fresh) $("tool-list").prepend(li);
    $("tool-empty").hidden = ui.tools.size > 0;
  }

  // ---------- speech output (browser, local) ----------
  function speak(text) {
    if (!("speechSynthesis" in window) || !text) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text.slice(0, 1200));
    const sv = /[åäöÅÄÖ]/.test(text) || /\b(och|jag|det|är|inte|du)\b/i.test(text);
    u.lang = sv ? "sv-SE" : "en-GB";
    const voice = window.speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith(sv ? "sv" : "en"));
    if (voice) u.voice = voice;
    u.onstart = () => { ui.speaking = true; updateCore(); };
    u.onend = u.onerror = () => { ui.speaking = false; updateCore(); };
    window.speechSynthesis.speak(u);
  }

  // ---------- events ----------
  function onEvent(ev) {
    const d = ev.data || {};
    switch (ev.type) {
      case "state":
        ui.state = d;
        ui.busy = !!(d.conversation && d.conversation.busy) || d.ai.status === "processing";
        renderAll();
        break;
      case "monitoring":
        if (ui.state) { ui.state.monitoring = d; renderMonitoring(d); renderSystems(ui.state); }
        break;
      case "ai.status":
        if (ui.state) { ui.state.ai.status = d.status; if (d.error) ui.state.ai.last_error = d.error; }
        ui.busy = d.status === "processing";
        updateCore();
        break;
      case "turn.started":
        addThinking();
        break;
      case "turn.completed":
        if (!ui.renderedTurns.has(d.request_id)) { ui.renderedTurns.add(d.request_id); addAssistant(d); }
        refreshState();
        break;
      case "turn.error":
        addError(d.error);
        break;
      case "turn.cancelled":
        addError("Request cancelled.");
        break;
      case "tool.updated":
        renderTool(d);
        break;
      case "confirmation.requested":
        if (ui.state) { ui.state.pending_confirmations = [...(ui.state.pending_confirmations || []), d]; renderConfirmations(ui.state); updateCore(); }
        break;
      case "confirmation.resolved":
        if (ui.state) { ui.state.pending_confirmations = (ui.state.pending_confirmations || []).filter((c) => c.id !== d.id); renderConfirmations(ui.state); updateCore(); }
        if (d.outcome === "expired") toast("A confirmation request expired and was treated as denied.");
        break;
      case "conversation.reset":
        clearMessages();
        break;
      case "history.cleared":
        ui.tools.clear();
        $("tool-list").replaceChildren();
        $("tool-empty").hidden = false;
        break;
    }
  }

  let ws = null;
  let retry = 0;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
    ws.onopen = () => {
      retry = 0;
      ui.wsOnline = true;
      const link = $("link");
      link.className = "link online";
      $("link-text").textContent = "Core linked";
      loadInitial();
    };
    ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
    ws.onclose = () => {
      ui.wsOnline = false;
      $("link").className = "link offline";
      $("link-text").textContent = "Core offline";
      if (ui.state) renderSystems(ui.state);
      updateCore();
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, 500 * 2 ** retry);
    };
  }

  async function refreshState() {
    try { ui.state = await api("/api/state"); renderAll(); } catch (_) { /* ws will resync */ }
  }

  async function loadInitial() {
    try {
      const [hist, conv] = await Promise.all([api("/api/history"), api("/api/conversation")]);
      ui.tools.clear();
      $("tool-list").replaceChildren();
      hist.slice().reverse().forEach(renderTool);
      $("tool-empty").hidden = ui.tools.size > 0;
      clearMessages();
      for (const m of conv.messages) {
        if (m.role === "user") addUser(m.text);
        else { hideEmpty(); const n = el("div", "msg msg-assistant"); n.appendChild(el("div", "msg-body", m.text)); $("messages").appendChild(n); }
      }
      scrollMessages();
    } catch (e) {
      toast(`Could not load history: ${e.message}`, "err");
    }
  }

  // ---------- input ----------
  async function send(text) {
    text = text.trim();
    if (!text || ui.busy) return;
    $("input").value = "";
    addUser(text);
    ui.busy = true;
    updateCore();
    try {
      const res = await api("/api/chat", { method: "POST", body: JSON.stringify({ text }) });
      if (res && res.ok && !ui.renderedTurns.has(res.request_id)) {
        ui.renderedTurns.add(res.request_id);
        addAssistant(res);
      }
    } catch (e) {
      addError(e.message);
    } finally {
      ui.busy = false;
      removeThinking();
      updateCore();
      $("input").focus();
    }
  }

  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("input").value); });
  $("btn-cancel").addEventListener("click", () => api("/api/chat/cancel", { method: "POST" }).catch((e) => toast(e.message, "err")));

  const SUGGESTIONS = ["System status", "List speakers", "Pause music in the kitchen", "Open notepad", "Vad är klockan?"];
  $("suggestions").replaceChildren(...SUGGESTIONS.map((s) => {
    const b = el("button", "suggestion", s);
    b.type = "button";
    b.addEventListener("click", () => send(s));
    return b;
  }));

  // ---------- settings drawer ----------
  function openDrawer(open) {
    $("drawer").hidden = !open;
    $("scrim").hidden = !open;
    if (open) $("btn-close-settings").focus();
  }
  $("btn-settings").addEventListener("click", () => openDrawer(true));
  $("btn-close-settings").addEventListener("click", () => openDrawer(false));
  $("scrim").addEventListener("click", () => openDrawer(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") openDrawer(false); });

  async function patch(obj) {
    try {
      await api("/api/settings", { method: "PATCH", body: JSON.stringify(obj) });
      await refreshState();
    } catch (e) {
      toast(e.message, "err");
      refreshState();
    }
  }
  const bindToggle = (id, key) => $(id).addEventListener("change", (e) => patch({ [key]: e.target.checked }));
  bindToggle("set-dry-computer", "dry_run_computer");
  bindToggle("set-dry-home", "dry_run_smart_home");
  bindToggle("set-advanced", "advanced_mode");
  bindToggle("set-local-only", "local_only");
  bindToggle("set-mic", "microphone_enabled");
  bindToggle("set-camera", "camera_enabled");
  bindToggle("set-speak", "speak_replies");
  $("set-profile").addEventListener("change", (e) => patch({ active_profile: e.target.value }));
  $("set-sensitivity").addEventListener("change", (e) => patch({ cloud_max_sensitivity: e.target.value }));

  $("btn-reset-conv").addEventListener("click", () => api("/api/conversation/reset", { method: "POST" }).catch((e) => toast(e.message, "err")));
  $("btn-clear-history").addEventListener("click", () => api("/api/history", { method: "DELETE" }).catch((e) => toast(e.message, "err")));
  $("btn-refresh").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { ui.state = await api("/api/integrations/refresh", { method: "POST" }); renderAll(); }
    catch (err) { toast(err.message, "err"); }
    finally { e.target.disabled = false; }
  });
  $("btn-diagnostics").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const out = $("diag-out");
    out.hidden = false;
    out.textContent = "Inspecting this machine…";
    try { out.textContent = JSON.stringify(await api("/api/diagnostics?refresh=true"), null, 2); }
    catch (err) { out.textContent = `Diagnostics failed: ${err.message}`; }
    finally { e.target.disabled = false; }
  });

  // Confirmation countdowns
  setInterval(() => { if (ui.state && (ui.state.pending_confirmations || []).length) renderConfirmations(ui.state); }, 1000);

  updateCore();
  connect();
})();
