"use strict";

// Interfaz sin paso de build: estado en memoria, el servidor valida y construye todo.
// Nunca se usa innerHTML con datos: todo texto entra por textContent.

const ROLES = {
  spc: ["situation", "population", "question"],
  pico: ["population", "intervention", "comparison", "outcome"],
  concepts: ["concept"],
};
const ROLE_LABELS = {
  situation: "Situation",
  population: "Population",
  question: "Question",
  intervention: "Intervention",
  comparison: "Comparison",
  outcome: "Outcome",
  concept: "Concept",
};
const DATABASES = [
  ["pubmed", "PubMed"],
  ["eric", "ERIC"],
  ["arxiv", "arXiv"],
  ["openalex", "OpenAlex"],
];
const SCOPES = [
  ["heading", "Heading [mh]"],
  ["major", "Major topic [mj]"],
  ["heading_no_explode", "Heading, no explode"],
  ["major_no_explode", "Major, no explode"],
];
const FIELDS = [
  ["title_abstract", "Title/abstract [tiab]"],
  ["title", "Title [ti]"],
];
const CONTROLLED_TAGS = new Set(["mh", "mj", "mh:noexp", "mj:noexp"]);

let state = emptyQuestion("spc");
let lastCheck = null;
let previewTimer = null;
let previewSeq = 0;

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function emptyCondition() {
  return { label: "", terms: [] };
}

function emptyComponent(role) {
  return { role, description: "", conditions: [emptyCondition()] };
}

function emptyQuestion(format) {
  return {
    question: "",
    format,
    discipline: "",
    databases: [],
    components: ROLES[format].map(emptyComponent),
    exclusions: [],
    options: { repeat_controlled_as_free: false, free_text_unindexed_only: false },
  };
}

// --- Conversión entre el estado del formulario y el formato del fichero de pregunta ---

function termToData(term) {
  if (term.kind === "free") {
    return term.field === "title_abstract" ? term.text : { term: term.text, field: term.field };
  }
  const data = { term: term.text };
  if (term.scope !== "heading") data.scope = term.scope;
  if (term.kind !== "mesh") data.vocabulary = term.kind;
  return Object.keys(data).length === 1 ? term.text : data;
}

function conditionToData(condition) {
  const terms = condition.terms.filter((t) => t.text.trim());
  const data = { label: condition.label };
  const controlled = terms.filter((t) => t.kind !== "free").map(termToData);
  const free = terms.filter((t) => t.kind === "free").map(termToData);
  if (controlled.length) data.controlled = controlled;
  if (free.length) data.free = free;
  return data;
}

function isBlank(condition) {
  return !condition.label.trim() && condition.terms.every((t) => !t.text.trim());
}

function toData(q) {
  const data = { question: q.question, format: q.format };
  if (q.discipline) data.discipline = q.discipline;
  if (q.databases.length) data.databases = q.databases;
  data.components = q.components.map((c) => {
    const item = { description: c.description };
    if (q.format !== "concepts") item.role = c.role;
    const conditions = c.conditions.filter((cond) => !isBlank(cond)).map(conditionToData);
    if (conditions.length) item.conditions = conditions;
    return item;
  });
  const exclusions = q.exclusions.filter((cond) => !isBlank(cond)).map(conditionToData);
  if (exclusions.length) data.exclusions = exclusions;
  const options = {};
  if (q.options.repeat_controlled_as_free) options.repeat_controlled_as_free = true;
  if (q.options.free_text_unindexed_only) options.free_text_unindexed_only = true;
  if (Object.keys(options).length) data.options = options;
  return data;
}

function termFromData(raw, controlled) {
  const table = typeof raw === "string" ? { term: raw } : raw;
  if (controlled) {
    return {
      kind: table.vocabulary || "mesh",
      text: table.term,
      scope: table.scope || "heading",
      field: "title_abstract",
    };
  }
  return { kind: "free", text: table.term, scope: "heading", field: table.field || "title_abstract" };
}

function conditionFromData(raw) {
  return {
    label: raw.label || "",
    terms: [
      ...(raw.controlled || []).map((t) => termFromData(t, true)),
      ...(raw.free || []).map((t) => termFromData(t, false)),
    ],
  };
}

function fromData(data) {
  const format = data.format || "spc";
  const components = (data.components || []).map((c) => ({
    role: c.role || "concept",
    description: c.description || "",
    conditions: (c.conditions || []).map(conditionFromData),
  }));
  const q = {
    question: data.question || "",
    format,
    discipline: data.discipline || "",
    databases: data.databases || [],
    components,
    exclusions: (data.exclusions || []).map(conditionFromData),
    options: {
      repeat_controlled_as_free: Boolean(data.options && data.options.repeat_controlled_as_free),
      free_text_unindexed_only: Boolean(data.options && data.options.free_text_unindexed_only),
    },
  };
  if (format !== "concepts") {
    // SPC y PICO enseñan siempre todos sus componentes, aunque el fichero omita alguno.
    q.components = ROLES[format].map(
      (role) => components.find((c) => c.role === role) || emptyComponent(role),
    );
  }
  return q;
}

// --- Formulario ---

function fillOptions(select, options, value) {
  select.replaceChildren(...options.map(([v, label]) => {
    const option = el("option", null, label);
    option.value = v;
    return option;
  }));
  select.value = value;
}

function renderTerm(term, list, index, rerender) {
  const node = $("tpl-term").content.firstElementChild.cloneNode(true);
  const kind = node.querySelector(".term-kind");
  const text = node.querySelector(".term-text");
  const option = node.querySelector(".term-option");
  kind.value = term.kind;
  text.value = term.text;
  text.placeholder = term.kind === "free" ? "e.g. caregiv*" : "e.g. Caregivers";
  if (term.kind === "free") fillOptions(option, FIELDS, term.field);
  else fillOptions(option, SCOPES, term.scope);
  kind.addEventListener("change", () => {
    term.kind = kind.value;
    rerender();
    schedulePreview();
  });
  text.addEventListener("input", () => {
    term.text = text.value;
    schedulePreview();
  });
  option.addEventListener("change", () => {
    if (term.kind === "free") term.field = option.value;
    else term.scope = option.value;
    schedulePreview();
  });
  node.querySelector(".remove-term").addEventListener("click", () => {
    list.splice(index, 1);
    rerender();
    schedulePreview();
  });
  return node;
}

function renderCondition(condition, list, index) {
  const node = $("tpl-condition").content.firstElementChild.cloneNode(true);
  const label = node.querySelector(".condition-label");
  const terms = node.querySelector(".terms");
  label.value = condition.label;
  label.addEventListener("input", () => {
    condition.label = label.value;
    schedulePreview();
  });
  const drawTerms = () => {
    terms.replaceChildren(
      ...condition.terms.map((t, i) => renderTerm(t, condition.terms, i, drawTerms)),
    );
  };
  drawTerms();
  const addTerm = (kind) => {
    const defaultKind = kind === "heading" ? (state.discipline === "education" ? "eric" : "mesh") : "free";
    condition.terms.push({ kind: defaultKind, text: "", scope: "heading", field: "title_abstract" });
    drawTerms();
    terms.lastElementChild.querySelector(".term-text").focus();
  };
  node.querySelector(".add-heading").addEventListener("click", () => addTerm("heading"));
  node.querySelector(".add-free").addEventListener("click", () => addTerm("free"));
  node.querySelector(".remove-condition").addEventListener("click", () => {
    list.splice(index, 1);
    renderForm();
    schedulePreview();
  });
  return node;
}

function renderComponent(component, index) {
  const node = $("tpl-component").content.firstElementChild.cloneNode(true);
  const concepts = state.format === "concepts";
  const sameRole = state.components.slice(0, index + 1).filter((c) => c.role === component.role);
  node.querySelector(".role-pill").textContent = concepts
    ? `Concept ${sameRole.length}`
    : ROLE_LABELS[component.role];
  const remove = node.querySelector(".remove-component");
  if (concepts && state.components.length > 1) {
    remove.addEventListener("click", () => {
      state.components.splice(index, 1);
      renderForm();
      schedulePreview();
    });
  } else {
    remove.remove();
  }
  const description = node.querySelector(".component-description");
  description.value = component.description;
  description.placeholder = component.role === "comparison"
    ? "Often described but left out of the search"
    : "What this part of the question is about";
  description.addEventListener("input", () => {
    component.description = description.value;
    schedulePreview();
  });
  node.querySelector(".conditions").replaceChildren(
    ...component.conditions.map((c, i) => renderCondition(c, component.conditions, i)),
  );
  node.querySelector(".add-condition").addEventListener("click", () => {
    component.conditions.push(emptyCondition());
    renderForm();
  });
  return node;
}

function renderForm() {
  $("question-text").value = state.question;
  $("question-format").value = state.format;
  $("question-discipline").value = state.discipline;
  for (const box of $("database-checks").querySelectorAll("input")) {
    box.checked = state.databases.includes(box.value);
  }
  $("opt-repeat").checked = state.options.repeat_controlled_as_free;
  $("opt-unindexed").checked = state.options.free_text_unindexed_only;
  const components = state.components.map(renderComponent);
  if (state.format === "concepts") {
    const add = el("button", "button add-concept", "+ Concept");
    add.type = "button";
    add.addEventListener("click", () => {
      state.components.push(emptyComponent("concept"));
      renderForm();
    });
    components.push(add);
  }
  $("components").replaceChildren(...components);
  $("exclusions").replaceChildren(
    ...state.exclusions.map((c, i) => renderCondition(c, state.exclusions, i)),
  );
}

function changeFormat(format) {
  if (format === "concepts") {
    // Al pasar a conceptos libres no se pierde nada: cada componente pasa a ser un concepto.
    state.components = state.components
      .filter((c) => c.description.trim() || c.conditions.some((cond) => !isBlank(cond)))
      .map((c) => ({ ...c, role: "concept" }));
    if (!state.components.length) state.components = [emptyComponent("concept")];
  } else {
    const previous = state.components;
    state.components = ROLES[format].map((role, i) => {
      const same = previous.find((c) => c.role === role);
      if (same) return same;
      if (state.format === "concepts" && previous[i]) return { ...previous[i], role };
      return emptyComponent(role);
    });
  }
  state.format = format;
  renderForm();
  schedulePreview();
}

function setupStaticForm() {
  const checks = $("database-checks");
  for (const [value, label] of DATABASES) {
    const wrap = el("label", "check inline-check");
    const box = el("input");
    box.type = "checkbox";
    box.value = value;
    box.addEventListener("change", () => {
      state.databases = DATABASES.map(([v]) => v).filter(
        (v) => checks.querySelector(`input[value="${v}"]`).checked,
      );
      schedulePreview();
    });
    wrap.append(box, el("span", null, label));
    checks.append(wrap);
  }
  $("question-text").addEventListener("input", (e) => {
    state.question = e.target.value;
    schedulePreview();
  });
  $("question-format").addEventListener("change", (e) => changeFormat(e.target.value));
  $("question-discipline").addEventListener("change", (e) => {
    state.discipline = e.target.value;
    schedulePreview();
  });
  $("opt-repeat").addEventListener("change", (e) => {
    state.options.repeat_controlled_as_free = e.target.checked;
    schedulePreview();
  });
  $("opt-unindexed").addEventListener("change", (e) => {
    state.options.free_text_unindexed_only = e.target.checked;
    schedulePreview();
  });
  $("add-exclusion").addEventListener("click", () => {
    state.exclusions.push(emptyCondition());
    renderForm();
  });
}

// --- Llamadas al servidor ---

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const isJson = (response.headers.get("content-type") || "").includes("json");
  const payload = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    const message = isJson ? payload.error || "the server rejected the request" : payload;
    throw new Error(message);
  }
  return payload;
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 250);
}

async function refreshPreview() {
  const seq = ++previewSeq;
  try {
    const preview = await postJson("/api/preview", toData(state));
    // Una respuesta lenta no puede pisar a otra más reciente.
    if (seq !== previewSeq) return;
    showError(null);
    renderPreview(preview);
  } catch (error) {
    if (seq !== previewSeq) return;
    showError(error.message);
  }
}

function showError(message) {
  const banner = $("preview-error");
  banner.hidden = !message;
  banner.textContent = message ? `Not valid yet: ${message}` : "";
  document.querySelector(".preview").classList.toggle("stale", Boolean(message));
}

// --- Vista previa ---

function termChip(term) {
  const chip = el("span", `chip ${term.kind === "free" ? "free" : term.vocabulary}`);
  chip.append(el("span", "chip-text", term.text));
  let detail;
  if (term.kind === "free") detail = term.field === "title" ? "free · title" : "free";
  else {
    const scope = SCOPES.find(([v]) => v === term.scope);
    detail = `${term.vocabulary_label} · ${scope ? scope[1].replace(/ \[.*\]$/, "").toLowerCase() : term.scope}`;
  }
  chip.append(el("span", "chip-detail", detail));
  return chip;
}

function joined(items, separator) {
  const nodes = [];
  items.forEach((item, i) => {
    if (i) nodes.push(el("span", "op", separator));
    nodes.push(item);
  });
  return nodes;
}

function gridRow(condition, operator) {
  const row = el("div", "grid-row");
  const head = el("div", "grid-label");
  if (operator) head.append(el("span", "op op-strong", operator));
  head.append(el("span", null, condition.label));
  const terms = el("div", "grid-terms");
  terms.append(...joined(condition.terms.map(termChip), "OR"));
  row.append(head, terms);
  return row;
}

function renderGrid(grid) {
  const box = $("grid");
  if (!grid.rows.length) {
    box.replaceChildren(el("p", "empty", "No conditions yet."));
    return;
  }
  box.replaceChildren(
    ...grid.rows.map((c, i) => gridRow(c, i ? "AND" : null)),
    ...grid.exclusions.map((c) => gridRow(c, "NOT")),
  );
}

function renderTree(tree) {
  const root = el("ul", "tree");
  const top = el("li");
  top.append(el("span", "tree-question", tree.text));
  const components = el("ul");
  for (const component of tree.components) {
    const item = el("li");
    item.append(el("span", "role-pill", ROLE_LABELS[component.role]), el("span", null, ` ${component.description}`));
    if (component.conditions.length) {
      const conditions = el("ul");
      for (const condition of component.conditions) {
        const leaf = el("li");
        leaf.append(el("span", "tree-condition", condition.label));
        const chips = el("div", "chips");
        chips.append(...condition.terms.map(termChip));
        leaf.append(chips);
        conditions.append(leaf);
      }
      item.append(conditions);
    } else {
      item.append(el("span", "hint", " (not searched)"));
    }
    components.append(item);
  }
  if (tree.exclusions.length) {
    const item = el("li");
    item.append(el("span", "role-pill not", "Excluded"));
    const chips = el("div", "chips");
    for (const condition of tree.exclusions) chips.append(...condition.terms.map(termChip));
    item.append(chips);
    components.append(item);
  }
  top.append(components);
  root.append(top);
  $("tree").replaceChildren(root);
}

function renderSelection(selection) {
  const list = el("div", "db-list");
  const how = selection.mode === "manual" ? "Chosen by hand" : "Chosen automatically";
  for (const choice of selection.choices) {
    const card = el("div", `db-card${choice.searchable ? "" : " pending"}`);
    const head = el("div", "db-head");
    head.append(el("strong", null, choice.label));
    head.append(el("span", `tag ${choice.searchable ? "ok" : "later"}`, choice.searchable ? "searched" : "not searchable yet"));
    const reasons = el("ul", "reasons");
    for (const reason of choice.reasons) reasons.append(el("li", null, reason));
    card.append(head, reasons);
    list.append(card);
  }
  $("selection").replaceChildren(el("p", "hint", how), list);
}

function highlight(equation) {
  const out = el("code", "equation");
  const pattern = /("[^"]*"|\[[^\]]+\]|\b(?:AND|OR|NOT)\b|[()]|\s+|[^\s()"[]+)/g;
  for (const [token] of equation.matchAll(pattern)) {
    let cls = null;
    if (/^(AND|OR|NOT)$/.test(token)) cls = `op-${token.toLowerCase()}`;
    else if (token === "(" || token === ")") cls = "paren";
    else if (token.startsWith("[")) {
      const tag = token.slice(1, -1);
      cls = CONTROLLED_TAGS.has(tag) ? "ftag controlled" : tag === "sb" ? "ftag filter" : "ftag free";
    } else if (token.startsWith('"')) cls = "phrase";
    out.append(cls ? el("span", cls, token) : document.createTextNode(token));
  }
  return out;
}

function renderEquations(equations) {
  const box = $("equations");
  if (!equations.length) {
    box.replaceChildren(el("p", "empty", "None of the chosen databases has an equation yet."));
    return;
  }
  box.replaceChildren(...equations.map((item) => {
    const block = el("div", "equation-block");
    const head = el("div", "equation-head");
    head.append(el("span", "equation-db", item.label));
    const copy = el("button", "button small", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(item.equation);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Copy failed";
      }
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    });
    head.append(copy);
    const pre = el("pre", "equation-pre");
    pre.append(highlight(item.equation));
    block.append(head, pre);
    if (item.note) block.append(el("p", "note", item.note));
    return block;
  }));
}

function renderPreview(preview) {
  renderEquations(preview.equations);
  renderGrid(preview.grid);
  renderSelection(preview.selection);
  renderTree(preview.tree);
}

// --- Comprobación contra el tesauro ---

const STATUS_LABELS = { valid: "ok", entry_term: "synonym", not_found: "not found" };

function renderCheck(result) {
  const box = $("check-result");
  const nodes = [];
  if (!result.checks.length) nodes.push(el("p", "empty", `The question has no ${result.vocabulary_label} headings.`));
  const list = el("ul", "check-list");
  for (const check of result.checks) {
    const item = el("li", `check-item ${check.status}`);
    item.append(el("span", "tag", STATUS_LABELS[check.status]));
    let text;
    if (check.status === "valid") text = `${check.heading} (${check.identifier})`;
    else if (check.status === "entry_term") text = `${check.term} → ${check.heading} (${check.identifier})`;
    else text = `${check.term}: will be searched as free text`;
    item.append(el("span", null, ` ${text}`), el("span", "hint", ` · ${check.condition}`));
    if (check.note) item.append(el("p", "note", check.note));
    list.append(item);
  }
  if (result.checks.length) nodes.push(list);
  if (result.unchecked.length) {
    const names = result.unchecked.map((t) => `${t.text} (${t.vocabulary_label})`).join(", ");
    nodes.push(el("p", "note", `Not checked, biblio-search cannot check them against ${result.vocabulary_label}: ${names}`));
  }
  for (const suggestion of result.suggestions) {
    const row = el("div", "suggestions");
    row.append(el("span", "hint", `Synonyms for “${suggestion.condition}”:`));
    for (const synonym of suggestion.synonyms) {
      const add = el("button", "chip-button", `+ ${synonym}`);
      add.type = "button";
      add.title = "Add as free text";
      add.addEventListener("click", () => {
        addSynonym(suggestion.condition, synonym);
        add.disabled = true;
      });
      row.append(add);
    }
    nodes.push(row);
  }
  box.replaceChildren(...nodes);
  $("apply-check").hidden = result.checks.every((c) => c.status === "valid");
}

function addSynonym(label, synonym) {
  for (const condition of state.components.flatMap((c) => c.conditions)) {
    if (condition.label === label) {
      condition.terms.push({ kind: "free", text: synonym, scope: "heading", field: "title_abstract" });
      renderForm();
      schedulePreview();
      return;
    }
  }
}

async function runCheck() {
  const button = $("check-button");
  button.disabled = true;
  button.textContent = "Checking…";
  $("check-panel").hidden = false;
  $("check-result").replaceChildren(el("p", "empty", "Asking the MeSH thesaurus at NCBI…"));
  try {
    lastCheck = await postJson("/api/check", toData(state));
    renderCheck(lastCheck);
  } catch (error) {
    $("check-result").replaceChildren(el("p", "banner error", error.message));
    $("apply-check").hidden = true;
  } finally {
    button.disabled = false;
    button.textContent = "Check MeSH headings";
  }
}

// --- Búsqueda en directo (SSE sobre POST) ---

function progressStep(text, status) {
  const item = el("li", `step ${status}`, text);
  $("progress").append(item);
  return item;
}

function renderRecords(records, offset) {
  const list = $("records");
  list.start = offset + 1;
  list.replaceChildren(...records.map((record) => {
    const item = el("li", "record");
    const title = el("a", "record-title", record.title || "(no title)");
    title.href = record.url;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
    const authors = record.authors.length > 3
      ? `${record.authors.slice(0, 3).join(", ")} et al.`
      : record.authors.join(", ");
    const meta = el("div", "record-meta");
    meta.append(el("span", null, authors || "No authors listed"));
    meta.append(el("span", null, `${record.journal}${record.year ? `, ${record.year}` : ""}`));
    meta.append(el("span", "hint", `PMID ${record.id}`));
    if (record.doi) {
      const doi = el("a", "hint", `doi:${record.doi}`);
      doi.href = `https://doi.org/${encodeURI(record.doi)}`;
      doi.target = "_blank";
      doi.rel = "noopener noreferrer";
      meta.append(doi);
    }
    item.append(title, meta);
    if (record.publication_types.length) {
      const types = el("div", "chips");
      types.append(...record.publication_types.map((t) => el("span", "chip type", t)));
      item.append(types);
    }
    return item;
  }));
}

function renderCount(count) {
  const box = el("div", "count-box");
  const number = el("div", "count-number", count.count.toLocaleString("en"));
  box.append(number, el("div", "hint", "records found"));
  const details = el("div", "count-details");
  if (count.translation) {
    details.append(el("p", "field-label", "How PubMed understood the equation"));
    const pre = el("pre", "equation-pre small");
    pre.append(highlight(count.translation));
    details.append(pre);
  }
  for (const term of count.not_found) details.append(el("p", "banner warn", `Not found: ${term}`));
  for (const warning of count.warnings) details.append(el("p", "banner warn", warning));
  const row = el("div", "count-row");
  row.append(box, details);
  $("counts").append(row);
}

function handleEvent(name, data, steps) {
  const label = (db) => (DATABASES.find(([v]) => v === db) || [db, db])[1];
  if (name === "selection") {
    const names = data.choices.filter((c) => c.searchable).map((c) => c.label).join(", ");
    progressStep(`Databases chosen ${data.mode === "manual" ? "by hand" : "automatically"}: ${names || "none searchable"}`, "done");
  } else if (name === "skipped") {
    progressStep(`${data.label}: chosen, but biblio-search cannot search it yet`, "skipped");
  } else if (name === "equation") {
    steps[data.database] = progressStep(`${label(data.database)}: equation sent, waiting for the count…`, "running");
  } else if (name === "count") {
    steps[data.database].className = "step done";
    steps[data.database].textContent = `${label(data.database)}: ${data.count.toLocaleString("en")} records`;
    steps[`${data.database}-records`] = progressStep(`${label(data.database)}: fetching the first records…`, "running");
    renderCount(data);
  } else if (name === "records") {
    const step = steps[`${data.database}-records`];
    step.className = "step done";
    step.textContent = `${label(data.database)}: ${data.records.length} records listed`;
    renderRecords(data.records, 0);
  } else if (name === "error") {
    for (const step of Object.values(steps)) if (step.classList.contains("running")) step.className = "step failed";
    progressStep(data.message, "failed");
  } else if (name === "done") {
    progressStep("Search finished", "done");
  }
}

function parseEvent(block) {
  let name = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  return data.length ? { name, data: JSON.parse(data.join("\n")) } : null;
}

async function runSearch() {
  const button = $("search-button");
  button.disabled = true;
  $("progress").replaceChildren();
  $("counts").replaceChildren();
  $("records").replaceChildren();
  const steps = {};
  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: toData(state), max_records: Number($("max-records").value) }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload.error || (payload.detail && payload.detail[0] && payload.detail[0].msg);
      throw new Error(detail || `the server answered ${response.status}`);
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value.replace(/\r\n/g, "\n");
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const event = parseEvent(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
        if (event) handleEvent(event.name, event.data, steps);
      }
    }
  } catch (error) {
    progressStep(error.message, "failed");
  } finally {
    button.disabled = false;
  }
}

// --- Importar y exportar ---

async function importFile(file) {
  try {
    const text = await file.text();
    const { question } = await postJson("/api/import", { text });
    loadQuestion(question);
  } catch (error) {
    showError(`could not import ${file.name}: ${error.message}`);
  }
}

async function exportFile() {
  try {
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(toData(state)),
    });
    if (!response.ok) throw new Error((await response.json()).error);
    const url = URL.createObjectURL(await response.blob());
    const link = el("a");
    link.href = url;
    link.download = "question.toml";
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showError(`cannot export yet: ${error.message}`);
  }
}

function loadQuestion(data) {
  state = fromData(data);
  lastCheck = null;
  $("check-panel").hidden = true;
  renderForm();
  refreshPreview();
}

async function init() {
  setupStaticForm();
  $("search-button").addEventListener("click", runSearch);
  $("check-button").addEventListener("click", runCheck);
  $("apply-check").addEventListener("click", () => {
    if (lastCheck) loadQuestion(lastCheck.fixed);
  });
  $("export-button").addEventListener("click", exportFile);
  $("import-file").addEventListener("change", (e) => {
    const [file] = e.target.files;
    if (file) importFile(file);
    e.target.value = "";
  });
  renderForm();
  try {
    const response = await fetch("/api/initial");
    const { question } = await response.json();
    if (question) loadQuestion(question);
  } catch (error) {
    showError(`could not load the initial question: ${error.message}`);
  }
}

init();
