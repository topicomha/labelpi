// labelpi web UI. Plain JavaScript, no framework, no build step.
// Everything goes through the public /api - the same endpoints scripts use.
"use strict";

const PREVIEW_DELAY_MS = 400; // wait for typing to pause before re-rendering
const BUSY_POLL_MS = 5000; // refresh the printers' busy flags
const STORAGE_PREFIX = "labelpi."; // remembers your last choices, per browser

const $ = (id) => document.getElementById(id);

const state = {
  printers: [], // from GET /api/printers
  shortcuts: [], // from GET /api/shortcuts
  mode: "text", // "text" | "image" | "shortcut" (matches the API path)
  align: "center",
  file: null, // the chosen image File
  shortcut: null, // id of the selected shortcut
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
    const [printers, shortcuts] = await Promise.all([
      getJson("/api/printers"),
      getJson("/api/shortcuts"),
    ]);
    state.printers = printers;
    state.shortcuts = shortcuts;
  } catch (err) {
    setStatus("Can't reach labelpi - is the Pi on?", "error");
    return;
  }
  fillPrinters();
  restoreChoices();
  fillLabels();
  fillShortcuts();
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
// Filling the controls
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

function fillLabels() {
  const printer = currentPrinter();
  const select = $("label");
  const previous = select.value || load("label");
  const labels = printer ? printer.labels : [];
  select.replaceChildren(...labels.map((l) => new Option(l.name, l.id)));
  if (labels.some((l) => l.id === previous)) select.value = previous;
}

function fillShortcuts() {
  const box = $("shortcuts");
  box.replaceChildren(
    ...state.shortcuts.map((shortcut) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = shortcut.name;
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => selectShortcut(shortcut.id));
      return button;
    }),
  );
  $("no-shortcuts").hidden = state.shortcuts.length > 0;
  if (state.shortcuts.length && !state.shortcut) state.shortcut = state.shortcuts[0].id;
  markSelectedShortcut();
}

function selectShortcut(id) {
  state.shortcut = id;
  markSelectedShortcut();
  saveChoices();
  schedulePreview(0);
}

function markSelectedShortcut() {
  const buttons = $("shortcuts").children;
  state.shortcuts.forEach((shortcut, i) => {
    buttons[i].setAttribute("aria-pressed", String(shortcut.id === state.shortcut));
  });
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
  if (state.mode === "shortcut") {
    if (!state.shortcut) return null;
    return jsonRequest(url, { printer, label, shortcut: state.shortcut });
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
  if (state.mode === "shortcut") return "Pick a shortcut.";
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
  if (state.shortcut) save("shortcut", state.shortcut);
}

function restoreChoices() {
  const printer = load("printer");
  if (state.printers.some((p) => p.id === printer)) $("printer").value = printer;
  const mode = load("mode");
  if (["text", "image", "shortcut"].includes(mode)) state.mode = mode;
  const text = load("text");
  if (text !== null) $("text").value = text;
  const shortcut = load("shortcut");
  if (state.shortcuts.some((s) => s.id === shortcut)) state.shortcut = shortcut;
}
