# Fika XP — Static Site

This package is ready to deploy to **/var/www/html**.

## Files
- `index.html` — Leaderboard (reads `/leaderboard-24h.json`, `/leaderboard-7d.json`, `/leaderboard-all.json`)
- `quests.html` — Quests (login with Discord, accept/claim/discard)
- `assets/css/site.css` — Shared styles
- `assets/js/api.js` — API helper. Default `API_BASE='/api'`. If you proxy under `/xp/`, change it to `'/xp/api'`.
- `assets/js/leaderboard.js` — Leaderboard renderer
- `assets/js/quests.js` — Quests UI
- `assets/img/*` — Icons

## Back-end expectations
- `GET /api/me` returns `{ authenticated: bool, user: { id, name, avatar } }`
- `GET /api/quests?scope=daily|weekly|all` returns an array of quests:
  `{ id, title, descr, scope, goal, progress, xp, accepted, claimed, metric }`
- `POST /api/quests/accept` with `{id}` (optionally `{claim:true}` or `{discard:true}` as fallback)
- Optional: `POST /api/quests/claim` and `/api/quests/discard`
- `GET /api/login?redirect=/quests.html` and `/api/logout?redirect=/quests.html`

## Deploy
```
sudo mkdir -p /var/www/html
sudo cp -r ./fika_site/* /var/www/html/
sudo chown -R ubuntu:www-data /var/www/html
sudo find /var/www/html -type d -exec chmod 775 {} \;
sudo find /var/www/html -type f -exec chmod 664 {} \;
```