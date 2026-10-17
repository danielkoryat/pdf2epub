"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  return node;
};

let MODE = "local";
let ME = null;
let INFO = {};

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    if (res.status === 401 && MODE === "server") showSignIn();
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------------------------------------------------------- analytics (PostHog, when configured) */

let analytics = null;

function loadAnalytics(config) {
  // Loaded from PostHog's own host (allowed by the page's Content-Security-Policy); book titles and file names
  // are never sent, only event names, kinds and sizes. No screen recording (see the privacy policy).
  const assets = config.host.replace(".i.posthog.com", "-assets.i.posthog.com");
  const script = el("script", { src: `${assets}/static/array.js`, async: true });
  script.addEventListener("load", () => {
    if (!window.posthog) return;
    window.posthog.init(config.key, {
      api_host: config.host, person_profiles: "identified_only", capture_pageview: true,
      autocapture: false, disable_session_recording: true,
    });
    analytics = window.posthog;
    if (ME) analytics.identify(ME.id, { email: ME.email, name: ME.name });
  });
  document.head.append(script);
}

function track(event, props = {}) {
  try { analytics?.capture(event, props); } catch { /* analytics never breaks the app */ }
}

/* ---------------------------------------------------------------- account */

function showSignIn() {
  for (const id of ["home", "review", "review-epub", "admin"]) $(`#${id}`).classList.add("hidden");
  $("#signin").classList.remove("hidden");
  $("#account").replaceChildren();
  const reason = new URLSearchParams(location.search).get("signin");
  const messages = {
    failed: "Signing in didn't work. Please try again.",
    unverified: "Your Google account's e-mail address isn't verified.",
    blocked: "This account has been blocked. If you think this is a mistake, contact the site owner.",
  };
  const msg = $("#signin-msg");
  msg.textContent = messages[reason] || "";
  msg.classList.toggle("hidden", !messages[reason]);
  $("#guest-box").classList.toggle("hidden", !INFO.guests);
}

/* Guests: a library for this browser without signing in, after Cloudflare's bot check when it is on. */

function signinError(text) {
  const msg = $("#signin-msg");
  msg.textContent = text;
  msg.classList.remove("hidden");
}

async function startGuest(token = "") {
  try {
    await api("/auth/guest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ turnstile: token }) });
  } catch (err) {
    signinError(err.message);
    $("#guest-start").disabled = false;
    return;
  }
  track("guest_started");
  await loadMe();
  show("home");
  refreshJobs();
}

function loadTurnstile() {
  return new Promise((resolve, reject) => {
    if (window.turnstile) return resolve(window.turnstile);
    const script = el("script", { src: "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit", async: true });
    script.addEventListener("load", () => resolve(window.turnstile));
    script.addEventListener("error", () => reject(new Error("The browser check could not load. Please sign in with Google instead.")));
    document.head.append(script);
  });
}

$("#guest-start").addEventListener("click", async () => {
  $("#guest-start").disabled = true;
  $("#signin-msg").classList.add("hidden");
  if (!INFO.turnstile) { startGuest(); return; }
  try {
    const turnstile = await loadTurnstile();
    turnstile.render("#turnstile", {
      sitekey: INFO.turnstile,
      theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
      callback: token => startGuest(token),
      "error-callback": () => { signinError("The browser check failed. Please try again."); $("#guest-start").disabled = false; },
    });
  } catch (err) {
    signinError(err.message);
    $("#guest-start").disabled = false;
  }
});

async function loadMe() {
  ME = await api("/api/me");
  renderAccount();
  if (analytics) analytics.identify(ME.id, { email: ME.email, name: ME.name });
  return ME;
}

function renderAccount() {
  const box = $("#account");
  if (MODE !== "server" || !ME) { box.replaceChildren(); return; }
  const b = ME.balance;
  if (ME.role === "guest") {
    box.replaceChildren(
      el("div", { class: "who" }, el("span", {}, "Guest"),
        el("span", { class: "muted", title: "Free pages for guests this month" }, `${b.available.toLocaleString()} pages left`)),
      el("a", { class: "button", href: "/auth/login", title: "Keep your books and get more pages" },
        el("button", { type: "button", class: "primary" }, "Sign in with Google")),
    );
    return;
  }
  if (b.unlimited) {
    box.replaceChildren(...[
      ME.picture ? el("img", { class: "avatar", src: ME.picture, alt: "", referrerpolicy: "no-referrer" }) : null,
      el("div", { class: "who" }, el("span", {}, ME.name || ME.email), el("span", { class: "muted" }, "Unlimited pages")),
      ME.role === "admin" ? el("button", { type: "button", class: "ghost", onclick: openAdmin }, "Dashboard") : null,
      el("button", { type: "button", class: "ghost", onclick: signOut }, "Sign out"),
    ].filter(Boolean));
    return;
  }
  const total = b.free_allowance + b.paid;
  const pct = total ? Math.round((100 * b.available) / total) : 0;
  const pages = el("div", { class: "who" },
    el("span", {}, ME.name || ME.email),
    el("span", { class: "muted", title: `${b.free_left} of ${b.free_allowance} free pages left this month${b.paid ? `, plus ${b.paid} paid pages` : ""}` },
      `${b.available.toLocaleString()} pages left`),
    el("div", { class: `meter ${pct < 15 ? "low" : ""}`, role: "meter", "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(b.available), "aria-label": "Pages left" },
      el("div", { style: `width:${pct}%` })),
  );
  box.replaceChildren(...[
    ME.picture ? el("img", { class: "avatar", src: ME.picture, alt: "", referrerpolicy: "no-referrer" }) : null,
    pages,
    ME.role === "admin" ? el("button", { type: "button", class: "ghost", onclick: openAdmin }, "Dashboard") : null,
    el("button", { type: "button", class: "ghost", onclick: signOut }, "Sign out"),
  ].filter(Boolean));
}

async function signOut() {
  try { await api("/auth/logout", { method: "POST" }); } catch { /* signed out anyway */ }
  try { analytics?.reset(); } catch { /* ignore */ }
  location.href = "/";
}

const STATUS = {
  queued: "Waiting", preparing: "Converting", review: "Ready for review",
  building: "Building", done: "Done", failed: "Failed",
};
const ACTIVE = new Set(["queued", "preparing", "building"]);

/* ---------------------------------------------------------------- header chips */

async function loadInfo() {
  try {
    const info = await api("/api/info");
    INFO = info;
    MODE = info.mode;
    if (info.posthog) loadAnalytics(info.posthog);
    const chips = $("#chips");
    if (MODE === "server") { chips.replaceChildren(); return info; }
    chips.replaceChildren(
      el("span", { class: `chip ${info.tesseract ? "ok" : "warn"}`, title: "Needed for scanned PDFs" },
        info.tesseract ? "OCR ready" : "Tesseract missing"),
      el("span", { class: `chip ${info.epubcheck ? "ok" : ""}`, title: "Validates the EPUB" },
        info.epubcheck ? "EPUBCheck ready" : "No EPUBCheck"),
    );
    return info;
  } catch { return { mode: "local" }; /* the page still works without it */ }
}

/* ---------------------------------------------------------------- upload */

const form = $("#upload");
const fileInput = $("#file");
const drop = $("#drop");
let chosen = null;

function choose(file) {
  if (!file) return;
  if (!/\.(pdf|epub)$/i.test(file.name) && !["application/pdf", "application/epub+zip"].includes(file.type)) {
    alertMessage(`${file.name} is not a PDF or EPUB file.`);
    return;
  }
  chosen = file;
  drop.classList.add("has-file");
  $("#drop-title").textContent = file.name;
  $("#drop-sub").textContent = `${(file.size / 1048576).toFixed(1)} MB · click to choose another`;
  $("#convert").disabled = false;
}

function alertMessage(text) {
  $("#drop-sub").textContent = text;
}

fileInput.addEventListener("change", () => choose(fileInput.files[0]));
for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); });
for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); });
drop.addEventListener("drop", e => choose(e.dataTransfer.files[0]));

form.addEventListener("submit", e => {
  e.preventDefault();
  if (!chosen) return;
  const data = new FormData(form);
  data.set("file", chosen, chosen.name);
  data.set("cover", form.cover.checked ? "true" : "false");
  data.set("review", form.review.checked ? "true" : "false");

  const bar = $("#upload-progress");
  bar.classList.remove("hidden");
  $("#convert").disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = ev => { if (ev.lengthComputable) bar.firstElementChild.style.width = `${(100 * ev.loaded) / ev.total}%`; };
  xhr.onload = () => {
    bar.classList.add("hidden");
    bar.firstElementChild.style.width = "0";
    if (xhr.status >= 200 && xhr.status < 300) {
      track("book_uploaded", { kind: /\.epub$/i.test(chosen.name) ? "epub" : "pdf", size_mb: Math.round(chosen.size / 1048576), review: form.review.checked });
      chosen = null;
      form.reset();
      drop.classList.remove("has-file");
      $("#drop-title").textContent = "Drop a PDF here or click to choose";
      $("#drop-sub").textContent = "Born-digital or scanned · Hebrew, English and other languages";
      refreshJobs();
    } else {
      let msg = "Upload failed";
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* keep default */ }
      alertMessage(msg);
      $("#convert").disabled = false;
    }
  };
  xhr.onerror = () => { bar.classList.add("hidden"); alertMessage("Upload failed: is the server still running?"); $("#convert").disabled = false; };
  xhr.send(data);
});

/* ---------------------------------------------------------------- jobs list */

let pollTimer = null;

function fmtSeconds(s) {
  if (!s && s !== 0) return "";
  const m = Math.floor(s / 60);
  return m ? `${m} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`;
}

function jobCard(job) {
  const status = el("span", { class: `badge ${job.status}` }, STATUS[job.status] || job.status);
  const sub = [job.kind === "epub" ? "EPUB enrichment" : "PDF conversion"];
  if (job.authors) sub.push(job.authors);
  if (job.summary && job.kind === "epub") {
    const s = job.summary;
    sub.push(`EPUB ${s.version}`);
    if (!job.report) sub.push(`${s.suggestions.length} suggestions`);
  } else if (job.summary) {
    const s = job.summary;
    sub.push(`${s.pages} pages${s.scanned_pages ? ` (${s.scanned_pages} scanned, OCR ${s.ocr_lang})` : ""}`);
    sub.push(`${job.report ? job.report.toc_entries : s.toc_entries} chapters`);
  }
  if (job.report) {
    const r = job.report;
    if (r.changes) sub.push(`${r.changes.length} improvements`);
    sub.push(`${(r.size / 1048576).toFixed(1)} MB`);
    if (r.validation) sub.push(r.validation.ok ? "EPUBCheck passed" : `EPUBCheck: ${r.validation.errors.length} errors`);
  }

  const buttons = el("div", { class: "buttons" });
  if (job.status === "done") {
    buttons.append(el("a", { class: "button", href: `/api/jobs/${job.id}/epub`, onclick: () => track("book_downloaded", { kind: job.kind }) },
      el("button", { class: "primary", type: "button" }, "Download EPUB")));
  }
  if (job.summary && !ACTIVE.has(job.status)) {
    const label = job.kind === "epub"
      ? (job.status === "review" ? "Review & enrich" : "Change improvements")
      : (job.status === "review" ? "Review & build" : "Edit chapters");
    buttons.append(el("button", { type: "button", onclick: () => openReview(job.id) }, label));
  }
  if (!ACTIVE.has(job.status)) {
    buttons.append(el("button", { class: "ghost danger", type: "button", title: "Delete", onclick: () => removeJob(job) }, "Delete"));
  }

  const card = el("div", { class: "card job" },
    el("div", {},
      el("div", { class: "name" }, el("bdi", {}, job.title || job.filename), status),
      el("div", { class: "sub muted" }, sub.join(" · ") || job.filename),
    ),
    buttons,
  );

  if (ACTIVE.has(job.status)) {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
    const bar = el("div", { class: `progress ${job.total > 1 ? "" : "indeterminate"}` }, el("div", { style: job.total > 1 ? `width:${pct}%` : "" }));
    const label = job.status === "queued"
      ? (job.queue_position > 1 ? `Waiting · ${job.queue_position - 1} ahead in the queue` : job.queue_position === 1 ? "Next in the queue" : "Waiting for the previous job") :
      `${job.stage_label || "Starting"}${job.total > 1 ? ` · ${job.done}/${job.total} pages` : ""}`;
    card.append(el("div", { class: "bar" }, bar, el("span", { class: "muted" }, label)));
    if (job.log.length) card.append(el("div", { class: "log", dir: "auto" }, job.log.slice(-4).join("\n")));
  }
  if (job.status === "failed") {
    card.append(el("div", { class: "error" }, job.error || "Failed"));
  }
  if (job.status === "done" && job.report?.changes?.length) {
    card.append(el("details", { class: "changes-box" },
      el("summary", { class: "muted" }, "What was improved"),
      el("ul", { class: "changes" }, job.report.changes.map(c => el("li", { dir: "auto" }, c)))));
  }
  if (job.summary?.warnings?.length && job.status !== "failed") {
    card.append(el("div", { class: "error" }, job.summary.warnings.join(" ")));
  }
  return card;
}

let allJobs = [];
let activeKey = "";

function renderJobs() {
  const q = $("#search").value.trim().toLowerCase();
  const shown = q ? allJobs.filter(j => [j.title, j.authors, j.filename].join(" ").toLowerCase().includes(q)) : allJobs;
  $("#jobs").replaceChildren(...shown.map(jobCard));
  $("#no-jobs").classList.toggle("hidden", shown.length > 0);
  $("#no-jobs").textContent = q ? "No books match the search." : "Nothing here yet.";
}

async function refreshJobs() {
  clearTimeout(pollTimer);
  try {
    const before = activeKey;
    allJobs = await api("/api/jobs");
    activeKey = allJobs.filter(j => ACTIVE.has(j.status)).map(j => j.id).join();
    renderJobs();
    if (MODE === "server" && activeKey !== before) await loadMe(); // a job started or settled: pages changed
  } catch (err) {
    if (err.status === 401) return; // signed out: the sign-in screen is showing
    $("#jobs").replaceChildren(el("p", { class: "error" }, `Cannot reach the server: ${err.message}`));
  }
  pollTimer = setTimeout(refreshJobs, allJobs.some(j => ACTIVE.has(j.status)) ? 1500 : 6000);
}

$("#search").addEventListener("input", renderJobs);

async function removeJob(job) {
  if (!confirm(`Delete "${job.title || job.filename}" and its files?`)) return;
  try { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
  refreshJobs();
}

/* ---------------------------------------------------------------- review */

let current = null; // { id, draft, rows } for a PDF; { id, draft } for an EPUB

function show(section) {
  for (const id of ["home", "review", "review-epub", "admin", "signin"]) $(`#${id}`).classList.toggle("hidden", section !== id);
  window.scrollTo(0, 0);
}

async function openReview(jobId) {
  let draft;
  try { draft = await api(`/api/jobs/${jobId}/draft`); } catch (err) { alert(err.message); return; }
  if (draft.kind === "epub") openEpubReview(jobId, draft);
  else openPdfReview(jobId, draft);
}

/* ---- PDF: chapters -------------------------------------------------------------------------------------
   Each row of `current.rows` is one chapter of the draft with the user's edits:
   { ch, title, inToc, drop, merged (joined to the chapter above), splits: [{at, title}], selected } */

function openPdfReview(jobId, draft) {
  const edits = draft.edits || {};
  const byIndex = new Map((edits.chapters || []).map(c => [c.index, c]));
  current = {
    id: jobId,
    draft,
    rows: draft.chapters.map(ch => {
      const e = byIndex.get(ch.index) || {};
      return {
        ch,
        title: e.title ?? ch.title,
        inToc: e.in_toc ?? ch.in_toc,
        drop: !!e.drop,
        merged: !!e.merge,
        splits: e.splits || [],
        selected: false,
      };
    }),
  };

  $("#review-title").textContent = edits.title || draft.title;
  $("#review-title").dir = "auto";
  $("#m-title").value = edits.title || draft.title;
  $("#m-authors").value = (edits.authors || draft.authors || []).join(", ");
  $("#m-language").value = edits.language || draft.language;
  $("#m-cover").checked = edits.cover ?? true;
  $("#m-cover-wrap").classList.toggle("hidden", !draft.has_cover);
  const cover = $("#cover");
  cover.classList.toggle("hidden", !draft.has_cover);
  if (draft.has_cover) cover.src = `/api/jobs/${jobId}/cover`;

  const s = draft.summary || {};
  $("#m-summary").textContent = [
    `${s.pages} pages`,
    s.scanned_pages ? `${s.scanned_pages} OCR'd (${s.ocr_lang})` : "born-digital text",
    s.ocr_fixes ? `${s.ocr_fixes} OCR letter fixes` : null,
    `prepared in ${fmtSeconds(s.seconds)}`,
  ].filter(Boolean).join(" · ");
  $("#m-warnings").replaceChildren(...(s.warnings || []).map(w => el("li", {}, w)));
  renderChapters();
  show("review");
}

function groupOf(i) {
  // The rows merged into the chapter at index i (which itself is not merged into the one above).
  const rows = current.rows;
  const out = [];
  for (let j = i + 1; j < rows.length && rows[j].merged; j++) out.push(j);
  return out;
}

function renderChapters() {
  const rows = current.rows;
  const tbody = $("#chapters tbody");
  tbody.replaceChildren(...rows.map((r, i) => chapterRow(r, i)));
  const selected = rows.filter(r => r.selected).length;
  $("#merge-selected").disabled = selected < 2;
  $("#selection-note").textContent = selected ? `${selected} selected` : "Tick two or more chapters to merge them";
  $("#select-all").checked = selected > 0 && selected === rows.length;
  const toc = rows.reduce((n, r) => n + (r.drop || r.merged ? 0 : (r.inToc && r.title ? 1 : 0)) + r.splits.filter(s => s.title).length, 0);
  $("#build-note").textContent = `${toc} entries in the table of contents`;
}

function chapterRow(r, i) {
  const ch = r.ch;
  const front = !ch.title;
  const select = el("input", { type: "checkbox", checked: r.selected, "aria-label": "Select" });
  select.addEventListener("change", () => { r.selected = select.checked; renderChapters(); });
  // A chapter that others are merged into shows the pages and words of the whole group.
  const members = r.merged ? [ch] : [ch, ...groupOf(i).map(j => current.rows[j].ch)];
  const last = members[members.length - 1].last_page;
  const pages = ch.first_page === last ? `p. ${ch.first_page}` : `p. ${ch.first_page}–${last}`;
  const images = members.reduce((n, m) => n + m.images, 0);
  const words = members.reduce((n, m) => n + m.words, 0).toLocaleString() + (images ? ` · ${images} img` : "");
  const preview = el("button", { type: "button", class: "ghost", onclick: () => openPreview(i) }, "Preview");

  if (r.merged) {
    // Cut the group here: this chapter stands on its own again, together with any merged below it.
    const unmerge = el("button", { type: "button", class: "link", onclick: () => { r.merged = false; r.inToc = !!r.title; renderChapters(); } }, "Split off again");
    return el("tr", { class: `follower ${r.selected ? "selected" : ""}` },
      el("td", { class: "c" }, select),
      el("td", {}),
      el("td", {},
        el("div", { class: "merged-into" }, "↳ merged into the chapter above: ", el("bdi", {}, ch.title || ch.snippet.slice(0, 60) || "(untitled)"), " ", unmerge)),
      el("td", { class: "num" }, pages),
      el("td", { class: "num" }, words),
      el("td", {}),
      el("td", {}, preview),
    );
  }

  const inToc = el("input", { type: "checkbox", checked: r.inToc, "aria-label": "In table of contents" });
  inToc.addEventListener("change", () => { r.inToc = inToc.checked; renderChapters(); });
  const title = el("input", { class: "title", dir: "auto", value: r.title || "", placeholder: front ? "(untitled page)" : "" });
  title.addEventListener("input", () => {
    r.title = title.value;
    if (title.value.trim() && !r.inToc) { r.inToc = true; inToc.checked = true; }
    if (!title.value.trim()) { r.inToc = false; inToc.checked = false; }
    $("#build-note").textContent = "";
  });
  title.addEventListener("change", renderChapters);
  const drop = el("input", { type: "checkbox", checked: r.drop, "aria-label": "Remove" });
  drop.addEventListener("change", () => { r.drop = drop.checked; renderChapters(); });

  const badges = [];
  const group = groupOf(i);
  if (group.length) badges.push(el("span", { class: "group-badge" }, `${group.length + 1} chapters merged`));
  if (r.splits.length) {
    badges.push(el("button", { type: "button", class: "split-badge link", onclick: () => openPreview(i) },
      `+${r.splits.length} new chapter${r.splits.length > 1 ? "s" : ""} inside`));
  }
  return el("tr", { class: `${front ? "front" : ""} ${ch.level > 1 ? "sub" : ""} ${r.drop ? "dropped" : ""} ${r.selected ? "selected" : ""}` },
    el("td", { class: "c" }, select),
    el("td", { class: "c" }, inToc),
    el("td", {}, title, badges.length ? el("div", {}, badges) : null,
      ch.snippet ? el("div", { class: "snippet muted", dir: "auto" }, ch.snippet) : null),
    el("td", { class: "num" }, pages),
    el("td", { class: "num" }, words),
    el("td", { class: "c" }, drop),
    el("td", {}, preview),
  );
}

$("#select-all").addEventListener("change", e => {
  for (const r of current.rows) r.selected = e.target.checked;
  renderChapters();
});

$("#merge-selected").addEventListener("click", () => {
  // Merge the whole range from the first to the last selected chapter into the first one.
  const rows = current.rows;
  const picked = rows.map((r, i) => (r.selected ? i : -1)).filter(i => i >= 0);
  if (picked.length < 2) return;
  let lead = picked[0];
  while (lead > 0 && rows[lead].merged) lead--; // a selected follower merges with its existing group
  const last = picked[picked.length - 1];
  for (let j = lead + 1; j <= last; j++) { rows[j].merged = true; rows[j].drop = false; }
  for (const r of rows) r.selected = false;
  const leader = rows[lead];
  leader.drop = false;
  if (!leader.title) leader.title = rows.slice(lead, last + 1).map(r => r.title).find(Boolean) || "";
  leader.inToc = !!leader.title;
  renderChapters();
  // Put the cursor in the merged chapter's title so it can be renamed right away.
  const input = $("#chapters tbody").rows[lead].querySelector("input.title");
  if (input) { input.focus(); input.select(); }
});

function collectEdits() {
  return {
    title: $("#m-title").value.trim(),
    authors: $("#m-authors").value,
    language: $("#m-language").value.trim(),
    cover: $("#m-cover").checked,
    chapters: current.rows.map(r => ({
      index: r.ch.index,
      title: (r.title || "").trim() !== (r.ch.title || "") ? (r.title || "").trim() : null,
      in_toc: r.inToc,
      merge: r.merged,
      drop: r.drop,
      splits: r.splits.filter(s => s.at > 0),
    })),
  };
}

$("#back").addEventListener("click", () => { show("home"); refreshJobs(); });

$("#build").addEventListener("click", async () => {
  if (!current) return;
  $("#build").disabled = true;
  try {
    await api(`/api/jobs/${current.id}/build`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collectEdits()),
    });
    track("book_built", { kind: "pdf", chapters: current.rows.filter(r => !r.drop).length });
    show("home");
    refreshJobs();
  } catch (err) {
    alert(err.message);
  } finally {
    $("#build").disabled = false;
  }
});

/* ---- PDF: chapter preview, with "start a new chapter here" ---------------------------------------------- */

let previewRow = null;

async function openPreview(i) {
  previewRow = current.rows[i];
  const ch = previewRow.ch;
  const dialog = $("#preview");
  $("#preview-title").textContent = previewRow.title || ch.title || `Pages ${ch.first_page}–${ch.last_page}`;
  $("#preview-title").dir = "auto";
  const body = $("#preview-body");
  body.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  dialog.showModal();
  let data;
  try { data = await api(`/api/jobs/${current.id}/chapters/${ch.index}`); } catch (err) { body.replaceChildren(el("p", { class: "error" }, err.message)); return; }
  previewRow.blocks = data.blocks;
  body.dir = data.direction;
  renderPreview();
}

function renderPreview() {
  const r = previewRow;
  const ch = r.ch;
  const body = $("#preview-body");
  const splitAt = new Map(r.splits.map(s => [s.at, s]));
  let page = null;
  const nodes = [el("p", { class: "muted", style: "text-indent:0;font:13px system-ui,sans-serif" },
    "Hover a paragraph and click ✂ to start a new chapter there.")];
  for (const b of r.blocks) {
    if (b.kind !== "footnote" && b.page !== page) {
      page = b.page;
      nodes.push(el("span", { class: "page" }, `page ${page}`));
    }
    const split = splitAt.get(b.n);
    if (split) nodes.push(splitLine(split));
    let node;
    if (b.kind === "heading") node = el(b.n === 0 ? "h1" : "h3", { dir: "auto" }, b.text);
    else if (b.kind === "image") node = el("figure", {}, el("img", { src: `/api/jobs/${current.id}/chapters/${ch.index}/images/${b.n}`, alt: "" }));
    else if (b.kind === "footnote") node = el("p", { class: "note", dir: "auto" }, b.text);
    else if (b.kind === "caption") node = el("p", { class: "muted", dir: "auto" }, b.text);
    else node = el("p", { dir: "auto" }, b.text);
    if (b.n > 0 && !split) {
      node = el("div", { class: "block" }, node,
        el("button", { type: "button", class: "cut", title: "Start a new chapter here", onclick: () => addSplit(b) }, "✂"));
    }
    nodes.push(node);
  }
  if (nodes.length === 1) nodes.push(el("p", { class: "muted" }, "This section has no text."));
  body.replaceChildren(...nodes);
}

function splitLine(split) {
  const input = el("input", { value: split.title, dir: "auto", placeholder: "New chapter title" });
  input.addEventListener("input", () => { split.title = input.value; renderChapters(); });
  const remove = el("button", { type: "button", class: "ghost small", title: "Remove this split" }, "✕");
  remove.addEventListener("click", () => {
    previewRow.splits = previewRow.splits.filter(s => s !== split);
    renderPreview();
    renderChapters();
  });
  return el("div", { class: "split-line" }, el("span", {}, "✂ New chapter:"), input, remove);
}

function addSplit(block) {
  const guess = block.kind === "heading" ? block.text : "";
  previewRow.splits.push({ at: block.n, title: guess });
  previewRow.splits.sort((a, b) => a.at - b.at);
  renderPreview();
  renderChapters();
  const inputs = $("#preview-body").querySelectorAll(".split-line input");
  const added = [...inputs].find(inp => inp.value === guess && !inp.value) || inputs[inputs.length - 1];
  if (added) added.focus();
}

$("#preview-close").addEventListener("click", () => $("#preview").close());
$("#preview").addEventListener("click", e => { if (e.target === e.currentTarget) e.currentTarget.close(); });

/* ---- EPUB: enrichment ------------------------------------------------------------------------------- */

const EPUB_OPTIONS = [
  { key: "upgrade", label: "Upgrade to EPUB 3", help: "Adds a navigation document built from the old table of contents. Needed for most of the improvements below.",
    applies: s => !s.version.startsWith("3"), present: () => false },
  { key: "fix_language", label: "Language", help: "Sets a missing language, or corrects it when the text is plainly in another script.",
    applies: () => true, present: s => !!s.language && (!s.detected_language || s.language.split("-")[0] === s.detected_language) },
  { key: "direction", label: "Right-to-left settings and language tags", help: "Right-to-left page turning and Kindle's writing mode for Hebrew and Arabic; language and direction on chapter files that lack them.",
    applies: () => true, present: s => s.rtl_ok },
  { key: "contents_page", label: "Contents page", help: "A page listing the chapters, placed before the text, built from the book's navigation.",
    applies: s => s.toc_entries >= 2, present: s => s.has_contents_page },
  { key: "landmarks", label: "Navigation landmarks", help: "Lets readers jump to the contents and the start of the text.",
    applies: () => true, present: s => s.has_landmarks },
  { key: "accessibility", label: "Accessibility metadata", help: "Describes the book for stores and assistive technology.",
    applies: () => true, present: s => s.has_accessibility },
  { key: "cover", label: "Mark the cover image", help: "So Kindle and other readers show the cover in the library.",
    applies: s => s.has_cover, present: s => s.cover_marked },
  { key: "ocr_fixes", label: "Fix OCR letter mix-ups", help: "Hebrew: corrects non-words caused by ד/ר, ה/ח and ב/כ confusions in books that were OCR'd. Changes the text; off unless you choose it.",
    applies: s => ["he", "yi"].includes(((s.language || s.detected_language || "").split("-")[0])), present: () => false, optional: true },
];

function openEpubReview(jobId, draft) {
  current = { id: jobId, draft };
  const s = draft.summary;
  const e = draft.edits || {};
  $("#e-head").textContent = e.title || s.title || "Enrich EPUB";
  $("#e-title").value = e.title ?? s.title;
  $("#e-authors").value = typeof e.authors === "string" ? e.authors : (e.authors || s.authors || []).join(", ");
  $("#e-language").value = e.language ?? (s.language || s.detected_language || "");
  updateEpubCover(draft.has_cover, draft.new_cover);

  const before = s.validation_before;
  $("#e-summary").textContent = [
    `EPUB ${s.version}`,
    `${s.chapters} chapter files`,
    `${s.toc_entries} contents entries`,
    `${s.words.toLocaleString()} words`,
    s.language ? `language ${s.language}` : "no language set",
    before ? (before.ok ? "EPUBCheck: valid" : `EPUBCheck: ${before.count} errors in the original`) : null,
  ].filter(Boolean).join(" · ");

  const list = $("#e-options");
  list.replaceChildren(...EPUB_OPTIONS.filter(o => o.applies(s)).map(o => {
    const present = o.present(s);
    const checked = e[o.key] ?? (o.optional ? false : !present);
    const box = el("input", { type: "checkbox", checked, "data-key": o.key, id: `opt-${o.key}` });
    const state = o.optional ? el("span", { class: "state optional" }, "optional")
      : el("span", { class: `state ${present ? "present" : "missing"}` }, present ? "already fine" : "missing");
    return el("div", { class: "option" }, box,
      el("label", { class: "label", for: `opt-${o.key}` }, o.label), state,
      el("div", { class: "help" }, o.help));
  }));
  $("#e-validation").replaceChildren(...(before && !before.ok ? before.errors.slice(0, 5).map(x => el("li", {}, `In the original: ${x}`)) : []));
  $("#e-note").textContent = `${s.suggestions.length} suggestions`;
  show("review-epub");
}

function updateEpubCover(hasCover, isNew) {
  const img = $("#e-cover");
  img.classList.toggle("hidden", !hasCover);
  $("#e-nocover").classList.toggle("hidden", hasCover);
  if (hasCover) img.src = `/api/jobs/${current.id}/cover?t=${Date.now()}`;
  $("#e-cover-undo").classList.toggle("hidden", !isNew);
}

$("#e-cover-file").addEventListener("change", async e => {
  const file = e.target.files[0];
  if (!file) return;
  const data = new FormData();
  data.set("file", file, file.name);
  try {
    await api(`/api/jobs/${current.id}/cover`, { method: "POST", body: data });
    updateEpubCover(true, true);
  } catch (err) { alert(err.message); }
  e.target.value = "";
});

$("#e-cover-undo").addEventListener("click", async () => {
  await api(`/api/jobs/${current.id}/cover`, { method: "DELETE" });
  const draft = await api(`/api/jobs/${current.id}/draft`);
  updateEpubCover(draft.has_cover, false);
});

$("#back-epub").addEventListener("click", () => { show("home"); refreshJobs(); });

$("#e-build").addEventListener("click", async () => {
  if (!current) return;
  const edits = {
    title: $("#e-title").value.trim(),
    authors: $("#e-authors").value,
    language: $("#e-language").value.trim(),
    new_cover: !$("#e-cover-undo").classList.contains("hidden"),
  };
  for (const box of $("#e-options").querySelectorAll("input[type=checkbox]")) edits[box.dataset.key] = box.checked;
  $("#e-build").disabled = true;
  try {
    await api(`/api/jobs/${current.id}/build`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(edits) });
    track("book_enriched", { new_cover: edits.new_cover });
    show("home");
    refreshJobs();
  } catch (err) {
    alert(err.message);
  } finally {
    $("#e-build").disabled = false;
  }
});

/* ---------------------------------------------------------------- admin dashboard (server mode) */

const fmtNum = n => (n >= 10000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : Number(n).toLocaleString());
const fmtBytes = n => (n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`);
const fmtTime = ts => new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
let countryNames = null;
try { countryNames = new Intl.DisplayNames([navigator.language || "en"], { type: "region" }); } catch { /* old browser */ }
const countryName = cc => (cc && cc !== "unknown" && /^[A-Z]{2}$/.test(cc) ? countryNames?.of(cc) || cc : cc === "TOR" ? "Tor network" : "Unknown");
const browserOf = ua => {
  const m = /(Edg|OPR|Chrome|Firefox|Safari)\/[\d.]+/.exec(ua || "");
  const os = /(Windows|Android|iPhone|iPad|Mac OS X|Linux)/.exec(ua || "");
  const name = m ? { Edg: "Edge", OPR: "Opera" }[m[1]] || m[1] : "Other";
  return `${name}${os ? ` · ${os[1].replace("Mac OS X", "macOS")}` : ""}`;
};

let adminTab = "overview";
let auditBefore = null;

async function openAdmin() {
  show("admin");
  await showAdminTab(adminTab);
}

$("#back-admin").addEventListener("click", () => { show("home"); refreshJobs(); });
for (const button of $("#a-tabs").querySelectorAll("button")) {
  button.addEventListener("click", () => showAdminTab(button.dataset.tab));
}

async function showAdminTab(tab) {
  adminTab = tab;
  for (const button of $("#a-tabs").querySelectorAll("button")) {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  for (const name of ["overview", "users", "signins", "audit", "jobs"]) $(`#a-${name}`).classList.toggle("hidden", name !== tab);
  try {
    if (tab === "overview") await loadOverview();
    else if (tab === "users") await loadUsers();
    else if (tab === "signins") await loadSignins();
    else if (tab === "audit") { auditBefore = null; await loadAudit(false); }
    else if (tab === "jobs") await loadAdminJobs();
  } catch (err) {
    alert(err.message);
  }
}

/* ---- overview: stat tiles, three single-series column charts, countries ---- */

function tile(label, value, note = "", hero = false) {
  return el("div", { class: `tile ${hero ? "hero" : ""}` },
    el("div", { class: "label" }, label), el("div", { class: "value" }, value), note ? el("div", { class: "note" }, note) : null);
}

async function loadOverview() {
  const data = await api("/api/admin/overview");
  const s = data.stats;
  $("#a-tz").textContent = `Days in ${data.timezone}`;
  $("#a-tiles").replaceChildren(
    tile("Active users today", fmtNum(s.users_active_today), `${fmtNum(s.users_total)} users in total`, true),
    tile("Sign-ins today", fmtNum(s.signins_today), s.failed_signins_today ? `${s.failed_signins_today} failed` : ""),
    tile("New users today", fmtNum(s.users_new_today)),
    tile("Books today", fmtNum(s.books_today), `${fmtNum(s.books_total)} in total`),
    tile("Pages today", fmtNum(s.pages_today), `${fmtNum(s.pages_total)} converted in total`),
    tile("Failed today", fmtNum(s.failed_today)),
    tile("Queue", fmtNum(s.queued), `${s.running} converting now`),
    tile("Storage", fmtBytes(s.storage_bytes)),
    tile("Blocked requests today", fmtNum(s.blocked_requests_today), `${s.users_blocked} blocked users`),
  );
  const charts = $("#a-charts");
  charts.replaceChildren(
    columnChart("Active users per day", data.days.map(d => ({ label: d.date, value: d.active_users }))),
    columnChart("Books per day", data.days.map(d => ({ label: d.date, value: d.books }))),
    columnChart("Pages converted per day", data.days.map(d => ({ label: d.date, value: d.pages }))),
  );
  const max = Math.max(1, ...data.countries.map(c => c[1]));
  $("#a-countries tbody").replaceChildren(...(data.countries.length ? data.countries.map(([cc, n]) => el("tr", {},
    el("td", {}, countryName(cc)),
    el("td", { class: "num" }, fmtNum(n)),
    el("td", {}, el("div", { class: "inline-bar", style: `width:${(100 * n) / max}%` })),
  )) : [el("tr", {}, el("td", { colspan: "3", class: "muted" }, "No sign-ins yet."))]));
}

const tip = $("#tooltip");
function showTip(target, value, label) {
  tip.replaceChildren(el("strong", {}, value), el("span", { class: "muted" }, label));
  tip.classList.remove("hidden");
  const r = target.getBoundingClientRect();
  const w = tip.offsetWidth;
  tip.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8)}px`;
  tip.style.top = `${Math.max(8, r.top - tip.offsetHeight - 8)}px`;
}
const hideTip = () => tip.classList.add("hidden");

function niceMax(v) {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

// A single-series column chart (no legend: the title names it): <=24px bars with 4px rounded tops on one
// baseline, a 2px surface gap between neighbours, hairline grid, hover/focus tooltips, and a table view.
function columnChart(title, points) {
  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs = {}) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const total = points.reduce((a, p) => a + p.value, 0);
  const card = el("div", { class: "card viz" });
  const tableBtn = el("button", { type: "button", class: "ghost toggle" }, "Show table");
  card.append(el("div", { class: "chart-head" }, el("h3", {}, title), el("span", { class: "total" }, `${fmtNum(total)} in 30 days`)));
  const svg = svgEl("svg", { role: "img", "aria-label": `${title}: ${fmtNum(total)} in the last 30 days` });
  card.append(svg, tableBtn);
  const table = el("table", { class: "data hidden" },
    el("thead", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "Value"))),
    el("tbody", {}, points.slice().reverse().map(p => el("tr", {}, el("td", {}, p.label), el("td", { class: "num" }, fmtNum(p.value))))));
  card.append(table);
  tableBtn.addEventListener("click", () => {
    table.classList.toggle("hidden");
    tableBtn.textContent = table.classList.contains("hidden") ? "Show table" : "Hide table";
  });

  const draw = () => {
    const W = Math.max(svg.clientWidth || 300, 200), H = 160, left = 30, right = 4, top = 8, bottom = 20;
    const plotW = W - left - right, plotH = H - top - bottom;
    const max = niceMax(Math.max(...points.map(p => p.value)));
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.replaceChildren();
    for (const t of [0, max / 2, max]) {
      const y = top + plotH - (t / max) * plotH;
      svg.append(svgEl("line", { class: t ? "gridline" : "baseline", x1: left, x2: W - right, y1: y, y2: y }));
      const label = svgEl("text", { class: "tick", x: left - 6, y: y + 4, "text-anchor": "end" });
      label.textContent = fmtNum(t);
      svg.append(label);
    }
    const band = plotW / points.length;
    const bw = Math.max(2, Math.min(24, band - 2));
    points.forEach((p, i) => {
      const x = left + i * band + (band - bw) / 2;
      const h = (p.value / max) * plotH;
      const base = top + plotH;
      if (h > 0) {
        const r = Math.min(4, bw / 2, h);
        const d = `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + bw - r} Q${x + bw},${base - h} ${x + bw},${base - h + r} V${base} Z`;
        svg.append(svgEl("path", { class: "bar", d }));
      }
      const hit = svgEl("rect", { class: "hit", x: left + i * band, y: top, width: band, height: plotH, tabindex: "0",
        "aria-label": `${p.label}: ${fmtNum(p.value)}` });
      const show = () => showTip(hit, fmtNum(p.value), new Date(`${p.label}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" }));
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      svg.append(hit, svgEl("rect", { class: "bar-hover", x, y: base - Math.max(h, 1), width: bw, height: Math.max(h, 1), rx: 2 }));
      const last = points.length - 1;
      if (i === 0 || i === last || ((last - i) % 7 === 0 && i >= 6)) { // weekly, counted back from today
        const label = svgEl("text", { class: "tick", x: x + bw / 2, y: H - 4, "text-anchor": i === 0 ? "start" : i === points.length - 1 ? "end" : "middle" });
        label.textContent = new Date(`${p.label}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" });
        svg.append(label);
      }
    });
  };
  requestAnimationFrame(draw);
  new ResizeObserver(() => draw()).observe(svg);
  return card;
}

/* ---- users ---- */

let userSearchTimer = null;
$("#a-user-search").addEventListener("input", () => { clearTimeout(userSearchTimer); userSearchTimer = setTimeout(loadUsers, 250); });

async function loadUsers() {
  const users = await api(`/api/admin/users?q=${encodeURIComponent($("#a-user-search").value.trim())}`);
  $("#a-users-table tbody").replaceChildren(...users.map(u => {
    const grant = el("input", { type: "number", min: "1", value: "100", "aria-label": "Pages to grant" });
    const blocked = u.status === "blocked";
    return el("tr", {},
      el("td", {}, el("div", { class: "user-cell" },
        u.picture ? el("img", { src: u.picture, alt: "", referrerpolicy: "no-referrer" }) : null,
        el("div", {}, el("div", {}, el("bdi", {}, u.name || "-"), " ",
          u.role === "admin" ? el("span", { class: "pill admin" }, "admin") : u.role === "guest" ? el("span", { class: "pill" }, "guest") : null),
          el("div", { class: "email" }, u.email || (u.role === "guest" ? `no account · ${u.last_ip}` : ""))))),
      el("td", {}, countryName(u.last_country || "unknown")),
      el("td", { class: "num" }, u.last_login ? fmtTime(u.last_login) : "-"),
      el("td", { class: "num" }, fmtNum(u.books)),
      el("td", { class: "num" }, fmtNum(u.pages_month)),
      el("td", { class: "num", title: u.balance.unlimited ? "Admins have no page limit" : `${u.balance.free_left} free + ${u.balance.paid} paid` },
        u.balance.unlimited ? "Unlimited" : fmtNum(u.balance.available)),
      el("td", { class: "num" }, fmtBytes(u.storage)),
      el("td", {}, el("span", { class: `pill ${blocked ? "blocked" : "ok"}` }, blocked ? "blocked" : "active")),
      el("td", {}, el("div", { class: "row-actions" },
        grant,
        el("button", { type: "button", onclick: () => userAction(u, "grant", { pages: Number(grant.value) }) }, "Grant pages"),
        u.id === ME?.id ? null : el("button", { type: "button", class: blocked ? "" : "danger",
          onclick: () => (blocked || confirm(`Block ${u.email}? They will be signed out at once.`)) && userAction(u, blocked ? "unblock" : "block") },
        blocked ? "Unblock" : "Block"))),
    );
  }));
}

async function userAction(user, action, body = null) {
  try {
    await api(`/api/admin/users/${user.id}/${action}`, body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : { method: "POST" });
    await loadUsers();
  } catch (err) { alert(err.message); }
}

/* ---- sign-ins, audit, jobs ---- */

async function loadSignins() {
  const rows = await api("/api/admin/signins");
  $("#a-signins-table tbody").replaceChildren(...rows.map(r => el("tr", {},
    el("td", { class: "num" }, fmtTime(r.ts)),
    el("td", {}, r.email || "-"),
    el("td", { class: "num" }, r.ip || "-"),
    el("td", {}, countryName(r.country || "unknown")),
    el("td", { title: r.user_agent }, browserOf(r.user_agent)),
    el("td", {}, el("span", { class: `pill ${r.outcome}` }, r.outcome === "ok" ? "signed in" : r.details?.reason || r.outcome)),
  )));
}

$("#a-audit-action").addEventListener("change", () => { auditBefore = null; loadAudit(false); });
$("#a-audit-outcome").addEventListener("change", () => { auditBefore = null; loadAudit(false); });
$("#a-audit-more").addEventListener("click", () => loadAudit(true));
$("#a-verify").addEventListener("click", async () => {
  const out = $("#a-verify-result");
  out.textContent = "Checking…";
  try {
    const r = await api("/api/admin/audit/verify");
    out.textContent = r.intact ? `✓ Intact: all ${fmtNum(r.entries)} entries verified` : "✗ The audit log was altered!";
  } catch (err) { out.textContent = err.message; }
});

async function loadAudit(more) {
  const q = new URLSearchParams({ action: $("#a-audit-action").value, outcome: $("#a-audit-outcome").value });
  if (more && auditBefore) q.set("before", auditBefore);
  const rows = await api(`/api/admin/audit?${q}`);
  if (rows.length) auditBefore = rows[rows.length - 1].id;
  const tbody = $("#a-audit-table tbody");
  const trs = rows.map(r => el("tr", {},
    el("td", { class: "num" }, fmtTime(r.ts)),
    el("td", {}, r.action),
    el("td", {}, el("span", { class: `pill ${r.outcome}` }, r.outcome)),
    el("td", {}, r.email || "-"),
    el("td", { class: "num" }, `${r.ip || "-"} ${r.country && r.country !== "unknown" ? r.country : ""}`.trim()),
    el("td", { class: "details" }, r.target || ""),
    el("td", { class: "details" }, Object.keys(r.details || {}).length ? JSON.stringify(r.details) : ""),
  ));
  if (more) tbody.append(...trs); else tbody.replaceChildren(...trs);
  $("#a-audit-more").classList.toggle("hidden", rows.length < 200);
}

async function loadAdminJobs() {
  const rows = await api("/api/admin/jobs");
  $("#a-jobs-table tbody").replaceChildren(...rows.map(j => el("tr", {},
    el("td", { class: "num" }, fmtTime(j.created)),
    el("td", {}, j.email || "-"),
    el("td", {}, j.kind.toUpperCase()),
    el("td", {}, el("span", { class: `pill ${j.status === "failed" ? "failed" : j.status === "done" ? "ok" : ""}` }, STATUS[j.status] || j.status)),
    el("td", { class: "num" }, j.pages ? fmtNum(j.pages) : "-"),
    el("td", { class: "num" }, j.size ? fmtBytes(j.size) : "-"),
    el("td", { class: "details" }, j.worker || ""),
    el("td", { class: "details" }, j.error || ""),
  )));
}

/* ---------------------------------------------------------------- start */

(async () => {
  await loadInfo();
  if (MODE === "server") {
    try { await loadMe(); } catch { showSignIn(); return; }
    history.replaceState(null, "", "/");
  }
  show("home");
  refreshJobs();
})();
