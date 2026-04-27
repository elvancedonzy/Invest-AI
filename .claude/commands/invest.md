# /invest — Invest AI Quick Reference

This skill loads full context for the Invest AI project so you can immediately build, debug, or extend without re-explaining the project.

## When invoked, Claude should:

1. Confirm you're working in `C:\Users\elvis\Documents\invest-ai\` (or the current repo checkout)
2. Remind the user of the two core files: `main.py` (FastAPI dashboard) and `analyzer.py` (CronJob analysis)
3. Show the deploy commands needed to push changes to Kubernetes
4. Ask what they want to build or fix today

## Deploy Workflow
```bash
# Push main.py changes to live dashboard
kubectl create configmap api-script --from-file=main.py=./main.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/invest-ai-api -n invest-ai

# Push analyzer.py changes
kubectl create configmap analyzer-script --from-file=analyzer.py=./analyzer.py -n invest-ai --dry-run=client -o yaml | kubectl apply -f -

# Watch rollout
kubectl rollout status deployment/invest-ai-api -n invest-ai

# Check logs
kubectl logs deployment/invest-ai-api -n invest-ai --tail=50 -f
```

## Quick Checks
```bash
kubectl get all -n invest-ai
curl http://192.168.1.201:30080/health
curl http://192.168.1.201:30080/debug
```

## Architecture Reminder
- `main.py` → FastAPI app → served at port 30080 via NodePort
- `analyzer.py` → CronJob runs every 10 min → writes to `/reports/results/`
- SQLite at `/reports/users.db` (on Synology NFS, persists across restarts)
- All API keys live in `invest-ai-secrets` K8s Secret (never in code or git)

## Active APIs
| API | Used For | Key Prefix |
|-----|----------|-----------|
| Anthropic | Claude analysis + Q&A | sk-ant-... |
| Alpaca | Live prices + OHLCV | PKV... |
| Tradier | Options chain (sandbox) | W9g... |
| Polygon | News + Earnings | ZEv... |
| Home Assistant | Push notifications | eyJ... |

## What's Next (Phase 3)
- Tradier live account for real options data
- Backfill track_record.txt from 91 reports using Claude batch
- RSI/MACD historical weekend scans
- Earnings whisper integration for accurate dates
