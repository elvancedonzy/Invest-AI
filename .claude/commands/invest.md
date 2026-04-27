# /invest — Invest AI Full Project Context

Invoke this at the start of any session to reload complete project context.

---

## Who / What

Elvis is building a fully automated AI investment research platform on a self-hosted K3s Kubernetes cluster. It reads daily Alpha Reports from Meet Kevin's Discord, analyzes them with Claude AI (91 reports of historical context), pulls live stock prices, and serves a mobile-friendly dashboard.

---

## Canonical Paths

| Thing | Path |
|-------|------|
| Local project | `C:\Users\elvis\Downloads\Invest AI\` |
| GitHub repo | `https://github.com/elvancedonzy/Invest-AI.git` |
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
| `main.py` | FastAPI dashboard (~106KB) — deployed as `api-script` ConfigMap |
| `analyzer.py` | Background CronJob script — deployed as `analyzer-script` ConfigMap |
| `invest-ai-api.yaml` | Deployment + Service YAML |
| `alpha-analyzer-cronjob.yaml` | CronJob YAML (every 10 min) |
| `alpha-reports-storage.yaml` | PV + PVC |
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

**Deployments / Jobs:**
| Resource | Details |
|----------|---------|
| `invest-ai-api` | 1 replica, `python:3.11-slim`, port 8000 → NodePort 30080 |
| `alpha-analyzer` CronJob | `*/10 * * * *` — Haiku model, writes results to `/reports/results/` |
| `discord-fetcher` CronJob | `30 9 * * 1-5` — 9:30 AM Mon–Fri |

**Storage:**
- `alpha-reports-pv` / `alpha-reports-pvc` — hostPath `/mnt/alpha-reports` on VM

---

## Models & Cost

| Task | Model | Why |
|------|-------|-----|
| Interactive /ask | `claude-sonnet-4-6` | Best quality for real-time Q&A |
| Background CronJob | `claude-haiku-4-5-20251001` | 67% cheaper, sufficient for batch |

Prompt caching on system prompt + 91-report history block → ~65% cost reduction on back-to-back questions. Monthly estimate: ~$2–3.

---

## 11 Dashboard Features (all built)

1. Live Prices + Session Badge (PRE/OPEN/AH/CLOSED in Eastern Time)
2. Kevin's Track Record (from `track_record.txt` on NFS — maintain manually)
3. Earnings Calendar (Polygon API, red badge <7 days, orange <21 days — estimates only)
4. Position Sizing Calculator (shares + options, Fill button loads live price, localStorage)
5. Watchlist & Live P&L (SQLite `users.db` on NFS, per-user, green/red P&L)
6. Ask Claude (91 reports + prices + track record, quick chips, prompt caching)
7. Latest Analysis (10-section breakdown, auto-refresh 5 min, Copy button)
8. RSI & MACD (60 daily bars from Alpaca, client-side calc, plain-English signals)
9. Options Chain (Tradier sandbox — simulated prices, not live)
10. Ticker News (Polygon, last 5 headlines per ticker)
11. Multi-User Profiles (Netflix-style picker, 24 emoji avatars, per-profile SQLite history)

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

# Logs
kubectl logs deployment/invest-ai-api -n invest-ai --tail=50 -f

# Remount NFS if dropped
ssh oem@192.168.1.201 "sudo mount -t nfs 192.168.1.224:'/volume4/CE UNION/alpha-reports' /mnt/alpha-reports"

# Check secrets (masked)
kubectl get secret invest-ai-secrets -n invest-ai -o jsonpath="{.data}" | python3 -c "import sys,json,base64; d=json.load(sys.stdin); [print(k,'=',base64.b64decode(v).decode()[:8]+'...') for k,v in d.items()]"
```

---

## Git Workflow

```bash
# After editing main.py or analyzer.py:
git add main.py analyzer.py
git commit -m "describe what changed"
git push

# Then deploy to K8s (main.py):
kubectl create configmap api-script --from-file=main.py=./main.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/invest-ai-api -n invest-ai
```

---

## Daily Alpha Report Workflow

1. Kevin posts in Discord ~9:22 AM
2. Download .txt → upload via Synology Drive app to `CE UNION/alpha-reports/`
3. Click "▶ Run Now" on dashboard OR wait up to 10 min for CronJob
4. Home Assistant push notification fires on iPhone
5. Open `http://192.168.1.201:30080` → full 10-section analysis ready

---

## Known Issues

1. **Tradier sandbox** — options prices are simulated. Upgrade to Tradier brokerage for live data.
2. **track_record.txt** — maintained manually. More data = better Claude context.
3. **Earnings dates are estimates** — period-end + ~45 days. Verify at earnings.com before trading.
4. **secrets.yaml** — never commit. If pushed accidentally, rotate all API keys immediately.

---

## Next Steps (Phase 3)

- [ ] Tradier live brokerage account for real options prices
- [ ] Backfill `track_record.txt` from existing 91 reports using Claude batch
- [ ] RSI/MACD historical scans on weekends
- [ ] Earnings whisper integration for accurate dates
- [ ] Grafana panels for portfolio performance
- [ ] Home Assistant dashboard card
- [ ] HTTPS/SSL certificate
- [ ] CI/CD auto-deploy on `git push`
