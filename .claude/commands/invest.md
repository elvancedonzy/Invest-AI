# /invest — Invest AI Full Project Context

Invoke this at the start of any session to reload complete project context.

---

## Who / What

Elvis is building a fully automated AI investment research platform on a self-hosted K3s Kubernetes cluster. It reads daily Alpha Reports from Meet Kevin's Discord, analyzes them with Claude AI (91+ reports of historical context), pulls live stock prices, and serves a mobile-friendly dashboard.

---

## Canonical Paths

| Thing | Path |
|-------|------|
| Local project | `C:\Users\elvis\Downloads\Invest AI\` |
| GitHub repo | `https://github.com/elvancedonzy/Invest-AI` (public) |
| Dashboard | `http://192.168.1.201:30080` |
| Grafana | `http://192.168.1.201:3000` |

**Never reference `C:\Users\elvis\Documents\invest-ai\` — that is the old path.**

---

## Infrastructure

| Device | Role | IP |
|--------|------|----|
| Synology NAS (CAESEA_NAS) | File storage, hosts the VM | 192.168.1.224 (static) |
| K3s VM (Kthrees) | Runs Kubernetes | 192.168.1.201 |

- SSH to VM: `oem@192.168.1.201` (key not configured from this PC — use kubectl privileged pods instead)
- NFS: Synology `/volume4/CE UNION/alpha-reports` → VM `/mnt/alpha-reports` → pods at `/reports`
- fstab entry: `192.168.1.224:/volume4/CE\040UNION/alpha-reports /mnt/alpha-reports nfs defaults,_netdev,x-systemd.automount 0 0`

---

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI dashboard — deployed as `api-script` ConfigMap |
| `analyzer.py` | Background CronJob script — deployed as `analyzer-script` ConfigMap |
| `backfill.py` | One-time track_record backfill script — deployed as `backfill-script` ConfigMap |
| `autodeploy.yaml` | CI/CD CronJob — polls GitHub every 5 min, auto-deploys on new commits |
| `grafana-dashboard.json` | Import into Grafana at port 3000 |
| `home-assistant-card.yaml` | HA Lovelace card + REST sensor examples |
| `secrets.yaml` | ⚠️ PLAINTEXT API KEYS — NEVER commit to git (gitignored) |
| `CLAUDE.md` | Human-readable project summary |

---

## Kubernetes (namespace: invest-ai)

**Secret:** `invest-ai-secrets`
| Key | Prefix | Used By |
|-----|--------|---------|
| ANTHROPIC_API_KEY | sk-ant-... | API pod + Analyzer |
| ALPACA_API_KEY | PKV... | API pod |
| ALPACA_SECRET_KEY | HQX... | API pod |
| TRADIER_TOKEN | W9g... | API pod (sandbox) |
| POLYGON_API_KEY | ZEv... | API pod |
| DISCORD_TOKEN | MTQ... | Discord fetcher |
| DISCORD_CHANNEL_ID | 132... | Discord fetcher |
| HA_URL | https://rosehillstr.duckdns.org:8123 | Analyzer |
| HA_TOKEN | eyJ... | Analyzer |
| HA_NOTIFY_SERVICE | mobile_app_elvis | Analyzer |
| GITHUB_TOKEN | github_pat_... | autodeploy CronJob (optional — repo is public) |

**Deployments / Jobs:**
| Resource | Details |
|----------|---------|
| `invest-ai-api` | 1 replica, `python:3.11-slim`, port 8000 → NodePort 30080 |
| `alpha-analyzer` CronJob | `*/10 * * * *` — Haiku model, writes to `/reports/results/` |
| `autodeploy` CronJob | `*/5 * * * *` — polls GitHub, auto-deploys on new commits |
| `discord-fetcher` CronJob | `30 9 * * 1-5` — 9:30 AM Mon–Fri |

**Storage:**
- `alpha-reports-pv` / `alpha-reports-pvc` — hostPath `/mnt/alpha-reports` on VM

---

## Models & Cost

| Task | Model | Cost |
|------|-------|------|
| Interactive /ask (Quality mode) | `claude-sonnet-4-6` | ~$0.026/question (cache hit) |
| Interactive /ask (Economy mode) | `claude-haiku-4-5-20251001` | ~$0.007/question |
| Background CronJob | `claude-haiku-4-5-20251001` | ~$0.01/analysis |
| AI News Sentiment | `claude-haiku-4-5-20251001` | ~$0.001/ticker query |

Monthly estimate: ~$4-15 depending on Ask Claude usage. Use Economy mode (💰 button in Ask Claude card) for daily queries.

---

## All 19 Dashboard Sections (current state)

1. Live Prices + Session Badge (PRE/OPEN/AH/CLOSED in Eastern Time) + Regime pill
2. Ask Claude (91 reports + prices + track record, quick chips, Quality/Economy toggle)
3. Latest Analysis (10-section breakdown, auto-refresh 5 min, Copy button)
4. Kevin's Track Record (client-side search + tab filters, 320px scrollable)
5. Earnings Calendar (Polygon API, red badge <7 days)
6. Position Sizing Calculator (shares + options, Fill button, localStorage)
7. Ticker News (Polygon, last 5 headlines per ticker)
8. Watchlist & Live P&L (SQLite `users.db`, per-user, green/red P&L)
9. RSI & MACD (60 daily bars from Alpaca, client-side calc)
10. Options Chain (Tradier sandbox — simulated prices)
11. Market Regime (SPY 50/200MA + vol → BULL/NEUTRAL/CHOPPY/BEAR/CRASH)
12. Kevin's Call Backtest (entry→target expected return, hit rate, scrollable table)
13. Correlation & Monte Carlo (SOXL/QQQ rolling Pearson + 1,000 sim signal strength)
14. AI News Sentiment (Claude Haiku scores last 5 headlines -10 to +10 per ticker)
15. RSI Zone Analysis (which RSI level has best hit rate on Kevin's closed calls)
16. Multi-User Profiles (Netflix-style picker, 24 emoji avatars, per-profile SQLite)

Layout: 2-column responsive grid on desktop, 3-col on >1500px, single column on mobile.
Full-width cards: Live Prices, Kevin's Track Record, Market Regime.

---

## ⚠️ Coding Rules — ALWAYS Follow These

### 1. Every new dashboard section MUST have a `?` help button
```html
<h3>Section Title <button class="help-btn" onclick="showHelp('section-key')">?</button></h3>
```
AND a matching entry in `_help_json` inside `home()`:
```python
"section-key": {
    "title": "Human-readable title",
    "what": "What this section shows",
    "how": "How to use it",
    "tip": "Pro tip for traders",
    "links": [{"label": "...", "url": "..."}]
}
```

### 2. Every new section needs a `?` help entry — no exceptions
Even small helper panels (cost toggle, badges, pills) should explain themselves.

### 3. Secondary buttons use `.btn-sm` class (NOT the global `button` style)
Global `button` is full-width. `.btn-sm` is auto-width pill style.
```html
<button class="btn-sm" onclick="...">↻ Refresh</button>
```

### 4. New cards go inside `<div class="dash">` and follow the grid layout
- Single-column (full width): add `class="card full"`
- Half-width (pairs naturally in the 2-col grid): just `class="card"`

### 5. Never use `\'` inside Python f-string JS — it breaks JS string literals
Use `Kevin picks` not `Kevin\'s picks`. The `\'` becomes `'` in output, breaking JS.

### 6. Always deploy AND push together
```bash
kubectl create configmap api-script --from-file=main.py=./main.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/invest-ai-api -n invest-ai
git add main.py && git commit -m "..." && git push
```
CI/CD autodeploy runs every 5 min and will also pick up the push automatically.

---

## Deploy Commands

```bash
# Push main.py to live dashboard
kubectl create configmap api-script --from-file=main.py=./main.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/invest-ai-api -n invest-ai

# Push analyzer.py
kubectl create configmap analyzer-script --from-file=analyzer.py=./analyzer.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -

# Watch rollout
kubectl rollout status deployment/invest-ai-api -n invest-ai

# Check everything
kubectl get all -n invest-ai

# Trigger manual analysis
kubectl create job manual-$(date +%s) --from=cronjob/alpha-analyzer -n invest-ai

# Force immediate autodeploy
kubectl create job autodeploy-now --from=cronjob/autodeploy -n invest-ai

# Logs
kubectl logs deployment/invest-ai-api -n invest-ai --tail=50 -f
kubectl logs -l app=autodeploy -n invest-ai --tail=20

# Remount NFS if dropped
ssh oem@192.168.1.201 "sudo mount -t nfs 192.168.1.224:'/volume4/CE UNION/alpha-reports' /mnt/alpha-reports"
```

---

## Git Workflow (CI/CD handles deploy automatically)

```bash
git add main.py analyzer.py    # or whatever changed
git commit -m "describe what changed"
git push
# autodeploy CronJob picks it up within 5 minutes
```

---

## Daily Alpha Report Workflow

1. Kevin posts in Discord ~9:22 AM
2. Download .txt → upload via Synology Drive app to `CE UNION/alpha-reports/`
3. Click "▶ Run Now" on dashboard OR wait up to 10 min for CronJob
4. Home Assistant push notification fires on iPhone (includes sentiment delta + regime)
5. Open `http://192.168.1.201:30080` → full 10-section analysis ready

---

## Known Issues

1. **Tradier sandbox** — options prices are simulated. Upgrade to Tradier brokerage for live data.
2. **track_record.txt** — maintained manually. More entries = better Backtest + Monte Carlo.
3. **Earnings dates are estimates** — period-end + ~45 days. Verify at earnings.com before trading.
4. **secrets.yaml** — never commit. If pushed accidentally, rotate all API keys immediately.
5. **GITHUB_TOKEN** — token shared in chat must be regenerated at github.com → Settings → Developer Settings → PATs.

---

## Remaining Phases

| Phase | Items |
|-------|-------|
| 5 | HTTPS/SSL via cert-manager + Traefik |
| 5 | Architecture README with diagram |
| 6 | Tradier live brokerage (user action) |
| 6 | Alpaca paper trade execution (auto-buy based on Top 3 Actions) |
| 7 | PWA — add to home screen on iPhone as native app |
