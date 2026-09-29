// labelpi web UI. Plain JavaScript, no framework, no build step.
// Everything goes through the public /api - the same endpoints scripts use.
"use strict";

const PREVIEW_DELAY_MS = 400; // wait for typing to pause before re-rendering
const BUSY_POLL_MS = 5000; // refresh the printers' busy flags
const STORAGE_PREFIX = "labelpi."; // remembers your last choices, per browser
const FIELD_RE = /\{field:([^{}]*)\}/g; // same syntax as labelpi/templates.py

const $ = (id) => document.getElementById(id);

const state = {
  printers: [], // from GET /api/printers
  templates: [], // from GET /api/templates: {id, name, text, fields}
  mode: "text", // "text" | "image" | "template" (matches the API path)
  align: "center",
  file: null, // the chosen image File
  template: null, // id of the selected template
  fieldValues: {}, // what's typed into each {field:Name} box, by name
  editing: null, // null, or {id: <id or null for a new one>} while the editor is open
  previewOk: false, // only allow printing what previewed without errors
  printing: false,
};

let previewTimer = null;
let previewAbort = null; // AbortController for the preview in flight
let previewUrl = null; // object URL of the current preview image

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  try {
    const [printers, templates] = await Promise.all([
      getJson("/api/printers"),
      getJson("/api/templates"),
    ]);
    state.printers = printers;
    state.templates = templates;
  } catch (err) {
    setStatus("Can't reach labelpi - is the Pi on?", "error");
    return;
  }
  fillPrinters();
  restoreChoices();
  fillLabels();
  fillTemplates();
  setMode(state.mode);
  schedulePreview(0);
  setInterval(refreshBusy, BUSY_POLL_MS);
}

function bindEvents() {
  $("printer").addEventListener("change", () => {
    fillLabels();
    saveChoices();
    schedulePreview();
  });
  $("label").addEventListener("change", () => {
    saveChoices();
    schedulePreview();
  });
  $("auto-feed").addEventListener("change", () => setAutoFeed($("auto-feed").checked));
  $("feed").addEventListener("click", feed);

  for (const tab of document.querySelectorAll("[data-mode]")) {
    tab.addEventListener("click", () => {
      setMode(tab.dataset.mode);
      saveChoices();
      schedulePreview(0);
    });
  }

  $("text").addEventListener("input", () => {
    saveChoices();
    schedulePreview();
  });
  for (const button of document.querySelectorAll("[data-align]")) {
    button.addEventListener("click", () => {
      state.align = button.dataset.align;
      for (const b of document.querySelectorAll("[data-align]")) {
        b.setAttribute("aria-checked", String(b === button));
      }
      schedulePreview(0);
    });
  }

  $("file").addEventListener("change", () => setFile($("file").files[0]));
  $("dither").addEventListener("change", () => schedulePreview(0));
  $("invert").addEventListener("change", () => schedulePreview(0));
  bindDropzone();

  bindTemplateEditor();
  $("print").addEventListener("click", print);
}

function bindDropzone() {
  const zone = $("dropzone");
  zone.addEventListener("dragover", (event) => {
    event.preventDefault(); // allow dropping
    zone.classList.add("dragging");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
    const file = event.dataTransfer.files[0];
    if (file) setFile(file);
  });
}

// ---------------------------------------------------------------------------
// Printers and labels
// ---------------------------------------------------------------------------
function fillPrinters() {
  const select = $("printer");
  select.replaceChildren(
    ...state.printers.map((p) => new Option(printerText(p), p.id)),
  );
}

function printerText(printer) {
  return printer.busy ? `${printer.name} (printing...)` : printer.name;
}

function currentPrinter() {
  return state.printers.find((p) => p.id === $("printer").value);
}

// Tape printers can print labels back to back (no feed-out, cut lines between
// them) and then feed the whole strip out with "Feed & cut".
function updateFeedControls() {
  const printer = currentPrinter();
  const canChain = Boolean(printer && printer.can_chain);
  $("feed-options").hidden = !canChain;
  $("feed").hidden = !canChain || printer.auto_feed;
  if (!canChain) return;
  $("auto-feed").checked = printer.auto_feed;
  $("feed-hint").textContent = printer.auto_feed
    ? "Each label comes out ready to cut (with ~24 mm of blank tape before it)."
    : "Labels print back to back with cut lines. Press Feed & cut when you're done.";
  $("feed").disabled = state.printing;
}

async function setAutoFeed(on) {
  const printer = currentPrinter();
  const response = await fetch(`/api/printers/${encodeURIComponent(printer.id)}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_feed: on }),
  }).catch(() => null);
  if (!response || !response.ok) {
    setStatus("Couldn't save the feed setting.", "error");
  } else {
    printer.auto_feed = (await response.json()).auto_feed;
  }
  updateFeedControls();
  schedulePreview(0); // cut lines appear or disappear
}

async function feed() {
  const printer = currentPrinter();
  if (!printer) return;
  state.printing = true;
  updatePrintButton();
  setStatus(`Feeding ${printer.name}...`);
  try {
    const response = await fetch(`/api/printers/${encodeURIComponent(printer.id)}/feed`, {
      method: "POST",
    });
    const body = await response.json().catch(() => ({}));
    const [message, kind] = response.ok
      ? ["Fed out - cut the strip now", "ok"]
      : describeResult(response.status, body, printer.name);
    setStatus(message, kind);
  } catch (err) {
    setStatus("Can't reach labelpi - is the Pi on?", "error");
  } finally {
    state.printing = false;
    updatePrintButton();
    refreshBusy();
  }
}

function fillLabels() {
  const printer = currentPrinter();
  const select = $("label");
  const previous = select.value || load("label");
  const labels = printer ? printer.labels : [];
  select.replaceChildren(...labels.map((l) => new Option(l.name, l.id)));
  if (labels.some((l) => l.id === previous)) select.value = previous;
  updateFeedControls();
}

function setMode(mode) {
  state.mode = mode;
  for (const tab of document.querySelectorAll("[data-mode]")) {
    tab.setAttribute("aria-selected", String(tab.dataset.mode === mode));
  }
  for (const panel of document.querySelectorAll("[data-panel]")) {
    panel.hidden = panel.dataset.panel !== mode;
  }
}

function setFile(file) {
  state.file = file || null;
  $("file-name").textContent = file ? file.name : "Choose an image, or drop one here";
  schedulePreview(0);
}

// ---------------------------------------------------------------------------
// Templates: pick one, fill in its boxes; or edit / create one
// ---------------------------------------------------------------------------
function fillTemplates() {
  const box = $("templates");
  box.replaceChildren(
    ...state.templates.map((template) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = template.name;
      button.dataset.id = template.id;
      button.addEventListener("click", () => selectTemplate(template.id));
      return button;
    }),
  );
  $("no-templates").hidden = state.templates.length > 0;
  if (!state.templates.some((t) => t.id === state.template)) {
    state.template = state.templates.length ? state.templates[0].id : null;
  }
  markSelectedTemplate();
  renderFieldInputs();
}

function currentTemplate() {
  return state.templates.find((t) => t.id === state.template);
}

function selectTemplate(id) {
  state.template = id;
  closeEditor();
  markSelectedTemplate();
  renderFieldInputs();
  saveChoices();
  schedulePreview(0);
}

function markSelectedTemplate() {
  // While writing a new template nothing is highlighted; while editing one, that one.
  const highlighted = state.editing ? state.editing.id : state.template;
  for (const button of $("templates").children) {
    button.setAttribute("aria-pressed", String(button.dataset.id === highlighted));
  }
  $("tpl-edit").disabled = !currentTemplate();
  $("template-actions").hidden = Boolean(state.editing);
}

// Field names in the text being shown: the editor's text while editing,
// otherwise the selected template's.
function activeFields() {
  if (state.editing) {
    const names = [];
    for (const match of $("tpl-text").value.matchAll(FIELD_RE)) {
      const name = match[1].trim();
      if (name && !names.includes(name)) names.push(name);
    }
    return names;
  }
  const template = currentTemplate();
  return template ? template.fields : [];
}

function renderFieldInputs() {
  const box = $("template-fields");
  const names = activeFields();
  // Keep the existing boxes (and the cursor) if the fields haven't changed.
  const current = [...box.querySelectorAll("input")].map((i) => i.dataset.field);
  if (current.join("\n") === names.join("\n")) return;

  box.replaceChildren(
    ...names.map((name) => {
      const wrapper = document.createElement("label");
      wrapper.className = "field";
      const caption = document.createElement("span");
      caption.textContent = name;
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      input.dataset.field = name;
      input.value = state.fieldValues[name] || "";
      input.addEventListener("input", () => {
        state.fieldValues[name] = input.value;
        schedulePreview();
      });
      wrapper.append(caption, input);
      return wrapper;
    }),
  );
}

function bindTemplateEditor() {
  $("tpl-edit").addEventListener("click", () => {
    const template = currentTemplate();
    if (template) openEditor(template.id, template.name, template.text);
  });
  $("tpl-new").addEventListener("click", () =>
    openEditor(null, "", "{field:Item}\n{date:%d %b %Y}"),
  );
  $("tpl-text").addEventListener("input", () => {
    renderFieldInputs();
    schedulePreview();
  });
  $("tpl-cancel").addEventListener("click", () => {
    closeEditor();
    renderFieldInputs();
    schedulePreview(0);
  });
  $("tpl-editor").addEventListener("submit", (event) => {
    event.preventDefault(); // we save with fetch, not a page reload
    saveTemplate();
  });
  $("tpl-delete").addEventListener("click", deleteTemplate);
}

function openEditor(id, name, text) {
  state.editing = { id };
  $("tpl-name").value = name;
  $("tpl-text").value = text;
  $("tpl-delete").hidden = id === null;
  showEditorMessage("");
  $("tpl-editor").hidden = false;
  markSelectedTemplate();
  renderFieldInputs();
  schedulePreview(0);
  $(id === null ? "tpl-name" : "tpl-text").focus();
}

function closeEditor() {
  state.editing = null;
  $("tpl-editor").hidden = true;
  markSelectedTemplate();
}

async function saveTemplate() {
  const isNew = state.editing.id === null;
  const url = isNew ? "/api/templates" : `/api/templates/${encodeURIComponent(state.editing.id)}`;
  const response = await fetch(url, {
    method: isNew ? "POST" : "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: $("tpl-name").value, text: $("tpl-text").value }),
  }).catch(() => null);
  if (!response) return showEditorMessage("Can't reach labelpi - is the Pi on?");
  const body = await response.json().catch(() => ({}));
  if (!response.ok) return showEditorMessage(body.detail || `Couldn't save (${response.status})`);
  await reloadTemplates(body.id);
  setStatus(`Saved template "${body.name}"`, "ok");
}

async function deleteTemplate() {
  const template = state.templates.find((t) => t.id === state.editing.id);
  if (!template || !confirm(`Delete the template "${template.name}"?`)) return;
  const response = await fetch(`/api/templates/${encodeURIComponent(template.id)}`, {
    method: "DELETE",
  }).catch(() => null);
  if (!response || !response.ok) return showEditorMessage("Couldn't delete the template.");
  await reloadTemplates(null);
  setStatus(`Deleted template "${template.name}"`, "ok");
}

async function reloadTemplates(selectId) {
  state.templates = await getJson("/api/templates");
  closeEditor();
  if (selectId) state.template = selectId;
  fillTemplates();
  saveChoices();
  schedulePreview(0);
}

function showEditorMessage(message) {
  const box = $("tpl-message");
  box.textContent = message;
  box.hidden = !message;
}

// ---------------------------------------------------------------------------
// Building the request - the same for preview and print, so what you see is
// what prints. Returns null if there's nothing to print yet.
// ---------------------------------------------------------------------------
function buildRequest(preview) {
  const printer = $("printer").value;
  const label = $("label").value;
  if (!printer || !label) return null;
  const url = `/api/print/${state.mode}${preview ? "?preview=1" : ""}`;

  if (state.mode === "text") {
    const text = $("text").value;
    if (!text.trim()) return null;
    return jsonRequest(url, { printer, label, text, align: state.align });
  }
  if (state.mode === "template") {
    const fields = {};
    for (const name of activeFields()) fields[name] = state.fieldValues[name] || "";
    if (state.editing) {
      // Unsaved template text: preview (and even print) it as it is now.
      const text = $("tpl-text").value;
      if (!text.trim()) return null;
      return jsonRequest(url, { printer, label, text, fields });
    }
    if (!state.template) return null;
    return jsonRequest(url, { printer, label, template: state.template, fields });
  }
  // image: multipart form, like `curl -F`
  if (!state.file) return null;
  const form = new FormData();
  form.append("printer", printer);
  form.append("label", label);
  form.append("file", state.file);
  form.append("dither", $("dither").checked ? "true" : "false");
  form.append("invert", $("invert").checked ? "true" : "false");
  return { url, options: { method: "POST", body: form } };
}

function jsonRequest(url, body) {
  return {
    url,
    options: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  };
}

// ---------------------------------------------------------------------------
// Preview
// ---------------------------------------------------------------------------
function schedulePreview(delay = PREVIEW_DELAY_MS) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, delay);
}

async function updatePreview() {
  const request = buildRequest(true);
  if (!request) {
    showPreviewHint(emptyHint());
    return;
  }
  if (previewAbort) previewAbort.abort(); // a newer preview replaces an older one
  previewAbort = new AbortController();
  $("preview-box").classList.add("loading");

  try {
    const response = await fetch(request.url, { ...request.options, signal: previewAbort.signal });
    if (response.ok) {
      showPreviewImage(await response.blob());
    } else {
      const body = await response.json().catch(() => ({}));
      showPreviewHint(body.detail || `Preview failed (${response.status})`, true);
    }
  } catch (err) {
    if (err.name === "AbortError") return; // superseded - not an error
    showPreviewHint("Can't reach labelpi - is the Pi on?", true);
  } finally {
    $("preview-box").classList.remove("loading");
  }
}

function emptyHint() {
  if (state.mode === "image") return "Choose an image to see a preview.";
  if (state.mode === "template") return "Pick a template.";
  return "Type something to see a preview.";
}

function showPreviewImage(blob) {
  if (previewUrl) URL.revokeObjectURL(previewUrl); // free the old image's memory
  previewUrl = URL.createObjectURL(blob);
  const img = $("preview");
  img.src = previewUrl;
  img.hidden = false;
  $("preview-hint").hidden = true;
  state.previewOk = true;
  updatePrintButton();
}

function showPreviewHint(message, isError = false) {
  $("preview").hidden = true;
  const hint = $("preview-hint");
  hint.textContent = message;
  hint.classList.toggle("error", isError);
  hint.hidden = false;
  state.previewOk = false;
  updatePrintButton();
}

function updatePrintButton() {
  $("print").disabled = !state.previewOk || state.printing;
  $("feed").disabled = state.printing;
}

// ---------------------------------------------------------------------------
// Print
// ---------------------------------------------------------------------------
async function print() {
  const request = buildRequest(false);
  const printer = currentPrinter();
  if (!request || !printer) return;

  state.printing = true;
  updatePrintButton();
  setStatus(`Printing on ${printer.name}...`);
  try {
    const response = await fetch(request.url, request.options);
    const body = await response.json().catch(() => ({}));
    const [message, kind] = describeResult(response.status, body, printer.name);
    setStatus(message, kind);
  } catch (err) {
    setStatus("Can't reach labelpi - is the Pi on?", "error");
  } finally {
    state.printing = false;
    updatePrintButton();
    refreshBusy();
  }
}

// Plain-English status for each API answer (see docs/SPEC.md section 5).
function describeResult(status, body, name) {
  const detail = body.detail || "";
  switch (status) {
    case 200:
      if (body.fed === false) {
        return [`Printed (${(body.ms / 1000).toFixed(1)} s) - Feed & cut when you're done`, "ok"];
      }
      return [`Printed (${(body.ms / 1000).toFixed(1)} s)`, "ok"];
    case 409:
      return [`${name} is busy - try again in a moment`, "warn"];
    case 503:
      return [`${name} not reachable - is it on?`, "error"];
    case 500:
      return [`${name} had a problem: ${detail}`, "error"];
    default:
      return [detail || `Something went wrong (${status})`, "error"];
  }
}

function setStatus(message, kind = "") {
  const status = $("status");
  status.textContent = message;
  status.className = `status ${kind}`;
}

async function refreshBusy() {
  try {
    const printers = await getJson("/api/printers");
    for (const fresh of printers) {
      const known = state.printers.find((p) => p.id === fresh.id);
      if (known) known.busy = fresh.busy;
    }
    for (const option of $("printer").options) {
      const printer = state.printers.find((p) => p.id === option.value);
      if (printer) option.textContent = printerText(printer);
    }
  } catch (err) {
    // Ignore: the next poll will try again.
  }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json();
}

// localStorage can be missing or throw (private windows); it's only a convenience.
function load(key) {
  try {
    return localStorage.getItem(STORAGE_PREFIX + key);
  } catch {
    return null;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(STORAGE_PREFIX + key, value);
  } catch {
    // not important
  }
}

function saveChoices() {
  save("printer", $("printer").value);
  save("label", $("label").value);
  save("mode", state.mode);
  save("text", $("text").value);
  if (state.template) save("template", state.template);
}

function restoreChoices() {
  const printer = load("printer");
  if (state.printers.some((p) => p.id === printer)) $("printer").value = printer;
  let mode = load("mode");
  if (mode === "shortcut") mode = "template"; // saved by an older version
  if (["text", "image", "template"].includes(mode)) state.mode = mode;
  const text = load("text");
  if (text !== null) $("text").value = text;
  const template = load("template");
  if (state.templates.some((t) => t.id === template)) state.template = template;
}
