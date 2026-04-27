# Invest AI — Project Context for Claude

## What This Is
A fully automated AI-powered investment research platform. It reads daily Alpha Reports from Meet Kevin's Discord, analyzes them with Claude AI (91 reports of historical context), pulls live stock prices, and serves a mobile-friendly dashboard. Runs on a self-hosted K3s Kubernetes cluster on a Synology NAS.

## Project Location
`C:\Users\elvis\Downloads\Invest AI\` — canonical working directory. Always work from here.

## Key Files
| File | Purpose |
|------|---------|
| `main.py` | FastAPI dashboard app (~106KB) — deployed as `api-script` ConfigMap |
| `analyzer.py` | Background analysis CronJob script — deployed as `analyzer-script` ConfigMap |
| `invest-ai-api.yaml` | Deployment + Service YAML |
| `alpha-analyzer-cronjob.yaml` | CronJob YAML (runs every 10 min) |
| `alpha-reports-storage.yaml` | PersistentVolume + PVC |
| `secrets.yaml` | ⚠️ PLAINTEXT API KEYS — NEVER commit to git |

## Infrastructure
- **K3s VM:** `192.168.1.201` (SSH user: `oem`)
- **Synology NAS:** `192.168.1.224` (static IP)
- **NFS share:** `/volume4/CE UNION/alpha-reports` → VM `/mnt/alpha-reports`
- **Kubernetes namespace:** `invest-ai`
- **Dashboard:** `http://192.168.1.201:30080`
- **Grafana:** `http://192.168.1.201:3000`

## Kubernetes Resources (namespace: invest-ai)
- **Secret:** `invest-ai-secrets` (ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY, TRADIER_TOKEN, POLYGON_API_KEY, DISCORD_TOKEN, DISCORD_CHANNEL_ID, HA_URL, HA_TOKEN, HA_NOTIFY_SERVICE)
- **ConfigMaps:** `api-script` (main.py), `analyzer-script` (analyzer.py)
- **Deployment:** `invest-ai-api` — `python:3.11-slim`, port 8000, startup installs fastapi uvicorn anthropic requests
- **Service:** `invest-ai-service` — NodePort 30080
- **CronJob:** `alpha-analyzer` — every 10 min, uses Haiku for cost savings
- **CronJob:** `discord-fetcher` — 9:30 AM Mon–Fri
- **PV/PVC:** `alpha-reports-pv` / `alpha-reports-pvc` — hostPath `/mnt/alpha-reports`

## Models Used
- **Interactive /ask:** `claude-sonnet-4-6` (best quality)
- **Background analysis CronJob:** `claude-haiku-4-5-20251001` (67% cheaper)
- **Prompt caching:** System prompt + 91-report history block cached → ~65% cost reduction on back-to-back questions

## Dashboard Features Built (all 11)
1. Live Prices + Session Badge (PRE/OPEN/AH/CLOSED in Eastern Time)
2. Kevin's Track Record log (from `track_record.txt` on NFS)
3. Earnings Calendar (Polygon API, warns within 7/21 days)
4. Position Sizing Calculator (with options contracts + Fill button)
5. Watchlist & Live P&L (SQLite `users.db`, per-user profiles)
6. Ask Claude (91 reports + prices + track record context, quick chips)
7. Latest Analysis (10-section breakdown, auto-refresh every 5 min)
8. RSI & MACD Indicators (60 daily bars from Alpaca, client-side calc)
9. Options Chain (Tradier sandbox — prices simulated, not live)
10. Ticker News (Polygon API, last 5 headlines)
11. Multi-User Profiles (Netflix-style picker, 24 emoji avatars, per-profile SQLite)

## Deploy Commands (run from this PC or any machine with kubectl access)
```bash
# Deploy updated main.py
kubectl create configmap api-script --from-file=main.py=./main.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/invest-ai-api -n invest-ai

# Deploy updated analyzer.py
kubectl create configmap analyzer-script --from-file=analyzer.py=./analyzer.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -

# Trigger manual analysis
kubectl create job manual-$(date +%s) --from=cronjob/alpha-analyzer -n invest-ai

# Check everything
kubectl get all -n invest-ai

# Remount NFS if dropped
ssh oem@192.168.1.201 "sudo mount -t nfs 192.168.1.224:'/volume4/CE UNION/alpha-reports' /mnt/alpha-reports"
```

## Known Issues
1. **Tradier token is sandbox** — options prices simulated. Upgrade to Tradier brokerage for live data.
2. **track_record.txt** — maintained manually. Add a line each time Kevin makes a call.
3. **Earnings dates are estimates** — calculated as period-end + ~45 days. Always verify before trading.
4. **secrets.yaml** — never commit. If accidentally pushed, rotate all API keys immediately.

## Planned Next Steps
- Phase 3: Tradier live account, track_record backfill from 91 reports, RSI/MACD weekend history
- Phase 4: Grafana portfolio panels, Home Assistant dashboard card, earnings notifications
- Phase 5: HTTPS/SSL, CI/CD auto-deploy on git push

## Cost
Monthly Claude API: ~$2–3 (prompt caching + Haiku for batch jobs)

## Alpha Report Workflow
1. Kevin posts in Discord ~9:22 AM
2. Download .txt → upload via Synology Drive app to `CE UNION/alpha-reports/`
3. Click "▶ Run Now" or wait up to 10 min for CronJob
4. Home Assistant push notification fires on iPhone
5. Open dashboard → full 10-section analysis ready
