// Entitäten-Felder mit Home-Assistant-Auswahl.
//
// <input data-phases ...>  -> Umschaltung „1 Entität (gesamt)“ / „3 Phasen (L1/L2/L3)“.
//                             Im Phasen-Modus wird der Wert als „a + b + c“ gespeichert (Summe).
// <input data-entity ...>  -> Button „Auswählen“ (ersetzt den Wert). Quellen: Home Assistant,
//                             Victron Modbus (victron:…, eigener Logger) und VRM (vrm:…, Cloud).
// data-unit="volume"       -> Auswahl-Dialog startet mit Filter Wasser/Volumen.
// window.EntityFields.init(container) initialisiert nachträglich eingefügte Felder.
(function () {
  let cache = null, target = null, dlg = null;
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const UNITS = {energy: ['kWh', 'Wh', 'MWh', 'W', 'kW'], volume: ['m³', 'L', 'l', 'gal', 'ft³']};
  const split = (v) => (v || '').split(/[\s+,;]+/).filter(Boolean);
  const source = (e) => e.entity_id.startsWith('victron:') ? 'victron' : e.entity_id.startsWith('vrm:') ? 'vrm' : 'ha';
  const SRC_LABEL = {ha: 'HA', victron: 'Victron Modbus', vrm: 'VRM'};

  // ------------------------------------------------------------------ Dialog
  function dialog() {
    if (dlg) return dlg;
    dlg = document.createElement('dialog');
    dlg.className = 'picker';
    dlg.innerHTML = `
      <div class="picker-head">
        <b>Datenquelle wählen</b>
        <button type="button" class="sec" data-close>✕</button>
      </div>
      <div class="picker-filter">
        <input type="text" placeholder="Suchen (Name oder entity_id) …" data-q>
        <select data-src>
          <option value="">alle Quellen</option>
          <option value="ha">Home Assistant</option>
          <option value="victron">Victron Modbus</option>
          <option value="vrm">VRM (Cloud)</option>
        </select>
        <select data-unit>
          <option value="energy">Energie / Leistung (kWh, W)</option>
          <option value="volume">Wasser/Volumen (m³/L)</option>
          <option value="">alle Sensoren</option>
        </select>
        <label class="check"><input type="checkbox" data-stat checked> nur mit Statistik</label>
        <button type="button" class="sec" data-refresh title="Neu laden">↻</button>
      </div>
      <div class="picker-list" data-list><p class="muted">Lade …</p></div>`;
    document.body.appendChild(dlg);
    const $ = (s) => dlg.querySelector(s);
    $('[data-list]').addEventListener('click', ev => {
      const tr = ev.target.closest('tr[data-id]');
      if (!tr || !target) return;
      target.value = tr.dataset.id;
      target.dispatchEvent(new Event('input', {bubbles: true}));
      dlg.close();
    });
    $('[data-q]').addEventListener('input', draw);
    $('[data-unit]').addEventListener('change', draw);
    $('[data-src]').addEventListener('change', draw);
    $('[data-stat]').addEventListener('change', draw);
    $('[data-refresh]').addEventListener('click', () => load(true));
    $('[data-close]').addEventListener('click', () => dlg.close());
    return dlg;
  }

  function draw() {
    const $ = (s) => dlg.querySelector(s), list = $('[data-list]');
    if (cache && cache.error) { list.innerHTML = `<p class="warn">${esc(cache.error)}</p>`; return; }
    if (!cache) { list.innerHTML = '<p class="muted">Lade …</p>'; return; }
    const q = $('[data-q]').value.toLowerCase(), unit = $('[data-unit]').value, stat = $('[data-stat]').checked;
    const src = $('[data-src]').value;
    const rows = cache.filter(e =>
      (!src || source(e) === src) &&
      (!unit || UNITS[unit].includes(e.unit)) && (!stat || e.statistics) &&
      (!q || e.entity_id.toLowerCase().includes(q) || (e.name || '').toLowerCase().includes(q)));
    if (!rows.length) {
      const hint = {victron: 'Victron Modbus: unter Einstellungen → „Victron direkt“ den Logger aktivieren.',
                    vrm: 'VRM: VRM_TOKEN setzen und unter Einstellungen → VRM die Anlage suchen.'}[src] || '';
      list.innerHTML = `<p class="muted">Keine passenden Entitäten. ${hint}</p>`; return;
    }
    list.innerHTML = '<table>' + rows.map(e => `
      <tr data-id="${esc(e.entity_id)}">
        <td><span class="src-badge src-${source(e)}">${SRC_LABEL[source(e)]}</span>
            <b>${esc(e.name || e.entity_id)}</b><br><code>${esc(e.entity_id)}</code>
            ${e.statistics ? '' : '<br><span class="muted">keine Langzeitstatistik</span>'}</td>
        <td class="r">${esc(e.state)} ${esc(e.unit)}</td></tr>`).join('') + '</table>';
  }

  async function load(refresh) {
    cache = null; draw();
    try {
      const r = await fetch('/api/entities' + (refresh ? '?refresh=1' : ''));
      cache = await r.json();
    } catch (e) { cache = {error: 'Home Assistant nicht erreichbar.'}; }
    draw();
  }

  function open(input) {
    target = input;
    const d = dialog();
    d.querySelector('[data-unit]').value = input.dataset.unit ?? 'energy';
    const cur = (input.value || '').trim();
    d.querySelector('[data-src]').value = cur.startsWith('victron:') ? 'victron' : cur.startsWith('vrm:') ? 'vrm' : '';
    d.querySelector('[data-q]').value = '';
    d.showModal();
    if (!cache || cache.error) load(false); else draw();
    d.querySelector('[data-q]').focus();
  }

  function attachPicker(input) {
    if (input.dataset.pickerReady) return;
    input.dataset.pickerReady = '1';
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'sec pick-btn'; btn.textContent = 'Auswählen (HA / Victron / VRM)';
    btn.addEventListener('click', () => open(input));
    input.insertAdjacentElement('afterend', btn);
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
})();
