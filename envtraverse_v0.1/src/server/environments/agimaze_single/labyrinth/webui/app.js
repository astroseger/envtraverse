const $ = (id) => document.getElementById(id);

let sessionId = null;
let stepNo = 0;
let WIN_BANNER = null;

async function loadWinBanner() {
  try {
    const r = await fetch("/webui/win.txt");
    if (r.ok) WIN_BANNER = await r.text();
  } catch (e) {
    WIN_BANNER = null;
  }
}

function setStatus(msg) {
  $("status").textContent = msg || "";
}

const LOG_MAX_LINES = 200;

function setLog(msg) {
  const el = $("log");
  if (!msg) return;

  const lines = ((msg + "\n" + el.textContent).trim()).split("\n");
  const clipped = lines.slice(0, LOG_MAX_LINES);
  el.textContent = clipped.join("\n");

  // keep the scroll at the top (newest first). Prevent layout jumps.
  el.scrollTop = 0;
}

function setMap(txt) {
  $("map").textContent = txt || "";
}

function setInv(inv) {
  if (!inv) {
    $("inv").textContent = "-";
    return;
  }
  const show = {};
  if ("key" in inv) show.key = inv.key;
  if ("treasure" in inv) show.treasure = inv.treasure;
  if ("exit_key" in inv) show.exit_key = inv.exit_key;
  $("inv").textContent = JSON.stringify(show);
}

function setRes(inv) {
  const box = $("res");
  const txt = $("resText");
  if (!inv) {
    box.style.display = "none";
    txt.textContent = "-";
    return;
  }
  const res = {};
  if ("grenades" in inv) res.grenades = inv.grenades;
  if ("flashlight" in inv) res.flashlight = inv.flashlight;
  const keys = Object.keys(res);
  if (keys.length === 0) {
    box.style.display = "none";
    txt.textContent = "-";
    return;
  }
  box.style.display = "inline-flex";
  txt.textContent = JSON.stringify(res);
}

function setInfo(info) {
  const box = $("info");
  const txt = $("infoText");
  if (!info) {
    box.style.display = "none";
    txt.textContent = "-";
    return;
  }
  box.style.display = "inline-flex";
  txt.textContent = `rivers=${info.rivers} pits=${info.pits_cycles}x${info.pits_cycle_len}`;
}

function setActions(actions) {
  const box = $("actions");
  box.innerHTML = "";
  (actions || []).forEach((a) => {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = a;
    btn.onclick = () => step(a);
    box.appendChild(btn);
  });
}

async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

async function apiPost(path, data) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

let CLASSIC_META = null;

async function loadClassicOptions() {
  const data = await apiGet("/api/classic");
  CLASSIC_META = data;

  const diffSel = $("classicDiff");
  const elemSel = $("classicElem");
  const sizeSel = $("classicSize");

  diffSel.innerHTML = "";
  (data.difficulties || []).forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    diffSel.appendChild(opt);
  });

  function refreshElems() {
    const d = diffSel.value;
    const elems = (data.elements && data.elements[d]) || [];
    elemSel.innerHTML = "";
    elems.forEach((e) => {
      const opt = document.createElement("option");
      opt.value = e;
      opt.textContent = e;
      elemSel.appendChild(opt);
    });
    refreshSizes();
  }

  function refreshSizes() {
    const d = diffSel.value;
    const e = elemSel.value;
    const sizes = (data.sizes && data.sizes[d] && data.sizes[d][e]) || [];
    sizeSel.innerHTML = "";
    sizes.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      sizeSel.appendChild(opt);
    });
  }

  diffSel.onchange = refreshElems;
  elemSel.onchange = refreshSizes;
  refreshElems();
}

let EXTENDED_META = null;

async function loadExtendedOptions() {
  const data = await apiGet("/api/extended");
  EXTENDED_META = data;

  const kindSel = $("extKind");
  const stageSel = $("extStage");

  kindSel.innerHTML = "";
  (data.extensions || []).forEach((e) => {
    const opt = document.createElement("option");
    opt.value = e;
    opt.textContent = e;
    kindSel.appendChild(opt);
  });

  function refreshStages() {
    const e = kindSel.value;
    const stages = (data.stages && data.stages[e]) || [];
    stageSel.innerHTML = "";
    stages.forEach((st) => {
      const opt = document.createElement("option");
      opt.value = st;
      opt.textContent = st;
      stageSel.appendChild(opt);
    });
  }

  kindSel.onchange = refreshStages;
  refreshStages();
}

async function loadTrainingOptions() {
  const data = await apiGet("/api/training");
  const seriesSel = $("series");
  const stageSel = $("stage");

  seriesSel.innerHTML = "";
  data.series.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    seriesSel.appendChild(opt);
  });

  function refreshStages() {
    const s = seriesSel.value;
    const stages = (data.stages && data.stages[s]) || [];
    stageSel.innerHTML = "";
    stages.forEach((st) => {
      const opt = document.createElement("option");
      opt.value = st;
      opt.textContent = st;
      stageSel.appendChild(opt);
    });
  }

  seriesSel.onchange = refreshStages;
  refreshStages();
}

async function loadTutorialOptions() {
  const data = await apiGet("/api/tutorial");
  const sel = $("tutorialStage");
  sel.innerHTML = "";
  (data.stages || []).forEach((st) => {
    const opt = document.createElement("option");
    opt.value = st;
    opt.textContent = st;
    sel.appendChild(opt);
  });
}
function groupChanged() {
  const g = $("group").value;
  if (g === "TUTORIAL") {
    $("tutorialBox").style.display = "flex";
    $("trainingBox").style.display = "none";
    $("trainingStageBox").style.display = "none";
    $("classicBox").style.display = "none";
    $("extendedBox").style.display = "none";
    $("browseBox").style.display = "none";
  } else if (g === "TRAINING") {
    $("tutorialBox").style.display = "none";
    $("trainingBox").style.display = "flex";
    $("trainingStageBox").style.display = "flex";
    $("classicBox").style.display = "none";
    $("extendedBox").style.display = "none";
    $("browseBox").style.display = "none";
  } else if (g === "CLASSIC") {
    $("tutorialBox").style.display = "none";
    $("trainingBox").style.display = "none";
    $("trainingStageBox").style.display = "none";
    $("classicBox").style.display = "block";
    $("extendedBox").style.display = "none";
    $("browseBox").style.display = "none";
  } else if (g === "EXTENDED") {
    $("tutorialBox").style.display = "none";
    $("trainingBox").style.display = "none";
    $("trainingStageBox").style.display = "none";
    $("classicBox").style.display = "none";
    $("extendedBox").style.display = "block";
    $("browseBox").style.display = "none";
  } else {
    $("tutorialBox").style.display = "none";
    $("trainingBox").style.display = "none";
    $("trainingStageBox").style.display = "none";
    $("classicBox").style.display = "none";
    $("extendedBox").style.display = "none";
    $("browseBox").style.display = "flex";
  }
}
async function start() {
  setStatus("starting...");
  const g = $("group").value;
  const lang = $("langGlobal").value;
  const seedRaw = $("seed").value.trim();
  const seed = seedRaw ? Number(seedRaw) : null;

  const payload = {
    group: g,
    lang,
    seed: Number.isFinite(seed) ? seed : null,
    client: "webui",
    agent: "human",
  };

  if (g === "TUTORIAL") {
    payload.stage = $("tutorialStage").value;
  } else if (g === "TRAINING") {
    payload.series = $("series").value;
    payload.stage = $("stage").value;
  } else if (g === "CLASSIC") {
    const d = $("classicDiff").value;
    const e = $("classicElem").value;
    const s = $("classicSize").value;
    payload.path = `${d}/${e}/${s}`;
  } else if (g === "EXTENDED") {
    const ext = $("extKind").value;
    const st = $("extStage").value;
    payload.path = `${ext}/${st}`;
  } else {
    payload.path = $("path").value.trim();
  }

  try {
    const res = await apiPost("/api/start", payload);
    sessionId = res.id;
    stepNo = 0;
    $("sid").textContent = sessionId;
    setMap(res.map);
    setInv(res.inventory);
    setRes(res.inventory);
    setActions(res.actions);
    $("log").textContent = "";
    if (res.text) setLog(res.text);
    setInfo(res.info);
    setStatus("ready");
  } catch (e) {
    setStatus("error");
    setLog(String(e));
  }
}

async function step(action) {
  if (!sessionId) return;
  try {
    const res = await apiPost("/api/step", { id: sessionId, action, seq: stepNo + 1 });
    if (res.text) {
      stepNo += 1;
      setLog(`[Step ${stepNo}] ${res.text}`);
    }
    setMap(res.map);
    setInv(res.inventory);
    setRes(res.inventory);
    if (res.done) {
      setStatus("WIN");
      setLog("=== YOU WIN ===");
      if (WIN_BANNER) setMap(WIN_BANNER);

      // Disable action buttons until a new game is started.
      const box = $("actions");
      [...box.querySelectorAll("button")].forEach((b) => (b.disabled = true));
    }
  } catch (e) {
    setLog(String(e));
  }
}

async function loadDescription(lang) {
  try {
    const data = await apiGet(`/api/description?lang=${encodeURIComponent(lang)}`);
    $("description").textContent = data.description || "";
  } catch (e) {
    $("description").textContent = "";
  }
}

function loadPageInfo(lang) {
  const ru = `Settings
- Group — выбирает набор/группу лабиринтов.

Группы
- TUTORIAL: лабиринты «открытые». На карте визуализируется текущее положение игрока и важные объекты:
  S — start и текущая позиция игрока
  T — treasure
  K — ключ от сокровищ
  E — выход (после сокровища)
- TRAINING / CLASSIC / EXTENDED: карта рисуется только для визуализации размеров поля и точки старта.
  Вся полезная информация о происходящем приходит в текстовых сообщениях (сверху — самые последние).

Seed
- Если seed не задан, игра выбирается из заранее сгенерированных и отобранных лабиринтов.
- Если задать seed, сервер будет генерировать новый лабиринт.
  Сложность может заметно варьироваться, и проходимость на 100% не гарантируется.`;

  const en = `Settings
- Group — selects the maze group / dataset.

Groups
- TUTORIAL: “open” mazes. The map visualizes the player position and key objects:
  S — start and current player position
  T — treasure
  K — treasure key
  E — exit (after taking the treasure)
- TRAINING / CLASSIC / EXTENDED: the map is shown only to visualize the board size and the start position.
  All actionable information comes from the text messages (newest messages are shown at the top).

Seed
- If seed is empty, a scenario is chosen from pre-generated, curated mazes.
- If seed is set, the server generates a new maze.
  Difficulty may vary significantly, and solvability is not guaranteed 100%.`;

  $("pageInfo").textContent = (lang === "ru") ? ru : en;
}

async function init() {
  await loadWinBanner();
  $("group").onchange = groupChanged;
  $("btnStart").onclick = start;
  const langSel = $("langGlobal");
  const savedLang = localStorage.getItem("lab_lang") || "en";
  langSel.value = savedLang;
  await loadDescription(savedLang);
  loadPageInfo(savedLang);

  langSel.onchange = async () => {
    localStorage.setItem("lab_lang", langSel.value);
    await loadDescription(langSel.value);
    loadPageInfo(langSel.value);
  };

  $("btnNew").onclick = () => {
    sessionId = null;
    $("sid").textContent = "-";
    $("log").textContent = "";
    setMap("(no game)");
    setActions([]);
    setInv(null);
    setRes(null);
    setStatus("");
  };

  groupChanged();
  await loadTutorialOptions();
  await loadTrainingOptions();
  await loadClassicOptions();
  await loadExtendedOptions();
}

init();
