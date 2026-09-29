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
  editing: null, // while the editor is open: {id (null = new), layout (the draft), openIndex}
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

// Field names in what's being shown: the draft's text elements while
// editing, otherwise the selected template's.
function activeFields() {
  if (state.editing) {
    const names = [];
    for (const element of state.editing.layout.elements || []) {
      if (element.type !== "text") continue;
      for (const match of (element.text || "").matchAll(FIELD_RE)) {
        const name = match[1].trim();
        if (name && !names.includes(name)) names.push(name);
      }
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

// ---------------------------------------------------------------------------
// Template editor. A template is edited as a *layout* (see labelpi/layout.py):
// a background plus a list of elements. state.editing.layout is the draft;
// every change re-renders the preview from it, and Save sends it as-is.
// ---------------------------------------------------------------------------

// Choices shared by several element types: [value, label].
const COLORS = [["black", "Black"], ["white", "White"]];
const FILLS = [["none", "None"], ["black", "Black"], ["white", "White"]];
const FITS = [["contain", "Fit inside"], ["cover", "Fill (crop)"], ["stretch", "Stretch"]];

// How to edit each setting: what kind of input, its label, and limits.
const BOX_FIELDS = [
  { key: "x", label: "Left %", kind: "number", min: 0, max: 100, step: 1 },
  { key: "y", label: "Top %", kind: "number", min: 0, max: 100, step: 1 },
  { key: "w", label: "Width %", kind: "number", min: 0.1, max: 100, step: 1 },
  { key: "h", label: "Height %", kind: "number", min: 0.1, max: 100, step: 1 },
];
const STROKE = { key: "stroke_mm", label: "Line (mm)", kind: "number", min: 0, max: 10, step: 0.1 };

// Every element type: its name on the page, the settings a new one starts
// with (the same defaults as layout.py), and the inputs to show.
const ELEMENT_KINDS = {
  text: {
    label: "Text",
    defaults: {
      x: 5, y: 10, w: 90, h: 80, text: "{field:Item}", align: "center", valign: "middle",
      font: "bold", size_mm: 0, color: "black",
    },
    fields: [
      { key: "text", label: "Text", kind: "textarea" },
      ...BOX_FIELDS,
      { key: "align", label: "Align", kind: "select",
        options: [["left", "Left"], ["center", "Centre"], ["right", "Right"]] },
      { key: "valign", label: "Vertical", kind: "select",
        options: [["top", "Top"], ["middle", "Middle"], ["bottom", "Bottom"]] },
      { key: "font", label: "Font", kind: "select",
        options: [["bold", "Bold"], ["regular", "Regular"], ["condensed", "Narrow bold"]] },
      { key: "size_mm", label: "Max size (mm)", kind: "number", min: 0, max: 50, step: 0.5 },
      { key: "color", label: "Colour", kind: "select", options: COLORS },
    ],
  },
  icon: {
    label: "Icon",
    defaults: { x: 0, y: 0, w: 20, h: 100, icon: "fa-solid:star", color: "black" },
    fields: [
      { key: "icon", label: "Icon", kind: "icon" },
      ...BOX_FIELDS,
      { key: "color", label: "Colour", kind: "select", options: COLORS },
    ],
  },
  rect: {
    label: "Box",
    defaults: { x: 0, y: 0, w: 100, h: 100, fill: "none", color: "black", stroke_mm: 0.3, radius_mm: 0 },
    fields: [
      ...BOX_FIELDS,
      { key: "fill", label: "Fill", kind: "select", options: FILLS },
      { key: "color", label: "Line colour", kind: "select", options: COLORS },
      STROKE,
      { key: "radius_mm", label: "Corners (mm)", kind: "number", min: 0, max: 50, step: 0.5 },
    ],
  },
  ellipse: {
    label: "Circle",
    defaults: { x: 40, y: 10, w: 20, h: 80, fill: "none", color: "black", stroke_mm: 0.3 },
    fields: [
      ...BOX_FIELDS,
      { key: "fill", label: "Fill", kind: "select", options: FILLS },
      { key: "color", label: "Line colour", kind: "select", options: COLORS },
      STROKE,
    ],
  },
  line: {
    label: "Line",
    defaults: { x1: 0, y1: 50, x2: 100, y2: 50, color: "black", stroke_mm: 0.3 },
    fields: [
      { key: "x1", label: "From left %", kind: "number", min: 0, max: 100, step: 1 },
      { key: "y1", label: "From top %", kind: "number", min: 0, max: 100, step: 1 },
      { key: "x2", label: "To left %", kind: "number", min: 0, max: 100, step: 1 },
      { key: "y2", label: "To top %", kind: "number", min: 0, max: 100, step: 1 },
      { key: "color", label: "Colour", kind: "select", options: COLORS },
      { ...STROKE, min: 0.05 },
    ],
  },
  image: {
    label: "Picture",
    defaults: { x: 0, y: 0, w: 30, h: 100, asset: null, fit: "contain", dither: false, invert: false },
    fields: [
      { key: "asset", label: "Picture", kind: "asset" },
      ...BOX_FIELDS,
      { key: "fit", label: "Fit", kind: "select", options: FITS },
      { key: "dither", label: "Dither (photos)", kind: "checkbox" },
      { key: "invert", label: "Invert", kind: "checkbox" },
    ],
  },
};

// A new element of `kind` with its defaults, plus any `changes`.
function newElement(kind, changes = {}) {
  return { type: kind, ...clone(ELEMENT_KINDS[kind].defaults), ...changes };
}

const NEW_TEMPLATE_LAYOUT = {
  tape_length_mm: null, // null = auto: as long as the text needs
  background: { fill: "white", frame: "none", frame_mm: 0.4, radius_mm: 1.5, image: null },
  elements: [
    newElement("text", { x: 0, y: 0, w: 100, h: 60 }),
    newElement("text", { x: 0, y: 65, w: 100, h: 35, text: "{date:%d %b %Y}" }),
  ],
};

function bindTemplateEditor() {
  $("tpl-edit").addEventListener("click", () => {
    const template = currentTemplate();
    if (template) openEditor(template.id, template.name, layoutOf(template));
  });
  $("tpl-new").addEventListener("click", () => openEditor(null, "", clone(NEW_TEMPLATE_LAYOUT)));
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

  // Background
  const background = () => state.editing.layout.background;
  $("bg-fill").addEventListener("change", () => {
    background().fill = $("bg-fill").value;
    layoutChanged();
  });
  $("bg-frame").addEventListener("change", () => {
    background().frame = $("bg-frame").value;
    layoutChanged();
  });
  $("bg-frame-mm").addEventListener("input", () => {
    background().frame_mm = numberOr($("bg-frame-mm").value, 0.4);
    layoutChanged();
  });
  $("tpl-length").addEventListener("input", () => {
    // Empty = auto length.
    const value = $("tpl-length").value.trim();
    state.editing.layout.tape_length_mm = value === "" ? null : numberOr(value, null);
    layoutChanged();
  });

  for (const button of $("tpl-add").querySelectorAll("[data-add]")) {
    button.addEventListener("click", () => addElement(button.dataset.add));
  }

  // JSON view: filled in when opened; "Apply" replaces the draft.
  $("tpl-json-box").addEventListener("toggle", () => {
    if ($("tpl-json-box").open) $("tpl-json").value = JSON.stringify(state.editing.layout, null, 2);
  });
  $("tpl-json-apply").addEventListener("click", () => {
    try {
      state.editing.layout = JSON.parse($("tpl-json").value);
    } catch (err) {
      return showEditorMessage(`That isn't valid JSON: ${err.message}`);
    }
    showEditorMessage("");
    fillEditor();
    layoutChanged();
  });
}

// Old-style templates are plain text; the editor turns them into a layout
// with one text element (they're saved as a layout from then on).
function layoutOf(template) {
  if (template.layout) return clone(template.layout);
  const layout = clone(NEW_TEMPLATE_LAYOUT);
  layout.elements = [newElement("text", { x: 0, y: 0, w: 100, h: 100, text: template.text })];
  return layout;
}

function openEditor(id, name, layout) {
  state.editing = { id, layout };
  $("tpl-name").value = name;
  $("tpl-delete").hidden = id === null;
  $("tpl-json-box").open = false;
  showEditorMessage("");
  fillEditor();
  $("tpl-editor").hidden = false;
  markSelectedTemplate();
  renderFieldInputs();
  schedulePreview(0);
  if (id === null) $("tpl-name").focus();
  loadAssets(); // for the picture choosers; fills them in when it arrives
}

function closeEditor() {
  state.editing = null;
  $("tpl-editor").hidden = true;
  markSelectedTemplate();
}

// Put the draft into the form (after opening, or after "Apply JSON").
function fillEditor() {
  const layout = state.editing.layout;
  layout.background = layout.background || {};
  const bg = layout.background;
  $("bg-fill").value = bg.fill || "white";
  $("bg-frame").value = bg.frame || "none";
  $("bg-frame-mm").value = bg.frame_mm ?? 0.4;
  $("tpl-length").value = layout.tape_length_mm ?? "";
  renderBackgroundImage();
  renderElements();
}

// Something in the draft changed: new {field:..} boxes, new preview.
function layoutChanged() {
  renderFieldInputs();
  schedulePreview();
}

function renderBackgroundImage() {
  const bg = state.editing.layout.background;
  const box = $("bg-image");
  const options = document.createElement("div");
  options.className = "inline-options";
  if (bg.image) {
    options.append(
      selectControl(FITS, bg.image.fit || "cover", (value) => {
        bg.image.fit = value;
        layoutChanged();
      }),
      checkboxControl("Dither", bg.image.dither, (value) => {
        bg.image.dither = value;
        layoutChanged();
      }),
      checkboxControl("Invert", bg.image.invert, (value) => {
        bg.image.invert = value;
        layoutChanged();
      }),
    );
  }
  const picker = assetPicker(bg.image ? bg.image.asset : null, true, (assetId) => {
    bg.image = assetId ? { asset: assetId, fit: "cover", dither: false, invert: false } : null;
    renderBackgroundImage();
    layoutChanged();
  });
  box.replaceChildren(picker, options);
}

// --- the element list -------------------------------------------------------------
function addElement(kind) {
  const element = newElement(kind);
  state.editing.layout.elements = state.editing.layout.elements || [];
  state.editing.layout.elements.push(element);
  state.editing.openIndex = state.editing.layout.elements.length - 1;
  renderElements();
  layoutChanged();
}

function renderElements() {
  const elements = state.editing.layout.elements || [];
  const list = $("tpl-elements");
  list.replaceChildren(...elements.map((element, index) => elementCard(element, index, elements)));
}

function elementCard(element, index, elements) {
  const kind = ELEMENT_KINDS[element.type];
  const item = document.createElement("li");
  const details = document.createElement("details");
  details.open = state.editing.openIndex === index;
  details.addEventListener("toggle", () => {
    if (details.open) state.editing.openIndex = index;
  });

  const summary = document.createElement("summary");
  const title = document.createElement("span");
  title.className = "element-title";
  const refreshTitle = () => {
    title.textContent = `${kind ? kind.label : element.type}${elementSummary(element)}`;
  };
  refreshTitle();

  const tools = document.createElement("span");
  tools.className = "element-tools";
  tools.append(
    toolButton("↑", "Move up (drawn earlier)", index === 0, () => moveElement(index, -1)),
    toolButton("↓", "Move down (drawn on top)", index === elements.length - 1, () =>
      moveElement(index, 1),
    ),
    toolButton("⧉", "Duplicate", false, () => {
      elements.splice(index + 1, 0, clone(element));
      state.editing.openIndex = index + 1;
      renderElements();
      layoutChanged();
    }),
    toolButton("✕", "Remove", false, () => {
      elements.splice(index, 1);
      state.editing.openIndex = null;
      renderElements();
      layoutChanged();
    }),
  );
  summary.append(title, tools);

  const body = document.createElement("div");
  body.className = "element-fields";
  if (kind) {
    for (const field of kind.fields) {
      body.append(
        elementField(element, field, () => {
          refreshTitle();
          layoutChanged();
        }),
      );
    }
  } else {
    body.textContent = `Unknown element type "${element.type}" - fix it in the JSON view.`;
  }
  details.append(summary, body);
  item.append(details);
  return item;
}

// A short description after the type name, so collapsed cards are recognisable.
function elementSummary(element) {
  if (element.type === "text") return `: ${(element.text || "").split("\n")[0]}`;
  if (element.type === "icon") return `: ${element.icon || ""}`;
  if (element.type === "line") return "";
  if (element.fill && element.fill !== "none") return ` (${element.fill})`;
  return "";
}

function moveElement(index, step) {
  const elements = state.editing.layout.elements;
  const target = index + step;
  if (target < 0 || target >= elements.length) return;
  [elements[index], elements[target]] = [elements[target], elements[index]];
  state.editing.openIndex = target;
  renderElements();
  layoutChanged();
}

function toolButton(text, title, disabled, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.disabled = disabled;
  button.addEventListener("click", (event) => {
    event.preventDefault(); // don't open/close the card
    onClick();
  });
  return button;
}

// One setting of an element as an input. `changed` runs after every edit.
function elementField(element, field, changed) {
  const set = (value) => {
    element[field.key] = value;
    changed();
  };
  const value = element[field.key];
  let control;
  if (field.kind === "number") {
    control = document.createElement("input");
    control.type = "number";
    control.min = field.min;
    control.max = field.max;
    control.step = field.step;
    control.value = value ?? 0;
    control.addEventListener("input", () => {
      if (control.value !== "") set(Number(control.value));
    });
  } else if (field.kind === "select") {
    control = selectControl(field.options, value, set);
  } else if (field.kind === "textarea") {
    control = document.createElement("textarea");
    control.rows = 2;
    control.spellcheck = false;
    control.value = value || "";
    control.addEventListener("input", () => set(control.value));
  } else if (field.kind === "checkbox") {
    return checkboxControl(field.label, value, set);
  } else if (field.kind === "icon") {
    control = iconPicker(value, set);
  } else if (field.kind === "asset") {
    control = assetPicker(value, false, set);
  }
  // Choosers hold several buttons, so they can't sit inside a <label> (a
  // click on the label would press the first button).
  const isChooser = field.kind === "icon" || field.kind === "asset";
  const wrapper = document.createElement(isChooser ? "div" : "label");
  wrapper.className = isChooser || field.kind === "textarea" ? "field wide" : "field";
  const caption = document.createElement("span");
  caption.textContent = field.label;
  wrapper.append(caption, control);
  return wrapper;
}

function selectControl(options, value, onChange) {
  const select = document.createElement("select");
  select.replaceChildren(...options.map(([v, label]) => new Option(label, v)));
  select.value = value;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function checkboxControl(label, checked, onChange) {
  const wrapper = document.createElement("label");
  wrapper.className = "check";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(checked);
  box.addEventListener("change", () => onChange(box.checked));
  wrapper.append(box, ` ${label}`);
  return wrapper;
}

// --- icons ---------------------------------------------------------------------------
// The icon fonts are the same TTF files the Pi prints with, served by labelpi
// itself (no CDN). Each style gets an @font-face the first time icons load.
const iconInfo = {}; // "fa-solid:snowflake" -> {style, name, codepoint}
let iconStyles = null; // from GET /api/icons: [{id, label, font}]

async function fetchIcons(query, style, limit = 60) {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  if (style) params.set("style", style);
  const body = await getJson(`/api/icons?${params}`);
  if (!iconStyles) {
    iconStyles = body.styles;
    const css = iconStyles
      .map((s) => `@font-face{font-family:"lp-${s.id}";src:url("${s.font}");font-display:block}`)
      .join("\n");
    const tag = document.createElement("style");
    tag.textContent = css;
    document.head.append(tag);
  }
  for (const icon of body.icons) iconInfo[icon.id] = icon;
  return body.icons;
}

// A <span> showing one icon, drawn with its font.
function iconGlyph(icon) {
  const span = document.createElement("span");
  span.className = "glyph";
  span.style.fontFamily = `"lp-${icon.style}"`;
  span.textContent = String.fromCodePoint(icon.codepoint);
  return span;
}

async function lookUpIcon(id) {
  if (iconInfo[id]) return iconInfo[id];
  const [style, name] = id.split(":");
  await fetchIcons(name || "", style, 5).catch(() => []);
  return iconInfo[id] || null;
}

// A button showing the chosen icon; clicking it opens a search panel below.
function iconPicker(value, onPick) {
  const wrapper = document.createElement("div");
  wrapper.className = "icon-picker";
  const current = document.createElement("button");
  current.type = "button";
  current.className = "icon-current";
  const panel = document.createElement("div");
  panel.className = "icon-panel";
  panel.hidden = true;

  const showCurrent = async (id) => {
    current.replaceChildren(document.createTextNode(`${id || "Choose an icon"} ▾`));
    const icon = id ? await lookUpIcon(id) : null;
    if (icon) current.prepend(iconGlyph(icon), " ");
  };
  showCurrent(value);

  const search = document.createElement("input");
  search.type = "text";
  search.placeholder = "Search icons, e.g. snow, fridge, warning";
  const style = document.createElement("select");
  const grid = document.createElement("div");
  grid.className = "icon-grid";

  let timer = null;
  const runSearch = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const found = await fetchIcons(search.value, style.value).catch(() => []);
      if (style.options.length === 0 && iconStyles) {
        style.replaceChildren(
          new Option("All libraries", ""),
          ...iconStyles.map((s) => new Option(s.label, s.id)),
        );
      }
      grid.replaceChildren(
        ...found.map((icon) => {
          const button = document.createElement("button");
          button.type = "button";
          button.title = icon.id;
          button.append(iconGlyph(icon));
          button.addEventListener("click", () => {
            onPick(icon.id);
            showCurrent(icon.id);
            panel.hidden = true;
          });
          return button;
        }),
      );
      if (!found.length) grid.textContent = "No icons found.";
    }, 250);
  };
  search.addEventListener("input", runSearch);
  style.addEventListener("change", runSearch);
  current.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      if (!search.value && value) search.value = value.split(":")[1] || "";
      runSearch();
      search.focus();
    }
  });

  const controls = document.createElement("div");
  controls.className = "icon-search";
  controls.append(search, style);
  panel.append(controls, grid);
  wrapper.append(current, panel);
  return wrapper;
}

// --- pictures (uploaded images) ------------------------------------------------------
state.assets = []; // from GET /api/assets
const assetPickers = new Set(); // redraw every open chooser when the list changes

async function loadAssets() {
  try {
    state.assets = await getJson("/api/assets");
  } catch (err) {
    state.assets = [];
  }
  for (const redraw of assetPickers) redraw();
}

// A row of thumbnails to choose from, plus "Upload". `allowNone` adds a
// "None" choice (for the background).
function assetPicker(value, allowNone, onPick) {
  const wrapper = document.createElement("div");
  wrapper.className = "asset-picker";
  let selected = value;

  const pick = (assetId) => {
    selected = assetId;
    onPick(assetId);
    draw();
  };
  const draw = () => {
    const choices = [];
    if (allowNone) choices.push(assetChoice(null, selected === null, pick));
    for (const asset of state.assets) choices.push(assetChoice(asset, asset.id === selected, pick));
    if (!allowNone && selected && !state.assets.some((a) => a.id === selected)) {
      const missing = document.createElement("span");
      missing.className = "hint error";
      missing.textContent = "Picture missing";
      choices.push(missing);
    }
    choices.push(uploadButton(pick));
    wrapper.replaceChildren(...choices);
  };
  const redrawIfShown = () => {
    if (wrapper.isConnected) draw();
    else assetPickers.delete(redrawIfShown); // this chooser was removed from the page
  };
  assetPickers.add(redrawIfShown);
  draw();
  return wrapper;
}

function assetChoice(asset, isSelected, pick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "asset";
  button.setAttribute("aria-pressed", String(isSelected));
  if (asset) {
    const img = document.createElement("img");
    img.src = asset.url;
    img.alt = `${asset.width} x ${asset.height}`;
    button.title = `${asset.width} x ${asset.height} px`;
    button.append(img);
  } else {
    button.textContent = "None";
  }
  button.addEventListener("click", () => pick(asset ? asset.id : null));
  return button;
}

function uploadButton(pick) {
  const label = document.createElement("label");
  label.className = "asset upload";
  label.textContent = "+ Upload";
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/png,image/jpeg,image/gif,image/bmp";
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/api/assets", { method: "POST", body: form }).catch(() => null);
    const body = response ? await response.json().catch(() => ({})) : {};
    if (!response || !response.ok) {
      showEditorMessage(body.detail || "Couldn't upload the picture.");
      return;
    }
    showEditorMessage("");
    await loadAssets();
    pick(body.id);
  });
  label.append(input);
  return label;
}

// --- save / delete ----------------------------------------------------------------------
async function saveTemplate() {
  const isNew = state.editing.id === null;
  const url = isNew ? "/api/templates" : `/api/templates/${encodeURIComponent(state.editing.id)}`;
  const response = await fetch(url, {
    method: isNew ? "POST" : "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: $("tpl-name").value, layout: state.editing.layout }),
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

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function numberOr(text, fallback) {
  return text === "" || Number.isNaN(Number(text)) ? fallback : Number(text);
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
      // The unsaved draft: preview (and even print) it as it is now.
      return jsonRequest(url, { printer, label, layout: state.editing.layout, fields });
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
