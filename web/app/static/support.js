/* Live support inbox. Plain JavaScript, no inline code, no libraries.
 * - polls /admin/support/api/<id>/messages?after=<last id> for NEW messages only,
 * - refreshes the ticket list from /admin/support/api/tickets,
 * - sends a reply with fetch and appends it when the server accepted it.
 * Every piece of text from the server is inserted with textContent (never as HTML). */
(() => {
  "use strict";
  const root = document.getElementById("support-live");
  if (!root) return;

  const ticketId = Number(root.dataset.ticket || 0);
  const csrf = root.dataset.csrf || "";
  const status = root.dataset.status || "all";
  const thread = document.getElementById("thread");
  const stateEl = document.getElementById("live-state");
  const newBtn = document.getElementById("new-msg");
  const POLL_MS = 2500;
  const LIST_MS = 5000;
  const HIDDEN_MS = 15000;
  const LABELS = { open: "waiting for support", answered: "answered", closed: "closed" };

  let lastId = Number(root.dataset.last || 0);
  let failures = 0;
  let stopped = false;
  let slowTimer = 0;
  let pollTimer = 0;
  let busy = false;

  /* ---------------------------------------------------------------- small helpers */
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const setState = (text, bad) => {
    if (!stateEl) return;
    stateEl.textContent = text;
    stateEl.classList.toggle("bad", !!bad);
  };
  const delay = (ms) => (document.hidden ? Math.max(ms, HIDDEN_MS) : ms);
  const getJSON = async (url) => {
    const r = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (r.status === 401) throw Object.assign(new Error("auth"), { auth: true });
    if (!r.ok) throw Object.assign(new Error("http"), { status: r.status });
    return r.json();
  };

  /* the last message the admin has seen per ticket, kept only in this browser */
  const seenKey = "support-seen";
  const loadSeen = () => {
    try {
      return JSON.parse(localStorage.getItem(seenKey) || "{}") || {};
    } catch (e) {
      return {};
    }
  };
  const saveSeen = (id, last) => {
    try {
      const seen = loadSeen();
      if ((seen[id] || 0) < last) {
        seen[id] = last;
        localStorage.setItem(seenKey, JSON.stringify(seen));
      }
    } catch (e) {
      /* storage may be blocked: the marker is a convenience only */
    }
  };

  /* ---------------------------------------------------------------- conversation */
  const nearBottom = () => !thread || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  const toBottom = () => {
    if (thread) thread.scrollTop = thread.scrollHeight;
    if (newBtn) newBtn.hidden = true;
  };
  const known = new Set();
  if (thread) {
    thread.querySelectorAll(".bubble[data-id]").forEach((b) => known.add(Number(b.dataset.id)));
  }

  const bubble = (m) => {
    const b = el("div", "bubble " + m.sender);
    b.dataset.id = String(m.id);
    b.appendChild(el("p", "said", m.text));
    const meta = el("p", "meta muted small");
    meta.appendChild(el("span", "", m.sender === "admin" ? "Support" : "User"));
    meta.appendChild(document.createTextNode(" · "));
    const when = el("span", "", m.at);
    when.title = m.at;
    meta.appendChild(when);
    if (m.admin) meta.appendChild(document.createTextNode(" · admin " + m.admin));
    b.appendChild(meta);
    return b;
  };

  /* Adds messages that are not on the page yet (by id), so a reply that the poll returns
   * again is never drawn twice. Scrolls only if the admin was already at the bottom. */
  const addMessages = (list, mine) => {
    const fresh = list.filter((m) => !known.has(m.id));
    if (!fresh.length || !thread) return;
    const follow = mine || nearBottom();
    fresh.forEach((m) => {
      known.add(m.id);
      thread.appendChild(bubble(m));
      lastId = Math.max(lastId, m.id);
    });
    if (follow) {
      toBottom();
      saveSeen(ticketId, lastId);
    } else if (newBtn && fresh.some((m) => m.sender === "user")) {
      newBtn.hidden = false;
    }
  };

  const applyTicket = (t) => {
    const badge = document.getElementById("ticket-status");
    if (badge) {
      badge.className = "badge " + t.status;
      badge.textContent = t.status_label;
    }
    if (t.closed && root.dataset.closed !== "1") {
      root.dataset.closed = "1";
      const form = document.getElementById("reply-form");
      if (form) form.replaceWith(el("p", "muted", "This ticket is closed. The user can open a new one."));
      document.querySelectorAll(".head-actions form.inline").forEach((f) => f.remove());
    }
  };

  const pollMessages = async () => {
    if (stopped || busy) return;
    busy = true;
    clearTimeout(slowTimer);
    slowTimer = setTimeout(() => setState("Updating…"), 700);
    let wait = POLL_MS;
    try {
      const data = await getJSON(`/admin/support/api/${ticketId}/messages?after=${lastId}`);
      failures = 0;
      addMessages(data.messages, false);
      applyTicket(data.ticket);
      setState("Live");
    } catch (e) {
      if (e.auth) {
        stopped = true;
        setState("Signed out: reload the page to sign in again", true);
      } else if (e.status === 404) {
        stopped = true;
        setState("This ticket no longer exists", true);
      } else {
        failures += 1;
        wait = Math.min(30000, POLL_MS * 2 ** Math.min(failures, 4));
        setState("Reconnecting…", true);
      }
    } finally {
      clearTimeout(slowTimer);
      busy = false;
    }
    if (!stopped) pollTimer = setTimeout(pollMessages, delay(wait));
  };

  /* ---------------------------------------------------------------- reply composer */
  const form = document.getElementById("reply-form");
  if (form) {
    const box = document.getElementById("reply");
    const send = document.getElementById("send-btn");
    const err = document.getElementById("send-error");
    let sending = false;
    const fail = (text) => {
      err.textContent = text;
      err.hidden = false;
    };
    const ERRORS = {
      empty: "Write something first.",
      too_long: "That reply is too long.",
      closed: "This ticket is closed.",
      full: "This ticket has reached its message limit.",
      rate_limited: "Too many actions in a short time. Wait a moment and retry.",
    };
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = box.value.trim();
      if (sending || !text) return;
      sending = true;
      send.disabled = true;
      err.hidden = true;
      try {
        const r = await fetch(`/admin/support/api/${ticketId}/reply`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
          body: new URLSearchParams({ csrf_token: csrf, reply: text }),
        });
        if (r.ok) {
          const data = await r.json();
          addMessages([data.message], true);
          applyTicket(data.ticket);
          box.value = ""; /* cleared only after the server accepted it */
          box.focus();
        } else {
          let kind = "";
          try {
            kind = (await r.json()).error || "";
          } catch (e) {
            /* not JSON: a blocked or expired session */
          }
          fail(ERRORS[kind] || "Not sent. Reload the page and sign in again if this keeps happening. Your text is kept.");
        }
      } catch (e) {
        fail("Not sent: no connection. Your text is kept; try again.");
      } finally {
        sending = false;
        send.disabled = false;
      }
    });
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }

  /* ---------------------------------------------------------------- ticket list */
  const list = () => document.getElementById("ticket-list");
  const unread = (t, seen) =>
    t.last_sender === "user" && t.last_id > (seen[t.id] || 0) && !(t.id === ticketId && nearBottom());

  const item = (t, seen) => {
    const li = el("li", "ticket");
    if (t.id === ticketId) li.classList.add("sel");
    if (t.status === "open") li.classList.add("waiting");
    const isNew = unread(t, seen);
    if (isNew) li.classList.add("unread");
    li.dataset.id = String(t.id);
    li.dataset.last = String(t.last_id);
    li.dataset.from = t.last_sender || "";
    const a = el("a");
    a.href = `/admin/support/${t.id}?status=${encodeURIComponent(status)}`;
    if (t.id === ticketId) a.setAttribute("aria-current", "page");
    const top = el("span", "t-top");
    top.appendChild(el("strong", "", "#" + t.id));
    top.appendChild(document.createTextNode(" "));
    top.appendChild(el("span", "t-user", "user " + t.user));
    top.appendChild(document.createTextNode(" "));
    top.appendChild(el("span", "badge " + t.status, LABELS[t.status] || t.status));
    const dot = el("span", "dot");
    dot.title = "New message";
    dot.hidden = !isNew;
    top.appendChild(dot);
    a.appendChild(top);
    a.appendChild(el("span", "t-prev", t.preview));
    const meta = el("span", "t-meta muted small");
    meta.appendChild(document.createTextNode(`${t.topic} · ${t.n} msg · `));
    meta.appendChild(el("span", "t-when", t.updated));
    a.appendChild(meta);
    li.appendChild(a);
    return li;
  };

  const refreshList = async () => {
    if (stopped) return;
    const current = list();
    let wait = LIST_MS;
    try {
      const page = current ? current.dataset.page || "0" : "0";
      const data = await getJSON(
        `/admin/support/api/tickets?status=${encodeURIComponent(status)}&page=${encodeURIComponent(page)}`
      );
      const seen = loadSeen();
      if (current && data.tickets.length) {
        const ul = el("ul", "tickets");
        ul.id = "ticket-list";
        ul.dataset.status = status;
        ul.dataset.page = page;
        data.tickets.forEach((t) => ul.appendChild(item(t, seen)));
        current.replaceWith(ul);
      }
    } catch (e) {
      if (e.auth) stopped = true;
      else wait = LIST_MS * 2;
    }
    if (!stopped) setTimeout(refreshList, delay(wait));
  };

  /* ---------------------------------------------------------------- start */
  const markInitialUnread = () => {
    const seen = loadSeen();
    document.querySelectorAll("#ticket-list li.ticket").forEach((li) => {
      const id = Number(li.dataset.id);
      const isNew = li.dataset.from === "user" && Number(li.dataset.last) > (seen[id] || 0) && id !== ticketId;
      li.classList.toggle("unread", isNew);
      const dot = li.querySelector(".dot");
      if (dot) dot.hidden = !isNew;
    });
  };
  markInitialUnread();

  if (thread && ticketId) {
    toBottom();
    saveSeen(ticketId, lastId);
    thread.addEventListener("scroll", () => {
      if (nearBottom()) {
        if (newBtn) newBtn.hidden = true;
        saveSeen(ticketId, lastId);
      }
    });
    if (newBtn) newBtn.addEventListener("click", toBottom);
    pollTimer = setTimeout(pollMessages, POLL_MS);
  } else {
    setState("Live");
  }
  setTimeout(refreshList, LIST_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && thread && ticketId && !stopped) {
      clearTimeout(pollTimer);
      pollMessages();
    }
  });
})();
