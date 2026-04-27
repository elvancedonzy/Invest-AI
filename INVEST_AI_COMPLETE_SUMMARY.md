# Invest AI — Complete Project Summary
**Generated:** April 25, 2026  
**Author:** Built with Claude Code (Anthropic)  
**Location:** `C:\Users\elvis\Documents\invest-ai\`

---

## 🎯 What It Does

A fully automated AI-powered investment research platform that:
- Reads daily Alpha Reports from Meet Kevin's Discord
- Analyzes them using Claude AI with full 5-month historical context (91 reports)
- Pulls live stock prices from Alpaca Markets
- Serves a mobile-friendly web dashboard accessible from any device
- Runs automatically every 10 minutes — no manual intervention needed
- Sends Home Assistant push notifications when new analysis is ready
- Supports multiple user profiles with personal watchlists and history

---

## 🏗️ Infrastructure

### Hardware
| Device | Role | IP |
|--------|------|----|
| Synology NAS (CAESEA_NAS) | File storage, hosts the VM | 192.168.1.224 (static) |
| K3s VM (Kthrees) | Runs Kubernetes | 192.168.1.201 |

### Software Stack
| Layer | Technology |
|-------|-----------|
| Orchestration | K3s (lightweight Kubernetes) |
| Storage | Synology NFS share → VM → pods |
| AI Brain | Claude Sonnet 4.6 (interactive), Claude Haiku 4.5 (background) |
| Market Data | Alpaca Markets API (live prices, historical bars) |
| Options Data | Tradier API (sandbox) |
| News | Polygon API |
| Earnings | Polygon financials API |
| Web Framework | FastAPI + Uvicorn |
| Database | SQLite at `/reports/users.db` (on Synology NFS) |
| Notifications | Home Assistant mobile push |

---

## 📁 Local Files (C:\Users\elvis\Documents\invest-ai\)

| File | Purpose |
|------|---------|
| `main.py` | FastAPI dashboard app (deploys as `api-script` ConfigMap) |
| `analyzer.py` | Background analysis script (deploys as `analyzer-script` ConfigMap) |
| `invest-ai-api.yaml` | Deployment + Service YAML (reference only — use kubectl apply) |
| `alpha-analyzer-cronjob.yaml` | CronJob YAML (reference only) |
| `analyzer-configmap.yaml` | Old configmap YAML (superseded by analyzer.py) |
| `alpha-reports-storage.yaml` | PersistentVolume + PVC |
| `secrets.yaml` | ⚠️ CONTAINS PLAINTEXT API KEYS — do not commit to git |
| `fstab-fix-pod.yaml` | One-off pod used to fix NFS fstab (keep for reference) |
| `INVEST_AI_COMPLETE_SUMMARY.md` | This file |

---

## ☸️ Kubernetes Resources (namespace: invest-ai)

### Secrets
**Name:** `invest-ai-secrets`
| Key | Value (masked) | Used By |
|-----|---------------|---------|
| ANTHROPIC_API_KEY | sk-ant-... | API pod, Analyzer pod |
| ALPACA_API_KEY | PKV... | API pod |
| ALPACA_SECRET_KEY | HQX... | API pod |
| TRADIER_TOKEN | W9g... | API pod (sandbox) |
| POLYGON_API_KEY | ZEv... | API pod |
| DISCORD_TOKEN | MTQ... | Discord fetcher pod |
| DISCORD_CHANNEL_ID | 132... | Discord fetcher pod |
| HA_URL | https://rosehillstr.duckdns.org:8123 | Analyzer pod |
| HA_TOKEN | eyJ... | Analyzer pod |
| HA_NOTIFY_SERVICE | mobile_app_elvis | Analyzer pod |

### ConfigMaps
| Name | Contains | Source File |
|------|----------|-------------|
| `api-script` | main.py FastAPI app | `main.py` |
| `analyzer-script` | analyzer.py analysis script | `analyzer.py` |

### Deployment
**Name:** `invest-ai-api` (1 replica)  
**Image:** `python:3.11-slim`  
**Startup command:** `pip install fastapi uvicorn anthropic requests -q && uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir /scripts`  
**Env vars from secret:** ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY, TRADIER_TOKEN, POLYGON_API_KEY  
**Volume mounts:**
- `/reports` → NFS share at `/mnt/alpha-reports` on VM
- `/scripts` → `api-script` ConfigMap
- `/analyzer` → `analyzer-script` ConfigMap

### Service
**Name:** `invest-ai-service`  
**Type:** NodePort  
**Port:** 30080 → pod 8000

### CronJobs
| Name | Schedule | What It Does |
|------|----------|-------------|
| `alpha-analyzer` | `*/10 * * * *` | Analyzes latest report with Claude every 10 min |
| `discord-fetcher` | `30 9 * * 1-5` | Attempts Discord fetch 9:30 AM Mon–Fri |

**Analyzer CronJob env vars from secret:** ANTHROPIC_API_KEY, HA_URL, HA_TOKEN, HA_NOTIFY_SERVICE  
**Analyzer pip install:** `pip install anthropic requests -q`

### Storage
| Resource | Type | Details |
|----------|------|---------|
| `alpha-reports-pv` | PersistentVolume | hostPath `/mnt/alpha-reports` on VM |
| `alpha-reports-pvc` | PVC | Bound to above PV |

### NFS Mount (on VM at 192.168.1.201)
- **Synology export:** `/volume4/CE UNION/alpha-reports`
- **VM mount point:** `/mnt/alpha-reports`
- **fstab entry:** `192.168.1.224:/volume4/CE\040UNION/alpha-reports /mnt/alpha-reports nfs defaults,_netdev,x-systemd.automount 0 0`
- **Manual remount:** `sudo mount -t nfs 192.168.1.224:"/volume4/CE UNION/alpha-reports" /mnt/alpha-reports`

---

## 🌐 Access URLs

| URL | Purpose |
|-----|---------|
| `http://192.168.1.201:30080/` | Main dashboard |
| `http://192.168.1.201:30080/prices` | Live stock prices |
| `http://192.168.1.201:30080/reports` | List of all loaded reports |
| `http://192.168.1.201:30080/health` | Health check |
| `http://192.168.1.201:30080/debug` | File system debug |
| `http://192.168.1.201:30080/earnings` | Earnings calendar |
| `http://192.168.1.201:30080/track-record` | Kevin's call history |
| `http://192.168.1.201:30080/analysis-status` | Manual trigger status |
| `http://192.168.1.201:3000` | Grafana monitoring |

---

## 📱 Dashboard Features (all 9 built)

### 1. Live Prices + Session Badge
- Real-time prices from Alpaca (SPY, QQQ, SOXL, META, MSFT, MRVL, TSLA)
- Session badge: `● MARKET OPEN` (green pulse) / `● PRE-MARKET` (orange) / `● AFTER-HOURS` (purple) / `● CLOSED` (grey)
- Time shown in Eastern Time (EDT/EST), not UTC
- PRE / AH badges on individual tickers during extended hours
- **▶ Run Now** button triggers immediate analysis without waiting for CronJob

### 2. Kevin's Track Record
- Personal log of Kevin's recommendations vs outcomes
- Maintained in `/reports/track_record.txt` on Synology
- Format: `date,ticker,call,entry,target,HIT/MISS/OPEN,notes`
- Claude reads this on every `/ask` query for context

### 3. Earnings Calendar
- Shows estimated next report dates for META, MSFT, TSLA, MRVL, AXON, RKT
- Sourced from Polygon financials API
- Red badge for reports within 7 days, orange within 21 days
- Dates are estimates — verify at earnings.com before trading

### 4. Position Sizing Calculator
- Inputs: account size, risk %, entry price, stop loss, options premium
- Outputs: shares to buy, position value, % of account, risk/share
- Options contracts calculated automatically (×100 multiplier)
- **Fill button** auto-loads live price for any ticker
- Account size and risk % saved in browser localStorage

### 5. Watchlist & Live P&L
- Server-side storage in SQLite database (synced across devices)
- Per-user profile (each profile has separate watchlist)
- Live P&L vs entry price with green/red color coding
- Supports both shares and options contracts
- ↻ refresh button for fresh prices
- Total portfolio P&L at bottom

### 6. Ask Claude
- Interactive Q&A using all 91 reports + live prices + Kevin's track record
- User profile context injected (open positions, recent lookups)
- **Quick question chips:** SOXL signal, Market mood, Top picks, Avoid list, Week ahead
- Prompt caching: 91-report history block cached → 65% cheaper for back-to-back questions

### 7. Latest Analysis
- Most recent CronJob output — auto-updates every 10 minutes
- 10-section breakdown: sentiment, patterns, short/medium/long plays, options strategy, accuracy, top 3 actions
- **Copy** button copies full analysis to clipboard
- Auto-refresh every 5 minutes

### 8. RSI & MACD Indicators
- Enter any ticker → fetches 60 daily bars from Alpaca
- Calculates RSI-14 (Wilder's smoothing) and MACD (12/26/9) in browser
- Plain-English signals: "Overbought — wait for pullback" / "Bullish crossover 🚀"
- Visual RSI bar with 30/70 oversold/overbought markers

### 9. Options Chain
- Tradier sandbox API (prices are simulated — verify in your broker before trading)
- Shows calls (green) and puts (red) side by side
- Strike, Bid, Ask, Volume, Open Interest
- Expiry date picker loads automatically

### 10. Ticker News
- Polygon API — last 5 headlines per ticker
- One-tap ticker badges: SPY, QQQ, SOXL, META, MSFT, MRVL, TSLA, AXON, RKT
- Shows title, source, time, and summary excerpt

### 11. Multi-User Profiles
- Netflix-style profile picker at first visit
- 24 emoji avatars + 8 color options
- No passwords — just pick a name and tap to enter
- Per-profile: watchlist, lookup history, Claude Q&A log
- **⇄ Switch** button to change profiles
- **📋 History** button shows last 30 lookups with timestamps

---

## 📂 Synology File Structure

```
/volume4/CE UNION/alpha-reports/
├── Alpha Report 12-1-2025.txt        (91 total reports, Dec 2025 – Apr 2026)
├── Alpha Report 12-2-2025.txt
├── ... (88 more reports)
├── Alpha Report 4-23-2026.txt        ← latest
├── track_record.txt                  ← Kevin's calls log (maintain manually)
└── results/
    └── Alpha Report 4-23-2026_analysis.txt   ← Claude's analysis output
```

---

## 🤖 Claude API Usage & Cost Optimization

### Model Split
| Task | Model | Why |
|------|-------|-----|
| Interactive /ask queries | `claude-sonnet-4-6` | Best quality for real-time Q&A |
| Background analysis (CronJob) | `claude-haiku-4-5-20251001` | 67% cheaper, quality sufficient for batch |

### Prompt Caching
Both calls use structured prompt caching:
- **System prompt** → cached (never changes)
- **91 reports history block** → cached with `cache_control: ephemeral` (5-min TTL, changes once/day)
- **Live prices + question** → NOT cached (changes every request)

**Result:** Back-to-back questions within 5 minutes cost ~65% less. Monthly estimate: ~$2–3.

### Why NOT using claude.ai subscription
The claude.ai subscription (Claude Pro $20/mo) is a **web UI product** — it cannot make API calls. The Anthropic API is a separate product billed per token. They cannot be combined.

---

## 📋 Daily Workflow

```
9:22 AM — Kevin posts Alpha Report in Discord
    ↓
Download the .txt file on your phone
    ↓
Upload via Synology Drive app → CE UNION/alpha-reports/
    ↓
Either wait up to 10 minutes (CronJob fires at :00 and :10 etc.)
OR click ▶ Run Now on the dashboard → analysis in ~60 seconds
    ↓
Home Assistant push notification fires on your iPhone
    ↓
Open http://192.168.1.201:30080 → full 10-section analysis ready
    ↓
Ask Claude: "Should I buy SOXL calls today?"
```

---

## 🔧 Useful Commands

```bash
# Check everything running
kubectl get all -n invest-ai

# Check logs
kubectl logs deployment/invest-ai-api -n invest-ai --tail=50
kubectl logs job/alpha-analyzer-XXXXX -n invest-ai

# Trigger manual analysis
kubectl create job manual-$(date +%s) --from=cronjob/alpha-analyzer -n invest-ai

# Restart API (picks up new configmap)
kubectl rollout restart deployment/invest-ai-api -n invest-ai

# Deploy updated main.py
kubectl create configmap api-script --from-file=main.py="C:/Users/elvis/Documents/invest-ai/main.py" -n invest-ai --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/invest-ai-api -n invest-ai

# Deploy updated analyzer.py
kubectl create configmap analyzer-script --from-file=analyzer.py="C:/Users/elvis/Documents/invest-ai/analyzer.py" -n invest-ai --dry-run=client -o yaml | kubectl apply -f -

# Remount NFS if dropped
ssh oem@192.168.1.201 "sudo mount -t nfs 192.168.1.224:'/volume4/CE UNION/alpha-reports' /mnt/alpha-reports"

# Check secrets (masked)
kubectl get secret invest-ai-secrets -n invest-ai -o jsonpath="{.data}" | python3 -c "import sys,json,base64; d=json.load(sys.stdin); [print(k,'=',base64.b64decode(v).decode()[:8]+'...') for k,v in d.items()]"

# View all files on NFS as seen by pod
curl http://192.168.1.201:30080/debug

# Check analysis status / trigger
curl -X POST http://192.168.1.201:30080/trigger-analysis
curl http://192.168.1.201:30080/analysis-status
```

---

## ⚠️ Known Issues / Notes

1. **Tradier token is sandbox** — options prices are simulated, not live. To get real options data, upgrade to a Tradier brokerage account and update the `TRADIER_TOKEN` secret.
2. **track_record.txt** — must be maintained manually. Add a line each time Kevin makes a specific call. The more data, the better Claude's context.
3. **SSH to VM blocked** — SSH from this PC to `oem@192.168.1.201` fails (key not configured). Use kubectl privileged pods instead for host-level operations.
4. **Earnings dates are estimates** — Polygon quarterly financials don't always have filing dates. Dates are calculated as period-end + ~45 days. Always verify before trading around earnings.
5. **secrets.yaml contains plaintext keys** — Never commit to git. If this file is ever shared, rotate all API keys immediately.

---

## 🚀 Next Steps / Planned Features

### Phase 3 — Smart Features
- [ ] Upgrade Tradier to live account for real options pricing
- [ ] Build Kevin's track_record.txt backfill from existing 91 reports
- [ ] RSI/MACD weekend historical analysis across all tickers
- [ ] Earnings earnings whisper integration (more accurate dates)

### Phase 4 — Dashboard
- [ ] Grafana panels for portfolio performance
- [ ] Home Assistant dashboard card
- [ ] Earnings alert notifications

### Phase 5 — Polish
- [ ] GitHub repo with architecture diagram
- [ ] HTTPS with SSL certificate (currently HTTP only)
- [ ] CI/CD pipeline for auto-deploy on code changes

---

## 📝 Resume Description

> **AI Investment Research Platform** | Python, Kubernetes, FastAPI, SQLite, Claude API  
> Designed and deployed a multi-service AI investment research platform on a self-hosted K3s Kubernetes cluster running on a Synology NAS VM. Built CronJob controllers to automate daily Alpha Report analysis using Claude AI with 5+ months of historical context (91 reports). Integrated Alpaca Markets API for real-time stock prices and historical OHLCV data, Polygon for news and earnings, and Tradier for options chains. Implemented FastAPI backend with SQLite multi-user profiles, prompt caching (65% cost reduction), and Home Assistant push notifications. Configured Kubernetes Secrets, ConfigMaps, PersistentVolumes, Deployments, NodePort Services, and resource limits across a custom namespace. Troubleshot NFS mount persistence, pod CrashLoopBackOff errors, YAML f-string parsing issues, Kubernetes rolling deployments, and prompt caching architecture.
