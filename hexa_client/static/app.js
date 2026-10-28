"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const svgNS = "http://www.w3.org/2000/svg";
  const directionLabels = ["↖", "↗", "→", "↘", "↙", "←"];
  const directionNames = ["左上", "右上", "右", "右下", "左下", "左"];
  const terrainNames = ["平地", "道路", "山地", "池"];
  const terrainColors = ["#223b3e", "#514b3c", "#394554", "#183d56"];
  const terrainStrokes = ["#355257", "#756b51", "#596576", "#285a77"];
  const agentColors = ["#78d7be", "#b6d991", "#85bce5", "#d2aee6", "#e8c379", "#95d3d9"];
  let session = {mode: "disconnected", setting: null, state: null};
  let actions = [];
  let selectedAgent = 0;
  let selectedPos = null;
  let validation = null;
  let actionDay = null;
  let busy = false;
  let refreshBusy = false;
  let refreshPromise = null;
  let lastRefresh = 0;
  let pollFailed = false;
  let points = new Map();
  let mapView = null;
  let mapExtent = null;
  let mapSignature = "";
  let mapDrag = null;
  let lastMapPan = 0;
  let lastAutoEvent = 0;
  let lastAutoPlan = "";

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function svgElement(tag, attrs, text) {
    const node = document.createElementNS(svgNS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function countAgents() {
    return Array.isArray(session.setting?.agents) ? session.setting.agents.length : (session.state?.agents?.length || 0);
  }
  function kinds() {
    return Array.from({length: countAgents()}, (_, i) => Number(session.kinds?.[i] ?? session.state?.agents?.[i]?.kind ?? 0));
  }
  function currentAgents() {
    const selectedKinds = kinds();
    return Array.from({length: countAgents()}, (_, i) => ({
      kind: selectedKinds[i],
      pos: session.state?.agents?.[i]?.pos ?? session.setting?.agents?.[i] ?? 0,
      fuel: session.state?.agents?.[i]?.fuel ?? session.setting?.fuelLimits ?? 0
    }));
  }
  function day() { return Number(session.state?.day ?? 0); }
  function routeAgents() {
    const plan = session.autopilot?.lastPlan;
    return session.finished && plan?.day === day() && plan.startAgents ? plan.startAgents : currentAgents();
  }
  function daySteps() { return Number(session.setting?.daySteps?.[day()] ?? 0); }
  function hasMap() { return !!session.setting?.map?.width; }
  function isPractice() { return session.mode === "demo" || session.mode === "offline"; }
  function canOperate() { return hasMap() && !session.finished; }
  function isAutomatic() { return session.autopilot?.running === true; }
  function reportError(error) {
    const message = error instanceof Error ? error.message : String(error);
    $("globalError").textContent = message;
    $("globalError").hidden = false;
    addLog(message, "error");
  }
  function clearError() { $("globalError").hidden = true; $("globalError").textContent = ""; }
  function addLog(message, type = "info") {
    const list = $("eventLog");
    list.querySelector(".log-empty")?.remove();
    const li = element("li", type === "error" ? "log-error" : "");
    const time = new Date().toLocaleTimeString("ja-JP", {hour12: false});
    li.append(element("time", "log-time", time), element("span", "log-icon", type === "error" ? "!" : "●"), element("span", "log-message", message));
    list.prepend(li);
    while (list.children.length > 60) list.lastElementChild.remove();
  }
  async function request(path, data) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {method: data === undefined ? "GET" : "POST", headers: data === undefined ? {Accept: "application/json"} : {"Content-Type": "application/json", Accept: "application/json"}, body: data === undefined ? undefined : JSON.stringify(data), signal: controller.signal, cache: "no-store"});
      let result;
      try { result = await response.json(); } catch { throw new Error("サーバーから JSON 形式ではない応答が返りました。"); }
      if (!response.ok) throw new Error(result.error || `通信に失敗しました（HTTP ${response.status}）`);
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("通信がタイムアウトしました。サーバーの状態を確認してください。");
      if (error instanceof TypeError) throw new Error("クライアントサーバーと通信できません。起動状態を確認してください。");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  async function operation(task, waitForRefresh = true) {
    if (busy) return;
    busy = true; clearError(); updateControls();
    try { if (waitForRefresh && refreshPromise) await refreshPromise.catch(() => {}); clearError(); await task(); } catch (error) { reportError(error); }
    finally { busy = false; updateControls(); }
  }
  function invalidatePlan() {
    validation = null;
    renderValidation();
    renderActions();
    renderMap();
    renderMetrics();
  }
  function applySession(next, reset = false) {
    const previousDay = actionDay;
    const previousCount = countAgents();
    const previousState = JSON.stringify(session.state);
    const hadStateError = !!session.stateError;
    session = next;
    if (!session || typeof session !== "object") throw new Error("試合データの形式が正しくありません。");
    if (reset || countAgents() !== previousCount) {
      actions = Array.from({length: countAgents()}, () => []);
      selectedAgent = 0; selectedPos = null; validation = null;
      actionDay = session.state?.day ?? (hasMap() ? 0 : null);
      if (reset) mapView = null;
      lastAutoPlan = "";
    } else if (previousDay !== null && session.state?.day !== undefined && session.state.day !== previousDay) {
      validation = null;
      actions = Array.from({length: countAgents()}, () => []);
      if (!isAutomatic()) addLog(`DAY ${day() + 1} に切り替わりました。前日の行動をクリアしました。プランを再生成してください。`);
    } else if (previousState !== JSON.stringify(session.state)) {
      validation = null;
    }
    if (session.state?.day !== undefined) actionDay = session.state.day;
    const autoPlan = session.autopilot?.lastPlan;
    if (autoPlan && autoPlan.day === day()) {
      const key = JSON.stringify([autoPlan.day, autoPlan.actions]);
      if (key !== lastAutoPlan) {
        actions = validateActions(autoPlan.actions); validation = autoPlan.validation;
        lastAutoPlan = key;
      }
    }
    for (const event of session.autopilot?.events || []) {
      if (event.id > lastAutoEvent) { addLog(event.message, event.level); lastAutoEvent = event.id; }
    }
    if (session.stateError) {
      $("globalError").textContent = `状態を取得できません：${session.stateError}`;
      $("globalError").hidden = false;
    } else if (hadStateError) clearError();
    if (session.notice && reset) addLog(session.notice);
    lastRefresh = Date.now(); pollFailed = false;
    renderAll();
  }
  function renderAll() {
    renderMode(); renderAutopilot(); renderMetrics(); renderAgents(); renderActions(); renderValidation(); renderMap(); renderCellDetails(); updateControls();
  }
  function renderAutopilot() {
    const auto = session.autopilot || {};
    const names = {idle:"待機中", starting:"開始中", choosing:"役割を選択中", waiting:"開始・状態公開待ち", planning:"探索中", submitting:"提出中", waiting_next_day:"翌日待ち", reconnecting:"再接続中", completed:"完了", stopped:"停止中", error:"確認が必要"};
    $("autoPhase").textContent = names[auto.phase] || "待機中";
    $("autoPhase").className = `mini-badge${auto.phase === "completed" ? " valid" : auto.error ? " invalid" : ""}`;
    $("autoMessage").textContent = auto.message || "役割の選択から計画・検証・提出まで、自動で進めます。";
    const days = auto.days || [];
    const bowls = days.reduce((sum,item)=>sum+(item.score?.bowls || 0),0);
    const brands = new Set(days.flatMap(item=>item.score?.dailyBrands || []));
    $("autoSummary").textContent = days.length ? `${days.length} / ${session.setting?.daySteps?.length || 0} 日を実行 · 収集予測 ${brands.size} 系列 / ${bowls} 杯` : isPractice() ? "トークン不要。練習を自動で最後まで進められます。" : session.mode === "live" ? "開始すると競技サーバーへ各日の回答を自動提出します。" : "サンプルを開くか、競技サーバーに接続してください。";
    const list = $("autoDayResults"); list.replaceChildren();
    days.forEach(item=>{ const li=element("li"); li.append(element("strong","",`DAY ${item.day+1}`),element("span","",`${item.score.dailyBrandCount} 系列 / ${item.score.bowls} 杯`),element("small","",item.revision === null ? "練習を実行" : `受理 ${item.revision}`)); list.append(li); });
  }
  function renderMode() {
    const badge = $("modeBadge");
    const labels = {live: "サーバー接続中", demo: "サンプルで練習中", offline: "ローカルで練習中", disconnected: "未接続"};
    badge.replaceChildren(element("i"), document.createTextNode(labels[session.mode] || "未接続"));
    badge.className = `status-badge${session.mode === "live" ? " connected" : isPractice() ? " demo" : ""}`;
    $("pollEnabled").disabled = session.mode !== "live";
    if (session.endpoint) $("serverUrl").value = session.endpoint;
    $("lastUpdated").textContent = lastRefresh ? `最終更新 ${new Date(lastRefresh).toLocaleTimeString("ja-JP", {hour12: false})}` : "まだ更新していません";
    $("advanceButton").hidden = !isPractice();
    $("submitButton").firstChild.textContent = "回答を提出 ";
    $("submitButton").title = isPractice() ? "練習モードでは「翌日へ進む」でプランを実行できます" : "検証後に大会サーバーへ回答を提出";
  }
  function renderMetrics() {
    const setting = session.setting;
    if (!setting) return;
    const total = setting.daySteps?.length || 0;
    const current = Math.min(day() + 1, total);
    $("dayValue").replaceChildren(document.createTextNode(String(current).padStart(2, "0")), element("small", "", ` / ${String(total).padStart(2, "0")}`));
    $("dayCaption").textContent = session.finished ? "すべての日程が終了しました" : isAutomatic() ? "自動で計画・検証・提出しています" : isPractice() ? "自分のペースで、試合をシミュレーション" : session.state ? "本日の行動を編集中" : "サーバーの試合開始を待っています";
    $("stepsValue").replaceChildren(document.createTextNode(String(daySteps())), element("small", "", " STEPS"));
    const costs = actions.map((row, i) => plannedCost(row, i));
    const maxCost = costs.length ? Math.max(...costs) : 0;
    $("stepsCaption").textContent = `プラン最大 ${maxCost} / ${daySteps()} ステップ · 残り ${Math.max(0, daySteps() - maxCost)}`;
    const brands = new Set((setting.spots || []).map(spot => spot.brand));
    $("brandsValue").replaceChildren(document.createTextNode(String(brands.size).padStart(2, "0")), element("small", "", " BRANDS"));
    const knownCount = Array.isArray(session.knownBrands) ? session.knownBrands.length : 0;
    $("spotsCaption").textContent = `${setting.spots?.length || 0} 店舗${knownCount ? ` · これまでに ${knownCount} ブランド獲得` : " · 巡回エージェントで収集"}`;
    tick();
  }
  function tick() {
    if (!session.setting) return;
    const now = Date.now() / 1000;
    let seconds = null;
    if (session.finished) { $("timeValue").textContent = "FINISH"; $("timeCaption").textContent = "試合終了"; return; }
    if (isPractice()) { $("timeValue").textContent = "PRACTICE"; $("timeValue").style.fontSize = "26px"; $("timeCaption").textContent = `${session.setting.daySeconds?.[day()] || 0} 秒 / 日 · 時間制限なし`; return; }
    $("timeValue").style.fontSize = "";
    if (session.state?.endsAt) { seconds = Math.max(0, Math.ceil(session.state.endsAt - now)); $("timeCaption").textContent = "本日の回答締め切りまで"; }
    else if (session.setting.startsAt > now) { seconds = Math.max(0, Math.ceil(session.setting.startsAt - now)); $("timeCaption").textContent = "試合開始まで"; }
    else { $("timeValue").textContent = "--:--"; $("timeCaption").textContent = session.setting.startsAt === 0 ? "開始時刻は未設定です" : "試合状態の取得を待っています"; return; }
    $("timeValue").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    $("timeValue").style.color = seconds < 15 ? "var(--amber)" : "";
  }
  function renderAgents() {
    const agents = currentAgents();
    $("agentCount").textContent = `${agents.length} AGENTS`;
    const tabs = $("agentTabs"); tabs.replaceChildren();
    agents.forEach((agent, index) => {
      const tab = element("button", "agent-tab", `A${index + 1}`);
      tab.type = "button"; tab.role = "tab"; tab.id = `agentTab${index}`;
      tab.setAttribute("aria-selected", String(index === selectedAgent));
      tab.setAttribute("aria-controls", "agentInfo");
      tab.tabIndex = index === selectedAgent ? 0 : -1;
      tab.addEventListener("click", () => { selectedAgent = index; renderAgents(); renderActions(); renderMap(); });
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        selectedAgent = event.key === "Home" ? 0 : event.key === "End" ? agents.length - 1 : (selectedAgent + (event.key === "ArrowRight" ? 1 : -1) + agents.length) % agents.length;
        renderAgents(); renderActions(); renderMap(); $(`agentTab${selectedAgent}`).focus();
      });
      tabs.append(tab);
    });
    const info = $("agentInfo"); info.replaceChildren();
    if (!agents.length) { info.append(element("p", "empty-copy", "試合データの読み込み後に編集できます。")); return; }
    const agent = agents[selectedAgent];
    info.setAttribute("role", "tabpanel"); info.setAttribute("aria-labelledby", `agentTab${selectedAgent}`);
    const row = element("div", "agent-kind-row");
    const label = element("label", "", "役割"); label.htmlFor = "agentKind";
    const select = element("select"); select.id = "agentKind";
    [[0, "巡回エージェント"], [1, "補給エージェント"]].forEach(([value, name]) => { const option = element("option", "", name); option.value = String(value); select.append(option); });
    select.value = String(agent.kind); select.disabled = busy || !canSetKinds();
    select.title = canSetKinds() ? "エージェントの役割を設定" : "役割は初日の試合開始前に設定できます";
    select.addEventListener("change", () => { const nextKinds = kinds(); nextKinds[selectedAgent] = Number(select.value); operation(async () => { applySession(await request("/api/agents", {kinds: nextKinds})); invalidatePlan(); addLog(`A${selectedAgent + 1} を${Number(select.value) ? "補給" : "巡回"}エージェントに設定しました。`); }); });
    row.append(label, select);
    const stats = element("div", "agent-stats");
    const width = session.setting.map.width;
    const pos = element("span", "", "現在地 "); pos.append(element("strong", "", `(${agent.pos % width}, ${Math.floor(agent.pos / width)})`));
    const fuel = element("span", "", "燃料 "); fuel.append(element("strong", "", agent.kind === 1 ? "∞" : `${agent.fuel} / ${session.setting.fuelLimits}`));
    stats.append(pos, fuel); info.append(row, stats);
    const track = element("div", "fuel-track");
    const ratio = agent.kind === 1 ? 1 : Math.max(0, Math.min(1, agent.fuel / Math.max(1, session.setting.fuelLimits)));
    const fill = element("div", `fuel-fill${ratio < .25 ? " low" : ""}`); fill.style.width = `${ratio * 100}%`; track.append(fill); info.append(track);
    if (validation?.agents?.[selectedAgent]) {
      const predicted = validation.agents[selectedAgent];
      const hint = element("p", "agent-prediction", `予測 → (${predicted.pos % width}, ${Math.floor(predicted.pos / width)}) · 燃料 ${agent.kind === 1 ? "∞" : predicted.fuel}`);
      info.append(hint);
    }
  }
  function renderActions() {
    const list = $("actionList"); list.replaceChildren();
    const row = actions[selectedAgent] || [];
    $("actionCount").textContent = `${row.length} ACTIONS · ${hasMap() ? plannedCost(row, selectedAgent) : 0} STEPS`;
    if (!row.length) list.append(element("span", "empty-copy", "方向ボタンで行動を追加"));
    row.forEach((action, index) => {
      const chip = element("button", `action-chip${action < 0 ? " wait" : ""}`);
      chip.type = "button"; chip.title = `${index + 1} 番目：${action < 0 ? `${-action} ステップ待機` : directionNames[action]}（クリックで削除）`;
      chip.setAttribute("aria-label", chip.title);
      chip.append(element("small", "", String(index + 1)), document.createTextNode(action < 0 ? `⏸ ${-action}` : `${directionLabels[action]} ${action}`));
      chip.disabled = busy || !canOperate();
      chip.addEventListener("click", () => { actions[selectedAgent].splice(index, 1); invalidatePlan(); });
      list.append(chip);
    });
    if (document.activeElement !== $("answerEditor")) $("answerEditor").value = JSON.stringify(actions);
    updateControls();
  }
  function renderValidation() {
    const panel = $("validationPanel"), badge = $("planStatus");
    panel.replaceChildren(); panel.className = "validation-panel"; badge.className = "mini-badge";
    const body = element("div");
    if (!validation) {
      panel.append(element("span", "validation-symbol", "○"));
      body.append(element("strong", "", "提出前にプランを検証"), element("p", "", "全エージェントを当日のステップ数に合わせてください。"));
      badge.textContent = "未検証";
    } else {
      const valid = validation.valid === true;
      panel.classList.add(valid ? "valid" : "invalid"); badge.classList.add(valid ? "valid" : "invalid"); badge.textContent = valid ? "検証済み" : "要修正";
      panel.append(element("span", "validation-symbol", valid ? "✓" : "!"));
      body.append(element("strong", "", valid ? "プランは実行可能です" : "プランを見直してください"));
      const lines = [];
      if (validation.score) {
        const score = validation.score;
        lines.push(`本日 ${score.dailyBrandCount ?? score.brandCount ?? 0} ブランド / ${score.bowls ?? 0} 杯を獲得予定`);
        if (Array.isArray(score.newBrands) && score.newBrands.length) lines.push(`新規ブランド：${score.newBrands.join(", ")}`);
      }
      (validation.errors || []).forEach(error => lines.push(typeof error === "string" ? error : error.message || JSON.stringify(error)));
      (validation.warnings || []).forEach(error => lines.push(typeof error === "string" ? error : error.message || JSON.stringify(error)));
      body.append(element("p", "", lines.join("\n") || (valid ? "提出する準備ができました。" : "行動と役割の設定を確認してください。")));
    }
    panel.append(body);
  }
  function nextPosition(pos, direction) {
    const width = session.setting.map.width, x = pos % width, y = Math.floor(pos / width);
    const offset = y % 2 === 0 ? 1 : 0;
    const offsets = [[offset - 1, -1], [offset, -1], [1, 0], [offset, 1], [offset - 1, 1], [-1, 0]];
    const nextX = x + offsets[direction][0], nextY = y + offsets[direction][1];
    if (nextX < 0 || nextX >= width || nextY < 0 || nextY >= session.setting.map.height) return null;
    return nextY * width + nextX;
  }
  function terrainAt(pos) {
    const {map} = session.setting;
    return Number(map.cells?.[Math.floor(pos / map.width)]?.[pos % map.width] ?? 0);
  }
  function trafficAt(pos) { return Number((session.state?.traffics || []).find(item => item.pos === pos)?.status ?? 0); }
  function plannedCost(row, agentIndex) {
    if (!hasMap()) return 0;
    let pos = routeAgents()[agentIndex]?.pos ?? 0, cost = 0;
    for (const action of row) {
      if (action < 0) { cost += -action; continue; }
      const terrain = terrainAt(pos);
      cost += terrain === 1 ? [1, 2, 4][trafficAt(pos)] ?? 1 : terrain === 2 ? 3 : 2;
      const next = nextPosition(pos, action);
      if (next !== null) pos = next;
    }
    return cost;
  }
  function renderMap() {
    const svg = $("hexMap"); svg.replaceChildren(); points = new Map();
    $("mapEmpty").hidden = hasMap();
    $("mapZoomControls").hidden = !hasMap();
    if (!hasMap()) return;
    const {map, spots = []} = session.setting;
    $("mapDimensions").textContent = `${map.width} × ${map.height} · ${map.width * map.height} HEXES`;
    const size = 25, hexWidth = Math.sqrt(3) * size, padding = 33;
    const totalWidth = hexWidth * (map.width + .5) + padding * 2;
    const totalHeight = size * 1.5 * (map.height - 1) + size * 2 + padding * 2;
    const signature = `${map.width},${map.height}`;
    mapExtent = {x: 0, y: 0, width: totalWidth, height: totalHeight};
    if (!mapView || mapSignature !== signature) mapView = {...mapExtent};
    mapSignature = signature; applyMapView();
    const title = svgElement("title", {}, `${map.width} 列 ${map.height} 行の六角形マップ。マスをクリックすると詳細を表示します。`);
    svg.append(title);
    const cellLayer = svgElement("g"), routeLayer = svgElement("g", {"pointer-events": "none"}), markerLayer = svgElement("g", {"pointer-events": "none"});
    for (let y = 0; y < map.height; y++) for (let x = 0; x < map.width; x++) {
      const pos = y * map.width + x;
      const cx = padding + hexWidth / 2 + hexWidth * (x + (y % 2 === 0 ? .5 : 0));
      const cy = padding + size + size * 1.5 * y;
      points.set(pos, [cx, cy]);
      const terrain = terrainAt(pos), spot = spots.find(item => item.pos === pos);
      const polygonPoints = Array.from({length: 6}, (_, i) => { const angle = Math.PI / 180 * (60 * i - 30); return `${cx + (size - 1.5) * Math.cos(angle)},${cy + (size - 1.5) * Math.sin(angle)}`; }).join(" ");
      const group = svgElement("g", {class: "hex-cell", tabindex: "0", role: "button", "aria-label": `座標 ${x}, ${y}、${terrainNames[terrain] || "地形不明"}${spot ? `、うどん店 ブランド ${spot.brand}` : ""}`, "aria-pressed": String(selectedPos === pos)});
      group.append(svgElement("polygon", {points: polygonPoints, fill: terrainColors[terrain] || terrainColors[0], stroke: selectedPos === pos ? "#f5cb80" : terrainStrokes[terrain] || terrainStrokes[0], "stroke-width": selectedPos === pos ? 2 : .8}));
      if (terrain === 2) {
        group.append(svgElement("path", {d: `M${cx - 8},${cy + 2} L${cx - 1},${cy - 8} L${cx + 3},${cy - 2} L${cx + 6},${cy - 5} L${cx + 12},${cy + 2}`, fill: "none", stroke: "#677685", "stroke-width": "1.2", opacity: ".8"}));
      } else if (terrain === 3) {
        group.append(svgElement("path", {d: `M${cx - 10},${cy - 4} q4,-3 8,0 t8,0 M${cx - 7},${cy + 1} q4,-3 8,0 t8,0`, fill: "none", stroke: "#4b7691", "stroke-width": "1", opacity: ".7"}));
      }
      if (spot) {
        group.append(svgElement("circle", {cx, cy: cy - 4, r: 10, fill: "#f3bd6d", stroke: "#ffe2ab", "stroke-width": .8}));
        group.append(svgElement("text", {x: cx, y: cy - .5, "text-anchor": "middle", class: "hex-brand"}, spot.brand));
      }
      const traffic = trafficAt(pos);
      if (terrain === 1 && traffic > 0) group.append(svgElement("text", {x: cx, y: cy - 5, "text-anchor": "middle", class: "hex-traffic"}, traffic === 1 ? "混雑" : "渋滞"));
      group.append(svgElement("text", {x: cx, y: cy + 14, "text-anchor": "middle", class: "hex-coordinate"}, `${x},${y}`));
      const choose = () => { if (Date.now() - lastMapPan < 200) return; selectedPos = pos; renderMap(); renderCellDetails(); };
      group.addEventListener("click", choose);
      group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); } });
      cellLayer.append(group);
    }
    routeAgents().forEach((agent, index) => {
      let pos = agent.pos;
      const path = [points.get(pos)].filter(Boolean);
      for (const action of actions[index] || []) {
        if (action < 0) continue;
        const next = nextPosition(pos, action);
        if (next === null || terrainAt(next) === 3) break;
        pos = next; if (points.has(pos)) path.push(points.get(pos));
      }
      if (path.length > 1) {
        routeLayer.append(svgElement("polyline", {points: path.map(point => point.join(",")).join(" "), fill: "none", stroke: agentColors[index % agentColors.length], "stroke-width": index === selectedAgent ? 2.7 : 1.7, "stroke-linecap": "round", "stroke-linejoin": "round", "stroke-dasharray": "4 5", opacity: index === selectedAgent ? .95 : .4}));
        const end = path[path.length - 1]; routeLayer.append(svgElement("circle", {cx: end[0], cy: end[1], r: 5, fill: "none", stroke: agentColors[index % agentColors.length], "stroke-width": 1.5, opacity: .8}));
      }
    });
    const grouped = new Map();
    function marker(agent, label, own, index) { const list = grouped.get(agent.pos) || []; list.push({agent, label, own, index}); grouped.set(agent.pos, list); }
    currentAgents().forEach((agent, index) => marker(agent, `A${index + 1}`, true, index));
    (session.state?.others || []).forEach(team => (team.agents || []).forEach((agent, index) => marker(agent, `${team.id + 1}.${index + 1}`, false, index)));
    grouped.forEach((items, pos) => {
      const center = points.get(pos); if (!center) return;
      const visible = items.slice(0, 4);
      visible.forEach((item, i) => {
        const cx = center[0] + (i - (visible.length - 1) / 2) * 13;
        const cy = center[1] - (spots.some(spot => spot.pos === pos) ? 15 : 3);
        markerLayer.append(svgElement("circle", {cx, cy, r: item.own ? 10 : 8, fill: item.own ? agentColors[item.index % agentColors.length] : "#85586b", stroke: item.own && item.index === selectedAgent ? "#e0fff0" : "#1c2b35", "stroke-width": item.own && item.index === selectedAgent ? 1.8 : 1}));
        markerLayer.append(svgElement("text", {x: cx, y: cy + 2.7, "text-anchor": "middle", class: item.own ? "hex-agent-label" : "hex-other-label"}, item.label));
        if (item.own && item.agent.kind === 1) markerLayer.append(svgElement("text", {x: cx + 8, y: cy - 7, "text-anchor": "middle", fill: "#f9d48f", "font-size": "10", "font-weight": "700", stroke: "#11232c", "stroke-width": ".4"}, "+"));
      });
      if (items.length > 4) markerLayer.append(svgElement("text", {x: center[0], y: center[1] - 23, "text-anchor": "middle", fill: "#b8cfdf", "font-size": "7"}, `+${items.length - 4}`));
    });
    svg.append(cellLayer, routeLayer, markerLayer);
  }
  function applyMapView() {
    if (!mapView) return;
    $("hexMap").setAttribute("viewBox", `${mapView.x} ${mapView.y} ${mapView.width} ${mapView.height}`);
    $("zoomLevel").textContent = `${Math.round(mapExtent.width / mapView.width * 100)}%`;
  }
  function zoomMap(factor) {
    if (!mapView || !mapExtent) return;
    const width = Math.max(mapExtent.width / 12, Math.min(mapExtent.width * 1.25, mapView.width / factor));
    const height = width * mapExtent.height / mapExtent.width;
    mapView = {x: mapView.x + (mapView.width - width) / 2, y: mapView.y + (mapView.height - height) / 2, width, height};
    applyMapView();
  }
  function renderCellDetails() {
    const details = $("cellDetails"); details.replaceChildren(element("span", "detail-icon", "⌖"));
    const body = element("div");
    if (selectedPos === null || !hasMap()) {
      body.append(element("strong", "", "マスを選択"), element("p", "", "地形・うどん店・エージェントの情報を確認できます。"));
    } else {
      const width = session.setting.map.width, terrain = terrainAt(selectedPos), spot = (session.setting.spots || []).find(item => item.pos === selectedPos);
      body.append(element("strong", "", `(${selectedPos % width}, ${Math.floor(selectedPos / width)}) · ${terrainNames[terrain] || "地形不明"}${spot ? ` · うどん店 ブランド ${spot.brand}` : ""}`));
      const meta = element("p", "detail-meta");
      if (terrain === 3) meta.append(element("span", "", "通行できません"));
      else meta.append(element("span", "", `出発時 ${terrain === 1 ? [1, 2, 4][trafficAt(selectedPos)] : terrain === 2 ? 3 : 2} ステップ / 燃料 ${terrain === 0 ? 1 : 2}`));
      if (spot) {
        const stock = validation?.stocks?.find(item => item.pos === selectedPos)?.stocks;
        meta.append(element("span", "", `在庫 ${spot.stocks} 杯 / チーム・日${stock !== undefined ? `（プラン後 ${stock} 杯）` : ""}`));
      }
      const agents = currentAgents().map((agent, i) => agent.pos === selectedPos ? `A${i + 1}` : null).filter(Boolean);
      if (agents.length) meta.append(element("span", "", `自チーム ${agents.join("・")}`));
      const others = (session.state?.others || []).flatMap(team => (team.agents || []).filter(agent => agent.pos === selectedPos));
      if (others.length) meta.append(element("span", "", `相手 ${others.length} エージェント`));
      if (terrain === 1) meta.append(element("span", "traffic-legend", ["通常", "混雑", "渋滞"][trafficAt(selectedPos)]));
      body.append(meta);
    }
    details.append(body);
  }
  function updateControls() {
    const editDisabled = busy || !canOperate() || isAutomatic();
    document.querySelectorAll("[data-direction]").forEach(button => { button.disabled = editDisabled; });
    document.querySelectorAll(".action-chip").forEach(button => { button.disabled = editDisabled; });
    ["waitButton", "applyJsonButton", "planButton", "validateButton", "downloadButton", "importAnswerButton"].forEach(id => { $(id).disabled = editDisabled; });
    $("undoButton").disabled = editDisabled || !actions[selectedAgent]?.length;
    $("resetButton").disabled = editDisabled || !actions[selectedAgent]?.length;
    $("fillWaitButton").disabled = editDisabled || plannedCost(actions[selectedAgent] || [], selectedAgent) >= daySteps();
    $("submitButton").disabled = editDisabled || session.mode !== "live" || !session.state || !!session.stateError;
    $("advanceButton").disabled = editDisabled || !session.state;
    $("planButton").disabled = editDisabled || !session.state || !!session.stateError;
    $("validateButton").disabled = editDisabled || !session.state || !!session.stateError;
    $("refreshButton").disabled = busy || refreshBusy || session.mode !== "live";
    ["connectButton", "demoButton", "emptyDemoButton", "importConfigButton"].forEach(id => { $(id).disabled = busy || isAutomatic(); });
    $("autoStartButton").disabled = busy || !canOperate() || isAutomatic();
    $("autoStopButton").disabled = busy || !isAutomatic();
    $("autoKinds").disabled = busy || isAutomatic(); $("autoSearch").disabled = busy || isAutomatic();
    $("autoDownloadButton").disabled = !(session.autopilot?.days?.length);
    if ($("agentKind")) $("agentKind").disabled = busy || !canSetKinds();
    $("answerEditor").disabled = editDisabled; $("waitSteps").disabled = editDisabled;
    document.body.classList.toggle("busy", busy);
  }
  function canSetKinds() {
    return canOperate() && !isAutomatic() && (isPractice() ? day() === 0 && !session.history?.length : !session.state && (session.setting.startsAt === 0 || session.setting.startsAt > Date.now() / 1000));
  }
  function validateActions(value) {
    if (!Array.isArray(value) || value.length !== countAgents()) throw new Error(`回答は ${countAgents()} エージェント分の配列にしてください。例：[[2, 3], [-1]]`);
    if (!value.every(row => Array.isArray(row) && row.every(action => Number.isSafeInteger(action) && action <= 5))) throw new Error("各行動は 0〜5 の移動方向、または負の整数による待機を指定してください。");
    return value.map(row => row.slice());
  }
  function appendAction(action) {
    if (!canOperate() || busy || isAutomatic()) return;
    if (!actions[selectedAgent]) actions[selectedAgent] = [];
    actions[selectedAgent].push(action); invalidatePlan();
  }
  async function refresh(manual = false) {
    if (refreshBusy || busy || (session.mode !== "live" && !isAutomatic())) return;
    refreshBusy = true; updateControls();
    try {
      refreshPromise = request(isAutomatic() ? "/api/session" : "/api/refresh");
      const next = await refreshPromise;
      const recovered = pollFailed;
      if (manual || recovered) clearError();
      applySession(next);
      if (manual) addLog("試合状態を更新しました。");
      else if (recovered) addLog("サーバーとの通信が復旧しました。");
    } catch (error) {
      if (!pollFailed || manual) reportError(error);
      pollFailed = true;
    } finally { refreshBusy = false; refreshPromise = null; updateControls(); }
  }
  $("connectionToggle").addEventListener("click", () => {
    const show = $("connectionPanel").hidden; $("connectionPanel").hidden = !show; $("connectionToggle").setAttribute("aria-expanded", String(show));
  });
  $("connectForm").addEventListener("submit", event => {
    event.preventDefault();
    const url = $("serverUrl").value.trim(), token = $("teamToken").value;
    operation(async () => { const next = await request("/api/connect", {url, token}); $("teamToken").value = ""; applySession(next, true); $("pollEnabled").checked = true; addLog("サーバーに接続しました。マップと試合状態を取得しました。"); });
  });
  function startDemo() { operation(async () => { applySession(await request("/api/demo", {}), true); addLog("サンプルマップを読み込みました。自動プラン生成や日送りを試せます。"); }); }
  $("demoButton").addEventListener("click", startDemo); $("emptyDemoButton").addEventListener("click", startDemo);
  $("importConfigButton").addEventListener("click", () => $("configFile").click());
  $("configFile").addEventListener("change", event => {
    const file = event.target.files[0]; if (!file) return;
    operation(async () => { const config = JSON.parse(await file.text()); applySession(await request("/api/import", {config}), true); addLog(`試合設定「${file.name}」を読み込みました。`); });
    event.target.value = "";
  });
  document.querySelectorAll("[data-direction]").forEach(button => button.addEventListener("click", () => appendAction(Number(button.dataset.direction))));
  $("waitButton").addEventListener("click", () => {
    const steps = Number($("waitSteps").value);
    if (!Number.isSafeInteger(steps) || steps < 1 || steps > 1000000) { reportError(new Error("待機ステップは 1〜1,000,000 の整数を指定してください。")); return; }
    appendAction(-steps);
  });
  $("undoButton").addEventListener("click", () => { actions[selectedAgent]?.pop(); invalidatePlan(); });
  $("resetButton").addEventListener("click", () => { actions[selectedAgent] = []; invalidatePlan(); });
  $("fillWaitButton").addEventListener("click", () => { const remaining = daySteps() - plannedCost(actions[selectedAgent] || [], selectedAgent); if (remaining > 0) appendAction(-remaining); });
  $("applyJsonButton").addEventListener("click", () => { try { actions = validateActions(JSON.parse($("answerEditor").value)); clearError(); invalidatePlan(); addLog("行動 JSON をプランに反映しました。"); } catch (error) { reportError(error); } });
  $("planButton").addEventListener("click", () => operation(async () => {
    const result = await request("/api/plan", {}); actions = validateActions(result.actions); validation = result.validation;
    renderAll(); addLog(`自動プランを生成しました。${validation?.score ? `獲得予測 ${validation.score.bowls} 杯。` : ""}`);
  }));
  $("autoStartButton").addEventListener("click", () => operation(async () => {
    applySession(await request("/api/auto/start", {autoKinds:$("autoKinds").checked, searchSeconds:Number($("autoSearch").value)}));
  }));
  $("autoStopButton").addEventListener("click", () => operation(async () => {
    const stopped = request("/api/auto/stop", {});
    if (refreshPromise) await refreshPromise.catch(() => {});
    applySession(await stopped);
  }, false));
  $("autoDownloadButton").addEventListener("click", () => {
    const result = {mode:session.mode, predicted:true, days:session.autopilot?.days || []};
    const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)+"\n"],{type:"application/json"}));
    const link=element("a"); link.href=url; link.download="hexa-udon-auto-results.json"; document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  $("validateButton").addEventListener("click", () => operation(async () => { validation = await request("/api/validate", {actions}); renderAll(); addLog(validation.valid ? "プランの検証が完了しました。実行可能です。" : "プランに修正が必要です。検証結果を確認してください。", validation.valid ? "info" : "error"); }));
  $("submitButton").addEventListener("click", () => operation(async () => {
    const submittedDay = day();
    validation = await request("/api/validate", {actions}); renderAll();
    if (!validation.valid) throw new Error("検証でエラーが見つかりました。プランを修正してから提出してください。");
    const result = await request("/api/submit", {actions, day: submittedDay});
    addLog(`DAY ${submittedDay + 1} の回答を${isPractice() ? "練習" : "サーバーに"}提出しました。リビジョン ${result.revision ?? "—"}。`);
  }));
  $("advanceButton").addEventListener("click", () => operation(async () => {
    validation = await request("/api/validate", {actions}); renderAll();
    if (!validation.valid) throw new Error("プランにエラーがあるため日を進められません。検証結果を確認してください。");
    const oldDay = day();
    const next = await request("/api/advance", {actions});
    applySession(next, true);
    const history = session.history?.find(item => item.day === oldDay);
    addLog(`DAY ${oldDay + 1} の練習を実行しました。${history?.score ? `${history.score.bowls} 杯獲得。` : ""}${session.finished ? "試合終了です。" : `DAY ${day() + 1} のプランを作成できます。`}`);
  }));
  $("downloadButton").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(actions, null, 2) + "\n"], {type: "application/json"}));
    const link = element("a"); link.href = url; link.download = `hexa-udon-day-${day() + 1}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); addLog("回答 JSON を保存しました。");
  });
  $("importAnswerButton").addEventListener("click", () => $("answerFile").click());
  $("answerFile").addEventListener("change", async event => {
    const file = event.target.files[0]; if (!file) return;
    try { const value = JSON.parse(await file.text()); actions = validateActions(Array.isArray(value) ? value : value.actions); clearError(); invalidatePlan(); addLog(`回答「${file.name}」を読み込みました。`); }
    catch (error) { reportError(error); }
    event.target.value = "";
  });
  $("refreshButton").addEventListener("click", () => refresh(true));
  $("fitButton").addEventListener("click", () => { mapView = mapExtent ? {...mapExtent} : null; applyMapView(); });
  $("zoomInButton").addEventListener("click", () => zoomMap(1.35));
  $("zoomOutButton").addEventListener("click", () => zoomMap(1 / 1.35));
  $("hexMap").addEventListener("keydown", event => {
    if (["+", "=", "-", "0"].includes(event.key)) {
      event.preventDefault(); if (event.key === "0") { mapView = mapExtent ? {...mapExtent} : null; applyMapView(); } else zoomMap(event.key === "-" ? 1 / 1.35 : 1.35);
    }
  });
  $("hexMap").addEventListener("wheel", event => { if (hasMap() && (event.ctrlKey || event.metaKey)) { event.preventDefault(); zoomMap(event.deltaY < 0 ? 1.15 : 1 / 1.15); } }, {passive: false});
  $("hexMap").addEventListener("pointerdown", event => {
    if (!mapView || event.button !== 0) return;
    mapDrag = {pointer: event.pointerId, x: event.clientX, y: event.clientY, view: {...mapView}, moved: false};
  });
  $("hexMap").addEventListener("pointermove", event => {
    if (!mapDrag || mapDrag.pointer !== event.pointerId) return;
    const dx = event.clientX - mapDrag.x, dy = event.clientY - mapDrag.y;
    if (!mapDrag.moved && Math.hypot(dx, dy) < 5) return;
    mapDrag.moved = true; $("hexMap").classList.add("dragging");
    $("hexMap").setPointerCapture(event.pointerId);
    const rect = $("hexMap").getBoundingClientRect();
    const scale = Math.max(mapDrag.view.width / rect.width, mapDrag.view.height / rect.height);
    mapView = {...mapDrag.view, x: mapDrag.view.x - dx * scale, y: mapDrag.view.y - dy * scale}; applyMapView();
  });
  function endMapDrag(event) {
    if (mapDrag?.moved) lastMapPan = Date.now();
    mapDrag = null; $("hexMap").classList.remove("dragging");
    if ($("hexMap").hasPointerCapture(event.pointerId)) $("hexMap").releasePointerCapture(event.pointerId);
  }
  $("hexMap").addEventListener("pointerup", endMapDrag); $("hexMap").addEventListener("pointercancel", endMapDrag);
  $("clearLogButton").addEventListener("click", () => { $("eventLog").replaceChildren(element("li", "log-empty", "ログをクリアしました。")); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && ($("pollEnabled").checked || isAutomatic())) refresh(); });
  setInterval(() => { tick(); if (!document.hidden && ($("pollEnabled").checked || isAutomatic())) refresh(); }, 1000);
  updateControls();
  request("/api/session").then(next => { applySession(next, true); if (hasMap()) addLog("現在のセッションを読み込みました。"); }).catch(reportError);
})();
