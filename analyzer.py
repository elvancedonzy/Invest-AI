import os, glob, json, anthropic, re, requests
from datetime import datetime

REPORTS_DIR = "/reports/"
RESULTS_DIR = "/reports/results/"
os.makedirs(RESULTS_DIR, exist_ok=True)

def _date_key(f):
    m = re.search(r'(\d+)-(\d+)-(\d{4})', os.path.basename(f))
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)

def _name_cleanliness(f):
    name = os.path.basename(f).lower()
    score = 0
    for bad in (" copy", "(1)", "(2)", "(3)", " ready", " txt.txt"):
        if bad in name:
            score += 1
    return (score, len(name))

def get_all_reports():
    files = glob.glob(os.path.join(REPORTS_DIR, "*.txt"))
    dated = [(f, _date_key(f)) for f in files if _date_key(f) != (0, 0, 0)]
    by_date = {}
    for f, dk in dated:
        if dk not in by_date or _name_cleanliness(f) < _name_cleanliness(by_date[dk]):
            by_date[dk] = f
    sorted_files = sorted(by_date.values(), key=_date_key)
    reports = []
    for f in sorted_files:
        with open(f, "r", errors="ignore") as fp:
            reports.append({"name": os.path.basename(f), "content": fp.read()})
    return reports

def build_history_context(reports):
    if not reports or len(reports) <= 1:
        return ""
    historical = reports[:-1]
    if len(historical) <= 10:
        return "\n".join(f"=== {r['name']} ===\n{r['content'][:800]}" for r in historical)
    recent  = historical[-10:]
    older   = historical[:-10]
    step    = max(1, len(older) // 20)
    sampled = older[::step]
    recent_text = "\n\n".join(f"=== {r['name']} ===\n{r['content'][:800]}" for r in recent)
    older_text  = "\n".join(f"• {r['name']}: {r['content'][:150].strip()}" for r in sampled)
    return (
        f"RECENT REPORTS — last 10 full:\n{recent_text}\n\n"
        f"EARLIER REPORTS — sampled ({len(sampled)} of {len(older)}):\n{older_text}"
    )

def get_latest_report(reports):
    if not reports:
        return None, None
    latest = reports[-1]
    return latest["name"], latest["content"]

def analyze_with_claude(reports):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    history_summary = build_history_context(reports)
    latest_name, latest_content = get_latest_report(reports)

    # Stable: all historical reports — cache this (changes once/day at most)
    stable_block = f"HISTORICAL CONTEXT ({len(reports)-1} reports from archive):\n{history_summary}"

    # Volatile: today's report + instructions — new every run, not cached
    volatile_block = (
        f"TODAY'S REPORT ({latest_name}):\n{latest_content}\n\n"
        "Analyze today's report with full historical context and provide:\n\n"
        "1. MARKET SENTIMENT: Bull/bear score, how has it changed over the past week?\n\n"
        "2. KEY ECONOMIC DATA: Important data points from today\n\n"
        "3. PATTERN ANALYSIS: What themes has Kevin been consistently bullish/bearish on over the past MONTHS (use full archive)? What changed today?\n\n"
        "4. SHORT TERM PLAYS (1-2 days): Specific actionable ideas with entry context\n\n"
        "5. MEDIUM TERM PLAYS (weeks): Stocks/sectors with historical conviction tracking\n\n"
        "6. LONG TERM HOLDS (10 years): Top picks with how long Kevin has held conviction\n\n"
        "7. STOCKS TO AVOID: Bearish calls with historical context\n\n"
        "8. OPTIONS STRATEGY: Current vol environment and specific plays\n\n"
        "9. ACCURACY CHECK: Based on past reports, which of Kevin's recent calls were right or wrong?\n\n"
        "10. TOP 3 ACTIONS: The 3 most important things to act on TODAY\n\n"
        "Be specific, actionable, and use the historical data to show conviction strength.\n"
        "Not personalized financial advice."
    )

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
        system=[{
            "type": "text",
            "text": "You are an expert investment research analyst with access to months of Alpha Reports from Kevin.",
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": stable_block,   "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": volatile_block},
        ]}],
    )
    return r.content[0].text

def main():
    print(f"Starting analysis at {datetime.now()}")
    reports = get_all_reports()
    if not reports:
        print("No reports found")
        return

    latest_name, _ = get_latest_report(reports)
    print(f"Found {len(reports)} reports. Latest: {latest_name}")

    base = latest_name.replace(".txt", "")
    out = os.path.join(RESULTS_DIR, f"{base}_analysis.txt")

    if os.path.exists(out):
        print(f"Already analyzed: {base}")
        return

    print(f"Analyzing with {len(reports)} reports of historical context...")
    result = analyze_with_claude(reports)

    with open(out, "w") as f:
        f.write(f"Report: {latest_name}\n")
        f.write(f"Reports used for context: {len(reports)}\n")
        f.write(f"Analyzed: {datetime.now()}\n")
        f.write("="*60 + "\n\n")
        f.write(result)

    print(result)
    print(f"\nSaved to: {out}")
    notify_home_assistant(latest_name)

def notify_home_assistant(report_name):
    ha_url   = os.getenv("HA_URL", "").rstrip("/")
    ha_token = os.getenv("HA_TOKEN", "")
    ha_svc   = os.getenv("HA_NOTIFY_SERVICE", "notify")
    if not ha_url or not ha_token:
        print("HA_URL / HA_TOKEN not set — skipping notification")
        return
    try:
        base = report_name.replace(".txt", "").replace("Alpha Report ", "")
        payload = {
            "title": "Invest AI — New Analysis Ready",
            "message": f"Alpha Report {base} analyzed. Open dashboard to see Kevin's breakdown + Top 3 actions.",
        }
        r = requests.post(
            f"{ha_url}/api/services/notify/{ha_svc}",
            headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        if r.status_code == 200:
            print(f"Home Assistant notification sent ({ha_svc})")
        else:
            print(f"HA notify returned {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"HA notification failed: {e}")

main()
