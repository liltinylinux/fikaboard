(async function(){
  const tabs = [...document.querySelectorAll('.tab[data-scope]')];
  const list = document.querySelector('#quest-list');
  const scopeLabel = document.querySelector('#scope-label');
  const meSlot = document.querySelector('#user-slot');
  const hero = document.querySelector('#auth-hero');

  async function renderUserBox() {
    const me = await fetchMe();
    if (me.authenticated) {
      meSlot.innerHTML = `<span class="userbox"><img src="${avatarUrl(me.user)}" alt="">${me.user.name}</span>
        <button class="btn" id="logout-btn">Logout</button>`;
      document.querySelector('#logout-btn').onclick = ()=>logout('/quests.html');
      hero.style.display = 'none';
    } else {
      meSlot.innerHTML = `<button class="discord-btn" id="login-btn"><img src="/assets/img/discord.svg" alt="">Continue with Discord</button>`;
      document.querySelector('#login-btn').onclick = ()=>login('/quests.html');
      hero.style.display = 'flex';
    }
  }

  function qCard(q) {
    const pct = Math.max(0, Math.min(100, Math.round((q.progress||0) / (q.goal||1) * 100)));
    const action = q.claimed ? '' :
      q.accepted ? `<button class="btn act-claim" data-id="${q.id}">Claim</button>
                    <button class="btn act-discard" data-id="${q.id}">Discard</button>` :
                   `<button class="btn act-accept" data-id="${q.id}">Accept</button>`;
    const scopeBadge = `<span class="badge">${q.scope||'all'}</span>`;
    return `<div class="card">
      <div class="q-head">
        <div>
          <div class="qtitle">${q.title||'Quest'}</div>
          <div class="badges">
            ${scopeBadge}
            <span class="badge">+${q.xp||0} XP</span>
            <span class="badge">${(q.metric||'progress').replace(/_/g,' ')}</span>
          </div>
        </div>
        <div class="row-right">
          <span class="muted">${q.progress||0}/${q.goal||0}</span>
          ${action}
        </div>
      </div>
      <div class="q-desc">${q.descr||q.desc||''}</div>
      <div class="progress"><div class="bar" style="width:${pct}%"></div></div>
    </div>`;
  }

  async function safePost(url, payload) {
    try { return await apiPost(url, payload); } catch (e) { return {error:String(e)}; }
  }

  async function accept(id) {
    // try canonical first
    let res;
    try { res = await apiPost('/quests/accept', {id}); }
    catch {
      // fallback: overloaded accept
      res = await apiPost('/quests/accept', {id, accept:true});
    }
    return res;
  }
  async function claim(id) {
    try { return await apiPost('/quests/claim', {id}); }
    catch { return await apiPost('/quests/accept', {id, claim:true}); }
  }
  async function discard(id) {
    try { return await apiPost('/quests/discard', {id}); }
    catch { return await apiPost('/quests/accept', {id, discard:true}); }
  }

  async function load(scope) {
    scopeLabel.textContent = scope;
    // if not logged in, show hero and no list fetch
    const me = await fetchMe();
    if (!me.authenticated) { list.innerHTML = ''; return; }

    const data = await apiGet(`/quests?scope=${encodeURIComponent(scope)}`);
    const qs = Array.isArray(data) ? data : (data.quests || []);
    list.innerHTML = qs.length ? qs.map(q=>qCard(q)).join('')
                               : `<div class="card empty">No quests for ${scope}.</div>`;

    list.querySelectorAll('.act-accept').forEach(b=>b.addEventListener('click', async (e)=>{
      const id = b.dataset.id; b.disabled = true;
      await accept(id); await load(scope);
    }));
    list.querySelectorAll('.act-claim').forEach(b=>b.addEventListener('click', async (e)=>{
      const id = b.dataset.id; b.disabled = true;
      await claim(id); await load(scope);
    }));
    list.querySelectorAll('.act-discard').forEach(b=>b.addEventListener('click', async (e)=>{
      const id = b.dataset.id; b.disabled = true;
      await discard(id); await load(scope);
    }));
  }

  tabs.forEach(b=>b.onclick=()=>{
    tabs.forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    load(b.dataset.scope);
  });

  await renderUserBox();
  const active = tabs.find(b=>b.classList.contains('active')) || tabs[0];
  active && load(active.dataset.scope);
})();