// Entitäten-Auswahl aus Home Assistant.
// Felder mit data-entity bekommen einen Button "Aus HA wählen".
// data-entity="multi" (Textarea) hängt die Auswahl als neue Zeile an.
(function () {
  const fields = document.querySelectorAll('[data-entity]');
  if (!fields.length) return;

  let cache = null, target = null;
  const dlg = document.createElement('dialog');
  dlg.className = 'picker';
  dlg.innerHTML = `
    <div class="picker-head">
      <b>Entität aus Home Assistant wählen</b>
      <button type="button" class="sec" data-close>✕</button>
    </div>
    <div class="picker-filter">
      <input type="text" placeholder="Suchen (Name oder entity_id) …" data-q>
      <select data-unit>
        <option value="energy">Energie (kWh/Wh)</option>
        <option value="volume">Wasser/Volumen (m³/L)</option>
        <option value="">alle Sensoren</option>
      </select>
      <label class="check"><input type="checkbox" data-stat checked> nur mit Statistik</label>
      <button type="button" class="sec" data-refresh title="Neu laden">↻</button>
    </div>
    <div class="picker-list" data-list><p class="muted">Lade …</p></div>`;
  document.body.appendChild(dlg);
  const $ = (s) => dlg.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const UNITS = {energy: ['kWh', 'Wh', 'MWh'], volume: ['m³', 'L', 'l', 'gal', 'ft³']};

  function draw() {
    const list = $('[data-list]');
    if (cache && cache.error) { list.innerHTML = `<p class="warn">${esc(cache.error)}</p>`; return; }
    if (!cache) { list.innerHTML = '<p class="muted">Lade …</p>'; return; }
    const q = $('[data-q]').value.toLowerCase(), unit = $('[data-unit]').value, stat = $('[data-stat]').checked;
    const rows = cache.filter(e =>
      (!unit || UNITS[unit].includes(e.unit)) && (!stat || e.statistics) &&
      (!q || e.entity_id.toLowerCase().includes(q) || (e.name || '').toLowerCase().includes(q)));
    if (!rows.length) { list.innerHTML = '<p class="muted">Keine passenden Entitäten.</p>'; return; }
    list.innerHTML = '<table>' + rows.map(e => `
      <tr data-id="${esc(e.entity_id)}">
        <td><b>${esc(e.name || e.entity_id)}</b><br><code>${esc(e.entity_id)}</code>
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

  $('[data-list]').addEventListener('click', ev => {
    const tr = ev.target.closest('tr[data-id]');
    if (!tr || !target) return;
    const id = tr.dataset.id;
    if (target.dataset.entity === 'sum') {
      const cur = target.value.split(/[\s+,;]+/).filter(Boolean);
      if (!cur.includes(id)) cur.push(id);
      target.value = cur.join(' + ');
    } else if (target.dataset.entity === 'multi') {
      const cur = target.value.split(/\s+/).filter(Boolean);
      if (!cur.includes(id)) cur.push(id);
      target.value = cur.join('\n');
    } else {
      target.value = id;
    }
    target.dispatchEvent(new Event('change'));
    dlg.close();
  });
  $('[data-q]').addEventListener('input', draw);
  $('[data-unit]').addEventListener('change', draw);
  $('[data-stat]').addEventListener('change', draw);
  $('[data-refresh]').addEventListener('click', () => load(true));
  $('[data-close]').addEventListener('click', () => dlg.close());

  fields.forEach(f => {
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'sec pick-btn'; btn.textContent = 'Aus HA wählen';
    btn.addEventListener('click', () => {
      target = f;
      $('[data-unit]').value = f.dataset.unit ?? 'energy';
      $('[data-q]').value = '';
      dlg.showModal();
      if (!cache || cache.error) load(false); else draw();
      $('[data-q]').focus();
    });
    f.insertAdjacentElement('afterend', btn);
  });
})();
