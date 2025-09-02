(() => {
  const bodyEl = document.getElementById('lb-body');
  const updatedEl = document.getElementById('lb-updated');
  const tabs = document.querySelectorAll('.tab[data-range]');

  async function load(range) {
    try {
      const r = await fetch(`/leaderboard-${range}.json?ts=${Date.now()}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      const data = await r.json();
      updatedEl.textContent = data.updatedAt || '—';
      render(data.players || []);
    } catch (e) {
      updatedEl.textContent = 'error';
      bodyEl.innerHTML = `<tr><td colspan="6" class="empty">Failed to load leaderboard (${e})</td></tr>`;
    }
  }

  function render(players) {
    if (!players.length) {
      bodyEl.innerHTML = `<tr><td colspan="6" class="empty">No data yet</td></tr>`;
      return;
    }
    bodyEl.innerHTML = players.map((p, idx) => `
      <tr>
        <td>${idx+1}</td>
        <td>${escapeHtml(p.name)}</td>
        <td>${p.raids ?? 0}</td>
        <td>${p.kills ?? 0}</td>
        <td>${p.deaths ?? 0}</td>
        <td>${p.xp ?? 0}</td>
      </tr>`).join('');
  }

  function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

  let current = '24h';
  tabs.forEach(t => t.addEventListener('click', () => {
    tabs.forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    current = t.dataset.range;
    load(current);
  }));

  // initial + refresh loop
  load(current);
  setInterval(() => load(current), 60_000);
})();
