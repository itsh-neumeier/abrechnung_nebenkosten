// Entitäten-Felder mit Auswahl aus Home Assistant, Victron Modbus TCP und VRM Cloud.
//
// <input data-phases ...>  -> Umschaltung „1 Entität (gesamt)“ / „3 Phasen (L1/L2/L3)“.
//                             Im Phasen-Modus wird der Wert als „a + b + c“ gespeichert (Summe).
// <input data-entity ...>  -> Button „Auswählen“: Popup fragt zuerst die Datenquelle ab
//                             (Home Assistant / Victron Modbus TCP / VRM Cloud), dann die Liste.
// data-unit="volume"       -> Auswahl-Dialog startet mit Filter Wasser/Volumen.
// window.EntityFields.init(container) initialisiert nachträglich eingefügte Felder.
(function () {
  let cache = null, target = null, dlg = null, current = null;
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const UNITS = {energy: ['kWh', 'Wh', 'MWh', 'W', 'kW'], volume: ['m³', 'L', 'l', 'gal', 'ft³']};
  const split = (v) => (v || '').split(/[\s+,;]+/).filter(Boolean);
  const sourceOf = (id) => id.startsWith('victron:') ? 'victron' : id.startsWith('vrm:') ? 'vrm' : 'ha';
  const BADGE = {ha: '🏠 HA', victron: '🔌 MODBUS', vrm: '☁️ VRM'};
  const SOURCES = {
    ha: {title: 'Home Assistant Entitäten', text: 'Sensoren aus deinem Home Assistant – Zähler (kWh, m³) und Leistung (W), mit Vergangenheit aus der Langzeitstatistik.', icon: '🏠'},
    victron: {title: 'Victron Modbus TCP', text: 'Eigener Logger direkt am GX (lokal, nur lesend) – Werte ab Start des Loggers.', icon: '🔌'},
    vrm: {title: 'VRM Cloud', text: 'Energieflüsse aus dem Victron-Portal (Netz, PV, Batterie → Verbraucher), auch rückwirkend.', icon: '☁️'},
  };

  // ------------------------------------------------------------------ Dialog
  function dialog() {
    if (dlg) return dlg;
    dlg = document.createElement('dialog');
    dlg.className = 'picker';
    dlg.innerHTML = `
      <div class="picker-head">
        <span><button type="button" class="sec pick-btn" data-back hidden>← Quelle</button>
        <b data-title>Datenquelle wählen</b></span>
        <button type="button" class="sec" data-close>✕</button>
      </div>
      <div data-step="sources" class="picker-sources"></div>
      <div data-step="list" hidden>
        <div class="picker-filter">
          <input type="text" placeholder="Suchen (Name oder ID) …" data-q>
          <select data-unit>
            <option value="energy">Energie / Leistung (kWh, W)</option>
            <option value="volume">Wasser / Volumen (m³, L)</option>
            <option value="">alle Einheiten</option>
          </select>
          <label class="check"><input type="checkbox" data-stat checked> nur mit Statistik</label>
          <button type="button" class="sec" data-refresh title="Neu laden">↻</button>
        </div>
        <div class="picker-list" data-list><p class="muted">Lade …</p></div>
      </div>`;
    document.body.appendChild(dlg);
    const $ = (s) => dlg.querySelector(s);
    $('[data-list]').addEventListener('click', ev => {
      const tr = ev.target.closest('tr[data-id]');
      if (!tr || !target) return;
      target.value = tr.dataset.id;
      target.dispatchEvent(new Event('input', {bubbles: true}));
      dlg.close();
    });
    $('[data-step="sources"]').addEventListener('click', ev => {
      const tile = ev.target.closest('[data-src]');
      if (tile && !tile.disabled) showList(tile.dataset.src);
    });
    $('[data-back]').addEventListener('click', showSources);
    $('[data-q]').addEventListener('input', drawList);
    $('[data-unit]').addEventListener('change', drawList);
    $('[data-stat]').addEventListener('change', drawList);
    $('[data-refresh]').addEventListener('click', () => load(true).then(drawList));
    $('[data-close]').addEventListener('click', () => dlg.close());
    return dlg;
  }

  function showSources() {
    current = null;
    const $ = (s) => dlg.querySelector(s);
    $('[data-step="sources"]').hidden = false;
    $('[data-step="list"]').hidden = true;
    $('[data-back]').hidden = true;
    $('[data-title]').textContent = 'Datenquelle wählen';
    const box = $('[data-step="sources"]');
    if (!cache) { box.innerHTML = '<p class="muted">Lade Datenquellen …</p>'; return; }
    const cur = target && target.value.trim() ? sourceOf(target.value.trim()) : null;
    box.innerHTML = Object.entries(SOURCES).map(([key, s]) => {
      const st = cache[key] || {ok: false, hint: '', entities: []};
      const n = st.entities.length;
      return `<button type="button" class="src-tile ${key === cur ? 'current' : ''}" data-src="${key}" ${st.ok ? '' : 'disabled'}>
        <span class="src-icon">${s.icon}</span>
        <span><b>${s.title}</b>${key === cur ? ' <span class="badge">aktuell</span>' : ''}<br>
        <span class="muted">${s.text}</span><br>
        ${st.ok ? `<span class="src-count">${n} Einträge</span>` : ''}
        ${st.hint ? `<span class="${st.ok ? 'muted' : 'src-hint'}">${esc(st.hint)}</span>` : ''}</span>
      </button>`;
    }).join('');
  }

  function showList(src) {
    current = src;
    const $ = (s) => dlg.querySelector(s);
    $('[data-step="sources"]').hidden = true;
    $('[data-step="list"]').hidden = false;
    $('[data-back]').hidden = false;
    $('[data-title]').textContent = SOURCES[src].title;
    $('[data-q]').value = '';
    drawList();
    $('[data-q]').focus();
  }

  function drawList() {
    if (!current) return;
    const $ = (s) => dlg.querySelector(s), list = $('[data-list]');
    const all = (cache && cache[current] && cache[current].entities) || [];
    const q = $('[data-q]').value.toLowerCase(), unit = $('[data-unit]').value, stat = $('[data-stat]').checked;
    const rows = all.filter(e =>
      (!unit || UNITS[unit].includes(e.unit)) && (!stat || e.statistics) &&
      (!q || e.entity_id.toLowerCase().includes(q) || (e.name || '').toLowerCase().includes(q)));
    if (!rows.length) { list.innerHTML = '<p class="muted">Keine passenden Einträge – ggf. Filter „alle Einheiten“ wählen.</p>'; return; }
    list.innerHTML = '<table>' + rows.map(e => `
      <tr data-id="${esc(e.entity_id)}" class="${target && target.value.trim() === e.entity_id ? 'selected' : ''}">
        <td><b>${esc(e.name || e.entity_id)}</b><br><code>${esc(e.entity_id)}</code>
            ${e.statistics ? '' : '<br><span class="muted">keine Langzeitstatistik</span>'}</td>
        <td class="r">${esc(e.state)} ${esc(e.unit)}</td></tr>`).join('') + '</table>';
  }

  async function load(refresh) {
    try {
      const r = await fetch('/api/sources' + (refresh ? '?refresh=1' : ''));
      cache = await r.json();
    } catch (e) {
      cache = {ha: {ok: false, hint: 'Server nicht erreichbar.', entities: []}};
    }
    refreshChips();
  }

  async function open(input) {
    target = input;
    const d = dialog();
    d.querySelector('[data-unit]').value = input.dataset.unit ?? 'energy';
    d.showModal();
    showSources();
    if (!cache) { await load(false); showSources(); }
  }

  // Etikett unter dem Feld: Quelle (HA / MODBUS / VRM) + Klarname des Sensors
  const chips = [];
  function nameOf(id) {
    const src = cache && cache[sourceOf(id)];
    const e = src && src.entities.find(x => x.entity_id === id);
    return e ? (e.name || id) : id;
  }
  function renderChip(input, chip) {
    const ids = split(input.value);
    chip.innerHTML = ids.map(id => {
      const src = sourceOf(id);
      // Quellen-Präfix im Namen weglassen – die Quelle steht schon im Etikett
      const name = nameOf(id).replace(/^(VRM \(Cloud\)|Victron direkt) – /, '');
      return `<span class="src-chip src-${src}" title="${esc(id)}"><b>${BADGE[src]}</b> · ${esc(name)}</span>`;
    }).join(' ');
  }
  function refreshChips() { chips.forEach(([i, c]) => renderChip(i, c)); }

  function attachPicker(input) {
    if (input.dataset.pickerReady) return;
    input.dataset.pickerReady = '1';
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'sec pick-btn'; btn.textContent = 'Auswählen';
    btn.addEventListener('click', () => open(input));
    input.insertAdjacentElement('afterend', btn);
    const chip = document.createElement('div');
    chip.className = 'src-chips';
    btn.insertAdjacentElement('afterend', chip);
    chips.push([input, chip]);
    input.addEventListener('input', () => renderChip(input, chip));
    input.addEventListener('change', () => renderChip(input, chip));
    renderChip(input, chip);
  }

  // ------------------------------------------------------------------ Phasen
  function initPhases(input) {
    if (input.dataset.phasesReady) return;
    input.dataset.phasesReady = '1';
    const unit = input.dataset.unit || 'energy';
    const box = document.createElement('div');
    box.className = 'phase-box';
    input.parentNode.insertBefore(box, input);

    const mode = document.createElement('select');
    mode.className = 'phase-mode';
    mode.innerHTML = '<option value="1">1 Entität (gesamt)</option><option value="3">3 Phasen (L1/L2/L3)</option>';
    box.appendChild(mode);

    const single = document.createElement('div');
    single.className = 'phase-single';
    box.appendChild(single);
    single.appendChild(input);
    input.dataset.entity = input.dataset.entity || '';
    input.dataset.unit = unit;
    attachPicker(input);

    const grid = document.createElement('div');
    grid.className = 'phase-grid';
    const phases = ['L1', 'L2', 'L3'].map(l => {
      const wrap = document.createElement('div');
      wrap.innerHTML = `<span class="phase-label">${l}</span>`;
      const f = document.createElement('input');
      f.type = 'text'; f.placeholder = `sensor.…_${l.toLowerCase()}_energy`;
      f.dataset.entity = ''; f.dataset.unit = unit;
      wrap.appendChild(f);
      grid.appendChild(wrap);
      attachPicker(f);
      f.addEventListener('input', () => { input.value = phases.map(p => p.value.trim()).filter(Boolean).join(' + '); });
      return f;
    });
    box.appendChild(grid);

    const parts = split(input.value);
    if (parts.length === 3) { mode.value = '3'; phases.forEach((f, i) => f.value = parts[i]); }
    function show() {
      const three = mode.value === '3';
      single.hidden = three; grid.hidden = !three;
      if (three) {
        const p = split(input.value);
        phases.forEach((f, i) => { if (!f.value && p[i]) f.value = p[i]; });
        input.value = phases.map(f => f.value.trim()).filter(Boolean).join(' + ');
      }
    }
    mode.addEventListener('change', show);
    show();
  }

  function init(root) {
    root = root || document;
    root.querySelectorAll('[data-phases]').forEach(initPhases);
    root.querySelectorAll('[data-entity]').forEach(attachPicker);
  }

  window.EntityFields = {init};
  init(document);
  // Klarnamen für die Etiketten nachladen, sobald ein Feld einen Wert hat
  if (chips.some(([i]) => i.value.trim())) load(false);
})();
