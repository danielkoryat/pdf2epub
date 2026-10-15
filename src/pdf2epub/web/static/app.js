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

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
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
    const chips = $("#chips");
    chips.replaceChildren(
      el("span", { class: `chip ${info.tesseract ? "ok" : "warn"}`, title: "Needed for scanned PDFs" },
        info.tesseract ? "OCR ready" : "Tesseract missing"),
      el("span", { class: `chip ${info.epubcheck ? "ok" : ""}`, title: "Validates the EPUB" },
        info.epubcheck ? "EPUBCheck ready" : "No EPUBCheck"),
    );
  } catch { /* the page still works without it */ }
}

/* ---------------------------------------------------------------- upload */

const form = $("#upload");
const fileInput = $("#file");
const drop = $("#drop");
let chosen = null;

function choose(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
    alertMessage(`${file.name} is not a PDF.`);
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
  const sub = [];
  if (job.summary) {
    const s = job.summary;
    sub.push(`${s.pages} pages${s.scanned_pages ? ` (${s.scanned_pages} scanned, OCR ${s.ocr_lang})` : ""}`);
    sub.push(`${job.report ? job.report.toc_entries : s.toc_entries} chapters`);
  }
  if (job.report) {
    const r = job.report;
    sub.push(`${(r.size / 1048576).toFixed(1)} MB`);
    if (r.validation) sub.push(r.validation.ok ? "EPUBCheck passed" : `EPUBCheck: ${r.validation.errors.length} errors`);
    sub.push(fmtSeconds(r.seconds));
  }

  const buttons = el("div", { class: "buttons" });
  if (job.status === "done") {
    buttons.append(el("a", { class: "button", href: `/api/jobs/${job.id}/epub` }, el("button", { class: "primary", type: "button" }, "Download EPUB")));
  }
  if (job.summary && !ACTIVE.has(job.status)) {
    buttons.append(el("button", { type: "button", onclick: () => openReview(job.id) }, job.status === "review" ? "Review & build" : "Edit chapters"));
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
    const label = job.status === "queued" ? "Waiting for the previous job" :
      `${job.stage_label || "Starting"}${job.total > 1 ? ` · ${job.done}/${job.total} pages` : ""}`;
    card.append(el("div", { class: "bar" }, bar, el("span", { class: "muted" }, label)));
    if (job.log.length) card.append(el("div", { class: "log", dir: "auto" }, job.log.slice(-4).join("\n")));
  }
  if (job.status === "failed") {
    card.append(el("div", { class: "error" }, job.error || "Failed"));
  }
  if (job.summary?.warnings?.length && job.status !== "failed") {
    card.append(el("div", { class: "error" }, job.summary.warnings.join(" ")));
  }
  return card;
}

async function refreshJobs() {
  clearTimeout(pollTimer);
  let jobs = [];
  try {
    jobs = await api("/api/jobs");
    $("#jobs").replaceChildren(...jobs.map(jobCard));
    $("#no-jobs").classList.toggle("hidden", jobs.length > 0);
  } catch (err) {
    $("#jobs").replaceChildren(el("p", { class: "error" }, `Cannot reach the server: ${err.message}`));
  }
  pollTimer = setTimeout(refreshJobs, jobs.some(j => ACTIVE.has(j.status)) ? 1500 : 6000);
}

async function removeJob(job) {
  if (!confirm(`Delete "${job.title || job.filename}" and its files?`)) return;
  try { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
  refreshJobs();
}

/* ---------------------------------------------------------------- review */

let current = null; // { id, draft }

function show(section) {
  $("#home").classList.toggle("hidden", section !== "home");
  $("#review").classList.toggle("hidden", section !== "review");
  window.scrollTo(0, 0);
}

async function openReview(jobId) {
  let draft;
  try { draft = await api(`/api/jobs/${jobId}/draft`); } catch (err) { alert(err.message); return; }
  current = { id: jobId, draft };
  const edits = draft.edits || {};
  const byIndex = new Map((edits.chapters || []).map(c => [c.index, c]));

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

  const tbody = $("#chapters tbody");
  tbody.replaceChildren(...draft.chapters.map(ch => chapterRow(ch, byIndex.get(ch.index))));
  updateBuildNote();
  show("review");
}

function chapterRow(ch, edit = {}) {
  const front = !ch.title;
  const inToc = el("input", { type: "checkbox", checked: edit.in_toc ?? ch.in_toc, "aria-label": "In table of contents" });
  const title = el("input", { class: "title", dir: "auto", value: edit.title ?? ch.title, placeholder: front ? "(untitled page)" : "" });
  const merge = el("input", { type: "checkbox", checked: !!edit.merge, disabled: ch.index === 0, "aria-label": "Merge into previous chapter" });
  const drop = el("input", { type: "checkbox", checked: !!edit.drop, "aria-label": "Remove" });
  const pages = ch.first_page === ch.last_page ? `p. ${ch.first_page}` : `p. ${ch.first_page}–${ch.last_page}`;
  const row = el("tr", { class: `${front ? "front" : ""} ${ch.level > 1 ? "sub" : ""}` },
    el("td", { class: "c" }, inToc),
    el("td", {}, title, ch.snippet ? el("div", { class: "snippet muted", dir: "auto" }, ch.snippet) : null),
    el("td", { class: "num" }, pages),
    el("td", { class: "num" }, ch.words.toLocaleString() + (ch.images ? ` · ${ch.images} img` : "")),
    el("td", { class: "c" }, merge),
    el("td", { class: "c" }, drop),
    el("td", {}, el("button", { type: "button", class: "ghost", onclick: () => openPreview(ch) }, "Preview")),
  );
  row.dataset.index = ch.index;
  row.dataset.original = ch.title;
  const sync = () => {
    row.classList.toggle("merged", merge.checked);
    row.classList.toggle("dropped", drop.checked);
    if (title.value.trim() === "") inToc.checked = false;
    updateBuildNote();
  };
  for (const input of [inToc, merge, drop]) input.addEventListener("change", sync);
  title.addEventListener("input", () => { if (title.value.trim() && !inToc.checked && !front) inToc.checked = true; sync(); });
  sync();
  return row;
}

function collectEdits() {
  const chapters = [...$("#chapters tbody").rows].map(row => {
    const [inToc, merge, drop] = row.querySelectorAll("input[type=checkbox]");
    const title = row.querySelector("input.title").value.trim();
    return {
      index: Number(row.dataset.index),
      title: title !== row.dataset.original ? title : null,
      in_toc: inToc.checked,
      merge: merge.checked,
      drop: drop.checked,
    };
  });
  return {
    title: $("#m-title").value.trim(),
    authors: $("#m-authors").value,
    language: $("#m-language").value.trim(),
    cover: $("#m-cover").checked,
    chapters,
  };
}

function updateBuildNote() {
  const rows = [...$("#chapters tbody").rows];
  const toc = rows.filter(r => {
    const [inToc, merge, drop] = r.querySelectorAll("input[type=checkbox]");
    return inToc.checked && !merge.checked && !drop.checked;
  }).length;
  $("#build-note").textContent = `${toc} entries in the table of contents`;
}

$("#back").addEventListener("click", () => { show("home"); refreshJobs(); });

$("#build").addEventListener("click", async () => {
  if (!current) return;
  $("#build").disabled = true;
  try {
    await api(`/api/jobs/${current.id}/build`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collectEdits()),
    });
    show("home");
    refreshJobs();
  } catch (err) {
    alert(err.message);
  } finally {
    $("#build").disabled = false;
  }
});

/* ---------------------------------------------------------------- chapter preview */

async function openPreview(ch) {
  const dialog = $("#preview");
  $("#preview-title").textContent = ch.title || `Pages ${ch.first_page}–${ch.last_page}`;
  $("#preview-title").dir = "auto";
  const body = $("#preview-body");
  body.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  dialog.showModal();
  let data;
  try { data = await api(`/api/jobs/${current.id}/chapters/${ch.index}`); } catch (err) { body.replaceChildren(el("p", { class: "error" }, err.message)); return; }
  body.dir = data.direction;
  let page = null;
  const nodes = [];
  for (const b of data.blocks) {
    if (b.kind !== "footnote" && b.page !== page) {
      page = b.page;
      nodes.push(el("span", { class: "page" }, `page ${page}`));
    }
    if (b.kind === "heading") nodes.push(el(nodes.length <= 1 ? "h1" : "h3", { dir: "auto" }, b.text));
    else if (b.kind === "image") nodes.push(el("figure", {}, el("img", { src: `/api/jobs/${current.id}/chapters/${ch.index}/images/${b.n}`, alt: "" })));
    else if (b.kind === "footnote") nodes.push(el("p", { class: "note", dir: "auto" }, b.text));
    else if (b.kind === "caption") nodes.push(el("p", { class: "muted", dir: "auto" }, b.text));
    else nodes.push(el("p", { dir: "auto" }, b.text));
  }
  if (!nodes.length) nodes.push(el("p", { class: "muted" }, "This section has no text."));
  body.replaceChildren(...nodes);
}

$("#preview-close").addEventListener("click", () => $("#preview").close());
$("#preview").addEventListener("click", e => { if (e.target === e.currentTarget) e.currentTarget.close(); });

/* ---------------------------------------------------------------- start */

loadInfo();
refreshJobs();
