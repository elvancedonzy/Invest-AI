from fastapi import FastAPI, HTTPException, Request, Response, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
import anthropic, os, glob, json, re, requests, sqlite3, subprocess, threading
from datetime import datetime, timedelta

# ── Manual analysis trigger state ──────────────────��─────────────────────────
_trigger_lock  = threading.Lock()
_trigger_state = {"running": False, "started": None, "finished": None,
                  "result": None, "error": None}

app = FastAPI()
REPORTS_DIR = "/reports/"
RESULTS_DIR = "/reports/results/"
DB_PATH     = "/reports/users.db"

PROFILE_COLORS = ["#00d4ff","#00ff88","#b388ff","#ff9800","#ff6b6b","#ffd700","#7ec8e3","#ff85a1"]
AVATARS = [
    "📈","💰","🚀","💎","🔥","⚡","🎯","🏆",
    "🦁","🐂","🐻","🦊","🦅","🐉","🤖","👾",
    "🧑‍💻","👨‍💼","👩‍💼","🎩","💫","🌙","🎪","🦈",
]

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with get_db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    UNIQUE NOT NULL,
                color     TEXT    DEFAULT '#00d4ff',
                created_at TEXT   DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                ticker     TEXT    NOT NULL,
                size       REAL    NOT NULL,
                entry      REAL    NOT NULL,
                type       TEXT    DEFAULT 'stock',
                added_at   TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                action     TEXT    NOT NULL,
                ticker     TEXT,
                query      TEXT,
                summary    TEXT,
                created_at TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            );
        """)
        # add avatar column if upgrading from older schema
        try:
            con.execute("ALTER TABLE profiles ADD COLUMN avatar TEXT DEFAULT '📈'")
        except Exception:
            pass
        # seed a default profile if none exist
        row = con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        if row == 0:
            con.execute("INSERT INTO profiles(name,color,avatar) VALUES(?,?,?)",
                        ("Elvis", "#00d4ff", "📈"))

try:
    init_db()
except Exception as _e:
    print("DB init skipped:", _e)

# ── Profile helpers ───────────────────────────────────────────────────────────

def db_get_profiles():
    with get_db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM profiles ORDER BY name").fetchall()]

def db_get_profile(pid):
    with get_db() as con:
        r = con.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

def db_create_profile(name, color, avatar="📈"):
    with get_db() as con:
        con.execute("INSERT INTO profiles(name,color,avatar) VALUES(?,?,?)", (name, color, avatar))

def db_delete_profile(pid):
    with get_db() as con:
        con.execute("DELETE FROM profiles WHERE id=?", (pid,))

# ── Watchlist helpers ─────────────────────────────────────────────────────────

def db_get_watchlist(profile_id):
    with get_db() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM watchlist WHERE profile_id=? ORDER BY added_at DESC", (profile_id,)
        ).fetchall()]

def db_add_watch(profile_id, ticker, size, entry, wtype):
    with get_db() as con:
        con.execute("INSERT INTO watchlist(profile_id,ticker,size,entry,type) VALUES(?,?,?,?,?)",
                    (profile_id, ticker.upper(), size, entry, wtype))

def db_remove_watch(wid, profile_id):
    with get_db() as con:
        con.execute("DELETE FROM watchlist WHERE id=? AND profile_id=?", (wid, profile_id))

# ── History helpers ───────────────────────────────────────────────────────────

def db_log(profile_id, action, ticker=None, query=None, summary=None):
    if not profile_id:
        return
    try:
        with get_db() as con:
            con.execute(
                "INSERT INTO history(profile_id,action,ticker,query,summary) VALUES(?,?,?,?,?)",
                (profile_id, action, ticker, query, (summary or "")[:400])
            )
            # keep last 200 per profile
            con.execute("""DELETE FROM history WHERE profile_id=? AND id NOT IN (
                SELECT id FROM history WHERE profile_id=? ORDER BY id DESC LIMIT 200)""",
                (profile_id, profile_id))
    except Exception:
        pass

def db_get_history(profile_id, limit=30):
    with get_db() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM history WHERE profile_id=? ORDER BY id DESC LIMIT ?",
            (profile_id, limit)
        ).fetchall()]

def db_build_user_context(profile_id):
    if not profile_id:
        return ""
    profile  = db_get_profile(profile_id)
    if not profile:
        return ""
    wl       = db_get_watchlist(profile_id)
    hist     = db_get_history(profile_id, 20)
    wl_text  = "\n".join(
        f"  - {w['ticker']}: {w['size']} {'contracts' if w['type']=='option' else 'shares'} at ${w['entry']}"
        for w in wl
    ) or "  (none)"
    hist_text = "\n".join(
        f"  - {h['created_at'][:10]} {h['action'].upper()}: "
        f"{h['ticker'] or ''} {('— ' + h['query']) if h['query'] else ''} {('→ ' + h['summary']) if h['summary'] else ''}"
        for h in hist
    ) or "  (none)"
    return (
        f"CURRENT USER: {profile['name']}\n\n"
        f"THEIR OPEN POSITIONS:\n{wl_text}\n\n"
        f"THEIR RECENT ACTIVITY (lookups, searches, questions):\n{hist_text}\n\n"
    )

# ── Cookie helpers ────────────────────────────────────────────────────────────

def current_profile_id(request: Request):
    v = request.cookies.get("profile_id", "")
    try:
        return int(v)
    except (ValueError, TypeError):
        return None

def _date_key(f):
    m = re.search(r'(\d+)-(\d+)-(\d{4})', os.path.basename(f))
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)

def _name_cleanliness(f):
    """Lower score = cleaner filename (prefer over duplicates)."""
    name = os.path.basename(f).lower()
    score = 0
    for bad in (" copy", "(1)", "(2)", "(3)", " ready", " txt.txt"):
        if bad in name:
            score += 1
    return (score, len(name))

def get_all_reports():
    files = glob.glob(os.path.join(REPORTS_DIR, "*.txt"))

    # 1. Only keep files that have a valid date in the filename
    dated = [(f, _date_key(f)) for f in files if _date_key(f) != (0, 0, 0)]

    # 2. Deduplicate by date — keep the cleanest filename per date
    by_date = {}
    for f, dk in dated:
        if dk not in by_date or _name_cleanliness(f) < _name_cleanliness(by_date[dk]):
            by_date[dk] = f

    # 3. Sort chronologically
    sorted_files = sorted(by_date.values(), key=_date_key)

    reports = []
    for f in sorted_files:
        with open(f, "r", errors="ignore") as fp:
            reports.append({"name": os.path.basename(f), "content": fp.read()})
    return reports

def build_history_context(reports):
    """
    Build a tiered history context from all available reports.
    - Latest 10 reports: full preview (600 chars each)
    - Older reports: sampled at ~1 per 2 weeks with brief excerpt (150 chars)
    This lets Claude see the full 5-month arc without blowing the prompt budget.
    """
    if not reports or len(reports) <= 1:
        return ""
    historical = reports[:-1]  # everything except today's report
    if len(historical) <= 10:
        return "\n\n".join(
            f"=== {r['name']} ===\n{r['content'][:600]}" for r in historical
        )
    recent = historical[-10:]
    older  = historical[:-10]
    # Sample older reports — roughly 1 per 2 weeks (max 20 samples)
    step = max(1, len(older) // 20)
    sampled = older[::step]
    recent_text = "\n\n".join(
        f"=== {r['name']} ===\n{r['content'][:600]}" for r in recent
    )
    older_text = "\n".join(
        f"• {r['name']}: {r['content'][:150].strip()}" for r in sampled
    )
    return (
        f"RECENT REPORTS — last 10, full context:\n{recent_text}\n\n"
        f"EARLIER REPORTS — sampled summaries ({len(sampled)} of {len(older)}):\n{older_text}"
    )

def get_latest_report():
    reports = get_all_reports()
    if not reports:
        return None, None
    latest = reports[-1]
    return latest["name"], latest["content"]

def get_latest_analysis():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    files = glob.glob(os.path.join(RESULTS_DIR, "*.txt"))
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    with open(latest, "r", errors="ignore") as f:
        return f.read()

def get_live_prices(tickers):
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return {}
    try:
        symbols = ",".join(tickers)
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={symbols}",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=5
        )
        data = r.json()
        result = {}
        for ticker in tickers:
            if ticker in data:
                snap = data[ticker]
                latest_trade = snap.get("latestTrade", {})
                daily_bar = snap.get("dailyBar", {})
                prev_close = snap.get("prevDailyBar", {}).get("c", 0)
                price = latest_trade.get("p", 0)
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
                result[ticker] = {
                    "price": round(price, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": daily_bar.get("v", 0),
                    "high": round(daily_bar.get("h", 0), 2),
                    "low": round(daily_bar.get("l", 0), 2),
                }
        return result
    except Exception as e:
        return {"error": str(e)}

def list_all_files():
    result = []
    for root, dirs, files in os.walk(REPORTS_DIR):
        for f in files:
            result.append(os.path.join(root, f))
    return result

def get_track_record():
    path = os.path.join(REPORTS_DIR, "track_record.txt")
    if not os.path.exists(path):
        return []
    trades = []
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.lower().startswith("date,"):
                    continue
                parts = line.split(",", 6)
                if len(parts) < 3:
                    continue
                trades.append({
                    "date":    parts[0].strip(),
                    "ticker":  parts[1].strip(),
                    "call":    parts[2].strip(),
                    "entry":   parts[3].strip() if len(parts) > 3 else "",
                    "target":  parts[4].strip() if len(parts) > 4 else "",
                    "outcome": parts[5].strip().upper() if len(parts) > 5 else "OPEN",
                    "notes":   parts[6].strip() if len(parts) > 6 else "",
                })
    except Exception:
        pass
    return trades

def build_track_record_html(trades):
    if not trades:
        return (
            '<div style="color:#8b949e;font-size:13px;padding:10px 0">'
            'No trades recorded yet. Create <b style="color:#e6edf3">track_record.txt</b> '
            'in your alpha-reports folder on Synology.<br><br>'
            '<span style="font-family:monospace;font-size:11px;color:#8b949e">'
            'Format: date,ticker,call,entry,target,outcome,notes<br>'
            'Example: 2026-04-07,SOXL,BUY CALLS,22.50,27.00,HIT,+18% by 4-14</span>'
            '</div>'
        )
    closed = [t for t in trades if t["outcome"] in ("HIT", "MISS")]
    hits   = [t for t in closed if t["outcome"] == "HIT"]
    opens  = [t for t in trades  if t["outcome"] == "OPEN"]
    hit_rate = round(len(hits) / len(closed) * 100) if closed else 0

    oc_color = {"HIT": "#00ff88", "MISS": "#ff6b6b", "OPEN": "#ffd700"}
    oc_icon  = {"HIT": "✅", "MISS": "❌", "OPEN": "⏳"}

    html = (
        '<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:12px">'
        '<span class="meta">Tracked: <b style="color:#e6edf3">' + str(len(trades)) + '</b></span>'
        '<span class="meta">Hit rate: <b style="color:#00ff88">' + str(hit_rate) + '%</b>'
        ' <span style="color:#8b949e">(' + str(len(hits)) + '/' + str(len(closed)) + ' closed)</span></span>'
        '<span class="meta">Open: <b style="color:#ffd700">' + str(len(opens)) + '</b></span>'
        '</div>'
        '<div class="scroll-x">'
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<tr>'
        + "".join(
            '<th style="text-align:' + align + ';padding:4px 8px;color:#8b949e;border-bottom:1px solid #30363d">' + h + '</th>'
            for h, align in [("Date","left"),("Ticker","left"),("Call","left"),
                              ("Entry","right"),("Target","right"),("Result","center"),("Notes","left")]
        ) +
        '</tr>'
    )
    for t in reversed(trades):
        oc    = t["outcome"]
        color = oc_color.get(oc, "#8b949e")
        icon  = oc_icon.get(oc, "")
        html += (
            '<tr>'
            '<td style="padding:4px 8px;color:#8b949e">'           + t["date"]              + '</td>'
            '<td style="padding:4px 8px;font-weight:bold;color:#00d4ff">' + t["ticker"]     + '</td>'
            '<td style="padding:4px 8px;color:#c9d1d9">'            + t["call"]              + '</td>'
            '<td style="padding:4px 8px;text-align:right;color:#c9d1d9">' + (t["entry"]  or "-") + '</td>'
            '<td style="padding:4px 8px;text-align:right;color:#c9d1d9">' + (t["target"] or "-") + '</td>'
            '<td style="padding:4px 8px;text-align:center">'
            '<span style="background:' + color + ';color:#000;padding:2px 6px;'
            'border-radius:8px;font-size:10px;font-weight:bold">' + icon + ' ' + oc + '</span>'
            '</td>'
            '<td style="padding:4px 8px;color:#8b949e">'            + (t["notes"] or "")    + '</td>'
            '</tr>'
        )
    html += '</table></div>'
    return html

@app.get("/pick-profile", response_class=HTMLResponse)
async def pick_profile():
    profiles = db_get_profiles()
    cards = ""
    for p in profiles:
        avatar = p.get("avatar") or "📈"
        cards += (
            '<div class="profile-card" onclick="selectProfile(' + str(p["id"]) + ')">'
            '<div class="avatar-circle" style="background:' + p["color"] + ';box-shadow:0 0 20px ' + p["color"] + '55">'
            + avatar +
            '</div>'
            '<div class="profile-name">' + p["name"] + '</div>'
            '<div class="profile-edit" onclick="event.stopPropagation();openEdit(' + str(p["id"]) + ',\'' + p["name"] + '\',\'' + p["color"] + '\',\'' + avatar + '\')" title="Edit">✏️</div>'
            '</div>'
        )
    color_opts = "".join(
        '<div class="color-dot" onclick="pickColor(this,\'' + c + '\')" style="background:' + c + '" data-color="' + c + '"></div>'
        for c in PROFILE_COLORS
    )
    avatar_opts = "".join(
        '<div class="av-opt" onclick="pickAvatar(this,\'' + a + '\')">' + a + '</div>'
        for a in AVATARS
    )
    first_color = PROFILE_COLORS[0]
    first_avatar = AVATARS[0]
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Invest AI</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0d1117;color:#e6edf3;font-family:Arial,sans-serif;min-height:100vh;
          display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;
          background-image:radial-gradient(ellipse at 20% 20%,rgba(0,212,255,.06) 0%,transparent 60%),
                           radial-gradient(ellipse at 80% 80%,rgba(179,136,255,.05) 0%,transparent 60%)}}
    h1{{color:#00d4ff;font-size:2em;margin-bottom:6px;text-align:center;text-shadow:0 0 30px rgba(0,212,255,.4)}}
    .sub{{color:#8b949e;font-size:15px;margin-bottom:44px;text-align:center}}
    .profiles{{display:flex;flex-wrap:wrap;gap:28px;justify-content:center;margin-bottom:44px;max-width:680px}}
    .profile-card{{display:flex;flex-direction:column;align-items:center;gap:10px;padding:12px;
                   cursor:pointer;border-radius:12px;transition:.2s;position:relative}}
    .profile-card:hover{{background:rgba(255,255,255,.04);transform:translateY(-3px)}}
    .avatar-circle{{width:88px;height:88px;border-radius:50%;display:flex;align-items:center;
                    justify-content:center;font-size:38px;transition:.2s}}
    .profile-card:hover .avatar-circle{{transform:scale(1.07)}}
    .profile-name{{color:#e6edf3;font-size:14px;font-weight:500}}
    .profile-edit{{position:absolute;top:6px;right:6px;font-size:13px;opacity:0;transition:.2s;
                   background:#161b22;border-radius:50%;width:24px;height:24px;display:flex;
                   align-items:center;justify-content:center;border:1px solid #30363d}}
    .profile-card:hover .profile-edit{{opacity:1}}
    .add-btn{{background:none;border:2px dashed #30363d;color:#8b949e;padding:13px 28px;
              border-radius:10px;cursor:pointer;font-size:14px;transition:.2s}}
    .add-btn:hover,.add-btn:active{{border-color:#00d4ff;color:#00d4ff}}
    .modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:99;
            align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(4px)}}
    .modal.open{{display:flex}}
    .modal-box{{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:28px;
                width:100%;max-width:360px;max-height:90vh;overflow-y:auto}}
    .modal-box h3{{color:#00d4ff;margin-bottom:18px;font-size:16px}}
    .label{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}}
    input.name-input{{width:100%;padding:11px;background:#0d1117;border:1px solid #30363d;
                      border-radius:8px;color:#e6edf3;font-size:16px;margin-bottom:16px}}
    .avatars{{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;margin-bottom:16px}}
    .av-opt{{font-size:24px;text-align:center;padding:6px;border-radius:8px;cursor:pointer;
             transition:.15s;border:2px solid transparent}}
    .av-opt:hover,.av-opt.sel{{background:#0d1117;border-color:#00d4ff}}
    .colors{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}}
    .color-dot{{width:28px;height:28px;border-radius:50%;cursor:pointer;border:2px solid transparent;transition:.15s}}
    .color-dot:hover,.color-dot.sel{{border-color:#fff;transform:scale(1.15)}}
    .save-btn{{background:#00d4ff;color:#000;width:100%;padding:12px;border:none;border-radius:8px;
               font-size:15px;font-weight:bold;cursor:pointer;margin-bottom:8px}}
    .del-btn{{background:none;border:1px solid #ff6b6b;color:#ff6b6b;width:100%;padding:9px;
              border-radius:8px;font-size:13px;cursor:pointer}}
    @media(max-width:480px){{
      .avatar-circle{{width:72px;height:72px;font-size:30px}}
      .avatars{{grid-template-columns:repeat(6,1fr)}}
    }}
  </style>
</head>
<body>
  <h1>📊 Invest AI</h1>
  <div class="sub">Who's watching?</div>
  <div class="profiles" id="profiles">{cards}</div>
  <button class="add-btn" onclick="openAdd()">+ Add Profile</button>

  <div class="modal" id="profileModal" onclick="if(event.target===this)closeModal()">
    <div class="modal-box">
      <h3 id="modalTitle">New Profile</h3>
      <div class="label">Name</div>
      <input class="name-input" id="pName" placeholder="Your name" maxlength="20"/>
      <div class="label">Avatar</div>
      <div class="avatars" id="avatarPicker">{avatar_opts}</div>
      <div class="label">Color</div>
      <div class="colors" id="colorPicker">{color_opts}</div>
      <input type="hidden" id="chosenColor" value="{first_color}"/>
      <input type="hidden" id="chosenAvatar" value="{first_avatar}"/>
      <input type="hidden" id="editingId" value=""/>
      <button class="save-btn" onclick="saveProfile()">Save Profile</button>
      <button class="del-btn" id="delBtn" style="display:none" onclick="deleteProfile()">Delete Profile</button>
    </div>
  </div>

  <script>
    function openAdd() {{
      document.getElementById('modalTitle').innerText = 'New Profile';
      document.getElementById('pName').value = '';
      document.getElementById('editingId').value = '';
      document.getElementById('delBtn').style.display = 'none';
      selectAvOpt(document.querySelector('.av-opt'));
      selectColorDot(document.querySelector('.color-dot'));
      document.getElementById('profileModal').classList.add('open');
    }}
    function openEdit(id, name, color, avatar) {{
      document.getElementById('modalTitle').innerText = 'Edit Profile';
      document.getElementById('pName').value = name;
      document.getElementById('editingId').value = id;
      document.getElementById('delBtn').style.display = 'block';
      document.querySelectorAll('.av-opt').forEach(function(el) {{
        if (el.innerText === avatar) selectAvOpt(el);
      }});
      document.querySelectorAll('.color-dot').forEach(function(el) {{
        if (el.dataset.color === color) selectColorDot(el);
      }});
      document.getElementById('profileModal').classList.add('open');
    }}
    function closeModal() {{ document.getElementById('profileModal').classList.remove('open'); }}
    function selectAvOpt(el) {{
      document.querySelectorAll('.av-opt').forEach(function(d) {{ d.classList.remove('sel'); }});
      if (el) {{ el.classList.add('sel'); document.getElementById('chosenAvatar').value = el.innerText; }}
    }}
    function selectColorDot(el) {{
      document.querySelectorAll('.color-dot').forEach(function(d) {{ d.classList.remove('sel'); }});
      if (el) {{ el.classList.add('sel'); document.getElementById('chosenColor').value = el.dataset.color; }}
    }}
    function pickAvatar(el) {{ selectAvOpt(el); }}
    function pickColor(el, color) {{ selectColorDot(el); }}
    async function saveProfile() {{
      const name   = document.getElementById('pName').value.trim();
      const color  = document.getElementById('chosenColor').value;
      const avatar = document.getElementById('chosenAvatar').value;
      const eid    = document.getElementById('editingId').value;
      if (!name) {{ alert('Enter a name'); return; }}
      if (eid) {{
        await fetch('/profiles/' + eid, {{
          method: 'PATCH',
          headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{name, color, avatar}})
        }});
      }} else {{
        await fetch('/profiles', {{
          method: 'POST',
          headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{name, color, avatar}})
        }});
      }}
      window.location.reload();
    }}
    async function deleteProfile() {{
      if (!confirm('Delete this profile? All watchlist and history will be lost.')) return;
      await fetch('/profiles/' + document.getElementById('editingId').value, {{method:'DELETE'}});
      window.location.reload();
    }}
    async function selectProfile(id) {{
      await fetch('/select-profile/' + id, {{method:'POST'}});
      window.location.href = '/';
    }}
    document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeModal(); }});
    // pre-select defaults
    const firstAv = document.querySelector('.av-opt');
    const firstCl = document.querySelector('.color-dot');
    if (firstAv) firstAv.classList.add('sel');
    if (firstCl) firstCl.classList.add('sel');
  </script>
</body>
</html>"""

@app.post("/select-profile/{pid}")
async def select_profile(pid: int, response: Response):
    profile = db_get_profile(pid)
    if not profile:
        raise HTTPException(404, "Profile not found")
    response.set_cookie("profile_id", str(pid), max_age=60*60*24*90, samesite="lax")
    return {"ok": True}

@app.get("/profiles")
async def list_profiles():
    return db_get_profiles()

@app.post("/profiles")
async def create_profile(data: dict):
    name   = (data.get("name") or "").strip()[:20]
    color  = data.get("color")  or PROFILE_COLORS[0]
    avatar = data.get("avatar") or AVATARS[0]
    if not name:
        raise HTTPException(400, "Name required")
    try:
        db_create_profile(name, color, avatar)
    except Exception:
        raise HTTPException(400, "Name already taken")
    return {"ok": True}

@app.patch("/profiles/{pid}")
async def update_profile(pid: int, data: dict):
    name   = (data.get("name") or "").strip()[:20]
    color  = data.get("color")  or PROFILE_COLORS[0]
    avatar = data.get("avatar") or AVATARS[0]
    if not name:
        raise HTTPException(400, "Name required")
    with get_db() as con:
        con.execute("UPDATE profiles SET name=?,color=?,avatar=? WHERE id=?",
                    (name, color, avatar, pid))
    return {"ok": True}

@app.delete("/profiles/{pid}")
async def delete_profile(pid: int):
    db_delete_profile(pid)
    return {"ok": True}

# ── Server-side watchlist ─────────────────────────────────────────────────────

@app.get("/watchlist-server")
async def wl_get(request: Request):
    pid = current_profile_id(request)
    if not pid:
        return []
    return db_get_watchlist(pid)

class WatchItem(BaseModel):
    ticker: str
    size: float
    entry: float
    type: str = "stock"

@app.post("/watchlist-server")
async def wl_add(item: WatchItem, request: Request):
    pid = current_profile_id(request)
    if not pid:
        raise HTTPException(403, "No profile selected")
    db_add_watch(pid, item.ticker, item.size, item.entry, item.type)
    return {"ok": True}

@app.delete("/watchlist-server/{wid}")
async def wl_remove(wid: int, request: Request):
    pid = current_profile_id(request)
    if not pid:
        raise HTTPException(403, "No profile selected")
    db_remove_watch(wid, pid)
    return {"ok": True}

# ── Per-user history ──────────────────────────────────────────────────────────

@app.get("/my-history")
async def my_history(request: Request, limit: int = 30):
    pid = current_profile_id(request)
    if not pid:
        return []
    return db_get_history(pid, limit)

def get_market_session():
    """Returns (label, color, pulse, et_datetime, tz_name)."""
    from datetime import datetime, timezone, timedelta, time as dtime
    utc = datetime.now(timezone.utc)
    month, day = utc.month, utc.day
    # DST: EDT (UTC-4) Mar 8 – Nov 6, EST (UTC-5) otherwise
    is_edt = (month > 3 or (month == 3 and day >= 8)) and (month < 11 or (month == 11 and day < 7))
    et_dt   = utc - timedelta(hours=4 if is_edt else 5)
    et_time = et_dt.time()
    tz_name = "EDT" if is_edt else "EST"
    if dtime(4, 0) <= et_time < dtime(9, 30):
        return "PRE-MARKET",    "#ff9800", False, et_dt, tz_name
    elif dtime(9, 30) <= et_time < dtime(16, 0):
        return "MARKET OPEN",   "#00ff88", True,  et_dt, tz_name
    elif dtime(16, 0) <= et_time < dtime(20, 0):
        return "AFTER-HOURS",   "#b388ff", False, et_dt, tz_name
    else:
        return "CLOSED",        "#555e6a", False, et_dt, tz_name

def get_ticker_news(ticker, limit=5):
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        return []
    try:
        r = requests.get(
            "https://api.polygon.io/v2/reference/news",
            params={"ticker": ticker, "limit": limit, "order": "desc", "sort": "published_utc", "apiKey": key},
            timeout=5
        )
        items = r.json().get("results", [])
        return [
            {
                "title": a.get("title", ""),
                "source": a.get("publisher", {}).get("name", ""),
                "url": a.get("article_url", ""),
                "published": a.get("published_utc", "")[:16].replace("T", " "),
                "summary": a.get("description", "")[:180],
            }
            for a in items
        ]
    except Exception as e:
        return [{"title": f"Error: {e}", "source": "", "url": "", "published": "", "summary": ""}]

def get_options_expirations(ticker):
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        return []
    try:
        r = requests.get(
            "https://sandbox.tradier.com/v1/markets/options/expirations",
            params={"symbol": ticker, "includeAllRoots": "true", "strikes": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=5
        )
        dates = r.json().get("expirations", {}).get("date", [])
        return dates if isinstance(dates, list) else [dates]
    except Exception:
        return []

def get_options_chain(ticker, expiration):
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        return {"error": "TRADIER_TOKEN not configured"}
    try:
        r = requests.get(
            "https://sandbox.tradier.com/v1/markets/options/chains",
            params={"symbol": ticker, "expiration": expiration, "greeks": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10
        )
        raw = r.json().get("options") or {}
        chain = raw.get("option", [])
        if isinstance(chain, dict):
            chain = [chain]
        def fmt(o):
            return {
                "strike": o.get("strike"),
                "bid": o.get("bid"),
                "ask": o.get("ask"),
                "last": o.get("last"),
                "volume": o.get("volume"),
                "open_interest": o.get("open_interest"),
            }
        calls = sorted([fmt(o) for o in chain if o.get("option_type") == "call"], key=lambda x: x["strike"] or 0)
        puts  = sorted([fmt(o) for o in chain if o.get("option_type") == "put"],  key=lambda x: x["strike"] or 0)
        return {"calls": calls, "puts": puts}
    except Exception as e:
        return {"error": str(e)}

class Query(BaseModel):
    question: str
    tickers: list = ["SPY", "QQQ", "SOXL", "META", "MSFT"]

# ── Item 4: Market Regime (live dashboard version) ───────────────────────────

def get_regime_data():
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return {"error": "ALPACA not configured"}
    try:
        import statistics as _stats
        start = (datetime.utcnow() - timedelta(days=320)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            params={"timeframe": "1Day", "limit": 220, "adjustment": "split",
                    "start": start, "sort": "asc"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10
        )
        bars = r.json().get("bars", [])
        if len(bars) < 50:
            return {"error": "Not enough SPY bar data"}
        closes   = [b["c"] for b in bars]
        ma50     = sum(closes[-50:]) / 50
        ma200    = sum(closes[-200:]) / 200 if len(closes) >= 200 else sum(closes) / len(closes)
        current  = closes[-1]
        returns  = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        daily_vol = _stats.stdev(returns[-20:]) if len(returns) >= 20 else 0
        ann_vol   = round(daily_vol * (252 ** 0.5) * 100, 1)
        pct_from_200 = round((current - ma200) / ma200 * 100, 2)
        above_200 = current > ma200
        above_50  = current > ma50
        if not above_200 and ann_vol > 40:
            regime, icon = "CRASH", "🔴"
        elif not above_200 and ann_vol > 25:
            regime, icon = "BEAR", "🔴"
        elif not above_200:
            regime, icon = "BEAR", "🟠"
        elif above_200 and above_50 and ann_vol < 20:
            regime, icon = "BULL", "🟢"
        elif above_200 and ann_vol > 30:
            regime, icon = "CHOPPY", "🟡"
        else:
            regime, icon = "NEUTRAL", "🟡"
        return {
            "regime": regime, "icon": icon,
            "spy": round(current, 2),
            "ma50": round(ma50, 2), "ma200": round(ma200, 2),
            "pct_from_200": pct_from_200, "ann_vol": ann_vol,
            "above_200": above_200, "above_50": above_50,
        }
    except Exception as e:
        return {"error": str(e)}

# ── Item 2: Kevin's Call Backtest ─────────────────────────────────────────────

def get_backtest_data():
    trades = get_track_record()
    closed = [t for t in trades if t["outcome"] in ("HIT", "MISS")]
    if not closed:
        return {"trades": [], "summary": {}}
    result = []
    for t in closed:
        entry  = float(t["entry"])  if t["entry"]  else None
        target = float(t["target"]) if t["target"] else None
        expected = round((target - entry) / entry * 100, 1) if (entry and target) else None
        result.append({
            "date": t["date"], "ticker": t["ticker"], "call": t["call"],
            "entry": t["entry"], "target": t["target"],
            "expected_return": expected, "outcome": t["outcome"], "notes": t["notes"],
        })
    actual_returns = []
    for r in result:
        if r["expected_return"] is not None:
            actual_returns.append(
                r["expected_return"] if r["outcome"] == "HIT"
                else -abs(r["expected_return"] * 0.5)
            )
        else:
            actual_returns.append(10.0 if r["outcome"] == "HIT" else -5.0)
    all_exp = [r["expected_return"] for r in result if r["expected_return"] is not None]
    summary = {
        "avg_return":  round(sum(actual_returns) / len(actual_returns), 1) if actual_returns else 0,
        "best":        round(max(all_exp), 1) if all_exp else 0,
        "worst":       round(min(actual_returns), 1) if actual_returns else 0,
        "hit_rate":    round(sum(1 for r in result if r["outcome"] == "HIT") / len(result) * 100),
        "trade_count": len(result),
    }
    return {"trades": result, "summary": summary}

# ── Item 5: Correlation Break Detector ───────────────────────────────────────

def get_correlation_data():
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return {"error": "ALPACA not configured"}
    try:
        from datetime import timedelta as _td
        start = (datetime.utcnow() - _td(days=100)).strftime("%Y-%m-%d")
        bars_a, bars_b = [], []
        for ticker, store in [("SOXL", bars_a), ("QQQ", bars_b)]:
            r = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
                params={"timeframe": "1Day", "limit": 65, "adjustment": "split",
                        "start": start, "sort": "asc"},
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                timeout=10
            )
            store.extend(b["c"] for b in r.json().get("bars", []))
        n = min(len(bars_a), len(bars_b))
        if n < 25:
            return {"error": "Not enough bar data (need 25 days)"}
        a, b = bars_a[-n:], bars_b[-n:]

        def pearson(x, y):
            m = len(x)
            mx, my = sum(x) / m, sum(y) / m
            num = sum((x[i] - mx) * (y[i] - my) for i in range(m))
            den = (sum((x[i] - mx) ** 2 for i in range(m)) *
                   sum((y[i] - my) ** 2 for i in range(m))) ** 0.5
            return num / den if den else 0

        current_corr  = round(pearson(a[-20:], b[-20:]), 3)
        baseline_corr = round(pearson(a, b), 3)
        delta         = round(current_corr - baseline_corr, 3)
        return {
            "current_corr": current_corr, "baseline_corr": baseline_corr,
            "delta": delta, "break_detected": abs(delta) > 0.25,
            "current_days": 20, "baseline_days": n,
        }
    except Exception as e:
        return {"error": str(e)}

# ── Item 6: Monte Carlo Signal Strength ───────────────────────────────────────

def run_monte_carlo():
    import random
    trades = get_track_record()
    closed = [t for t in trades if t["outcome"] in ("HIT", "MISS")]
    if len(closed) < 5:
        return {"message": f"Need ≥5 closed trades for Monte Carlo (have {len(closed)}). Log trades in track_record.txt."}
    trade_returns = []
    for t in closed:
        entry  = float(t["entry"])  if t["entry"]  else None
        target = float(t["target"]) if t["target"] else None
        if entry and target:
            exp = (target - entry) / entry * 100
            trade_returns.append(exp if t["outcome"] == "HIT" else -abs(exp * 0.5))
        else:
            trade_returns.append(10.0 if t["outcome"] == "HIT" else -5.0)
    N = 1000
    results = []
    for _ in range(N):
        shuffled   = random.sample(trade_returns, len(trade_returns))
        compounded = 100.0
        for ret in shuffled:
            compounded *= (1 + ret / 100)
        results.append(round(compounded - 100, 2))
    results.sort()
    return {
        "simulations":   N,
        "trade_count":   len(trade_returns),
        "median_return": round(results[N // 2], 1),
        "prob_profit":   round(sum(1 for r in results if r > 0) / N * 100, 1),
        "worst_5pct":    round(results[int(N * 0.05)], 1),
        "best_5pct":     round(results[int(N * 0.95)], 1),
    }

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    pid = current_profile_id(request)
    if not pid:
        return RedirectResponse(url="/pick-profile")
    profile = db_get_profile(pid)
    if not profile:
        return RedirectResponse(url="/pick-profile")

    avatar = profile.get("avatar") or "📈"
    profile_bar = (
        '<div style="display:flex;justify-content:space-between;align-items:center;'
        'margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid #30363d;flex-wrap:wrap;gap:8px">'
        '<div style="display:flex;align-items:center;gap:10px">'
        '<div style="width:36px;height:36px;border-radius:50%;background:' + profile["color"] + ';'
        'box-shadow:0 0 12px ' + profile["color"] + '55;'
        'display:flex;align-items:center;justify-content:center;font-size:20px">'
        + avatar + '</div>'
        '<span style="color:#e6edf3;font-weight:bold;font-size:15px">' + profile["name"] + '</span>'
        '</div>'
        '<div style="display:flex;gap:6px;align-items:center">'
        '<button onclick="toggleHistory()" style="background:#161b22;border:1px solid #30363d;'
        'color:#8b949e;padding:6px 12px;border-radius:20px;cursor:pointer;font-size:12px;white-space:nowrap">📋 History</button>'
        '<a href="/pick-profile" style="background:#161b22;border:1px solid #30363d;'
        'color:#8b949e;padding:6px 12px;border-radius:20px;cursor:pointer;font-size:12px;text-decoration:none;white-space:nowrap">⇄ Switch</a>'
        '</div>'
        '</div>'
    )

    analysis = get_latest_analysis()
    report_name, _ = get_latest_report()
    reports = get_all_reports()
    report_count = len(reports)

    session_label, session_color, session_pulse, et_dt, tz_name = get_market_session()
    is_extended = session_label in ("PRE-MARKET", "AFTER-HOURS")
    ext_tag     = "PRE" if session_label == "PRE-MARKET" else ("AH" if session_label == "AFTER-HOURS" else "")
    et_time_str = et_dt.strftime('%Y-%m-%d %H:%M') + " " + tz_name

    # Single smart session badge — pulses only when market is actually open
    _badge_cls = "badge-green" if session_pulse else ""
    _badge_style = (
        f'background:{session_color};color:{"#000" if session_label != "CLOSED" else "#c9d1d9"};'
        'padding:3px 10px;border-radius:10px;font-size:11px;font-weight:bold;'
        f'{"animation:livePulse 2.5s infinite;" if session_pulse else ""}'
    )
    session_badge = f'<span style="{_badge_style}">● {session_label}</span>'

    prices = get_live_prices(["SPY", "QQQ", "SOXL", "META", "MSFT", "MRVL", "TSLA"])
    price_html = ""
    for ticker, data in prices.items():
        if isinstance(data, dict) and "price" in data:
            color = "#00ff88" if data["change_pct"] >= 0 else "#ff6b6b"
            arrow = "▲" if data["change_pct"] >= 0 else "▼"
            ext_badge = (
                ' <span style="background:' + session_color + ';color:#000;padding:1px 4px;'
                'border-radius:3px;font-size:10px;font-weight:bold">' + ext_tag + '</span>'
            ) if is_extended else ""
            price_html += (
                f'<span class="ticker"><b>{ticker}</b> ${data["price"]} '
                f'<span style="color:{color}">{arrow}{abs(data["change_pct"])}%</span>'
                f'{ext_badge}</span>'
            )

    analysis_html = analysis.replace("\n", "<br>") if analysis else "No analysis yet. Upload a report to Synology first."

    track_html = build_track_record_html(get_track_record())

    _help_json = json.dumps({
        "prices": {
            "title": "Live Prices",
            "what": "Real-time stock prices pulled from Alpaca Markets, refreshed every 5 minutes. The % shows change vs. yesterday's closing price.",
            "how": "PRE badge = pre-market hours (4am–9:30am ET). AH = after-hours (4pm–8pm ET). Prices outside regular hours are less liquid and can look extreme — treat them as directional signals, not exact values.",
            "tip": "SOXL is a 3x leveraged ETF: a 1% move in semiconductors = roughly 3% move in SOXL. High reward but also high risk — position size carefully.",
            "links": [{"label": "How the stock market works", "url": "https://www.investopedia.com/terms/s/stockmarket.asp"}, {"label": "What is pre-market trading?", "url": "https://www.investopedia.com/terms/p/premarket.asp"}]
        },
        "track-record": {
            "title": "Kevin's Track Record",
            "what": "A personal log of every recommendation Kevin has made in his Alpha Reports — what he said to buy or avoid, your entry price, target, and whether it worked.",
            "how": "Edit track_record.txt in your alpha-reports folder on Synology. One trade per line. Claude reads this file when you ask questions so its answers account for Kevin's actual hit rate and past accuracy.",
            "tip": "Even 10 logged trades gives Claude meaningful context. Start now — even with hindsight entries from the reports you already have.",
            "links": [{"label": "What is a trading journal?", "url": "https://www.investopedia.com/terms/p/papertrade.asp"}, {"label": "How to evaluate a trading strategy", "url": "https://www.investopedia.com/articles/trading/09/risk-management.asp"}]
        },
        "earnings": {
            "title": "Earnings Calendar",
            "what": "Estimated dates when companies in Kevin's watchlist report quarterly earnings. Earnings = profits/losses for the quarter. These reports often cause 5–20% price swings in one day.",
            "how": "Dates show ~est because they are calculated estimates, not confirmed. Always verify the exact date in your broker app or at earningswhispers.com before trading around earnings.",
            "tip": "Holding options through earnings is very risky. Even if the stock moves your way, implied volatility collapses after the announcement and can kill your option's value (called IV crush).",
            "links": [{"label": "What is earnings season?", "url": "https://www.investopedia.com/terms/e/earnings-season.asp"}, {"label": "IV crush explained", "url": "https://www.investopedia.com/terms/i/iv-crush.asp"}]
        },
        "position-sizing": {
            "title": "Position Sizing Calculator",
            "what": "Calculates the exact number of shares or contracts to buy so you never risk more than your set percentage per trade. This is the single most important risk management tool.",
            "how": "Enter your account size and risk % (start with 1–2%). Enter your planned entry price and where you would sell if wrong (stop loss). The calculator outputs the quantity. For options, add the premium per contract to see how many contracts fit your risk budget.",
            "tip": "If you risk 2% per trade, you can be wrong 50 times in a row before losing your account. Most beginners skip position sizing and blow up. This one tool separates amateurs from professionals.",
            "links": [{"label": "Position sizing (Investopedia)", "url": "https://www.investopedia.com/terms/p/positionsizing.asp"}, {"label": "The 2% risk rule", "url": "https://www.investopedia.com/articles/trading/09/risk-management.asp"}]
        },
        "watchlist": {
            "title": "Watchlist & Live P&L",
            "what": "Your open positions with live profit and loss calculated against your entry price. Supports both shares and options contracts (auto-multiplies by 100).",
            "how": "Click + Add, enter the ticker, quantity, and the price you paid. Your positions save in your browser and come back next session. Click the refresh button for fresh prices. Click x to remove a closed position.",
            "tip": "Watching live P&L constantly can make you emotional and cause early exits on good trades. Many experienced traders set alerts instead of watching the number all day.",
            "links": [{"label": "Understanding unrealized P&L", "url": "https://www.investopedia.com/terms/u/unrealizedgain.asp"}, {"label": "How to manage open positions", "url": "https://www.investopedia.com/terms/p/position.asp"}]
        },
        "ask-claude": {
            "title": "Ask Claude",
            "what": "An AI investment analyst that reads all your historical reports, today's report, live prices, and Kevin's track record before answering your question. It knows the full context, not just today.",
            "how": "Ask specific, actionable questions. Good examples: 'Should I buy SOXL calls today given the RSI?', 'What has Kevin said about META over the past 3 weeks?', 'Is this a good time to add to my QQQ position?'",
            "tip": "The more specific your question, the better the answer. Instead of 'what should I buy?' try 'Given Kevin was bullish on SOXL last week and RSI is now 82, should I wait for a pullback or scale in now?'",
            "links": [{"label": "How to ask better investment questions", "url": "https://www.investopedia.com/articles/basics/09/how-to-analyze-investments.asp"}, {"label": "Disclaimer: not financial advice", "url": "https://www.investopedia.com/terms/f/financial-advisor.asp"}]
        },
        "analysis": {
            "title": "Latest Analysis",
            "what": "The most recent automated Claude analysis of Kevin's Alpha Report. Runs every 30 minutes. Reads all 15+ historical reports for context and produces a 10-section breakdown including market sentiment, specific trade ideas, and the top 3 actions.",
            "how": "The analysis updates automatically after you upload a new report to Synology. You will get a Home Assistant push notification when it is ready. Scroll to the Top 3 Actions for the most time-sensitive items.",
            "tip": "The Pattern Analysis section is the most valuable — it shows what Kevin has been consistently saying across multiple weeks, which reveals his highest-conviction themes vs. one-day observations.",
            "links": [{"label": "How to read investment research", "url": "https://www.investopedia.com/articles/basics/09/how-to-analyze-investments.asp"}, {"label": "Bull vs bear market explained", "url": "https://www.investopedia.com/terms/b/bullmarket.asp"}]
        },
        "rsi-macd": {
            "title": "RSI & MACD Technical Indicators",
            "what": "Two momentum indicators that help you time WHEN to enter a trade Kevin is already bullish on. RSI measures overbought/oversold. MACD measures momentum direction and crossovers.",
            "how": "Use after deciding WHAT to buy from Kevin's report. If RSI is above 70 (overbought), wait for it to pull back to 50–60 before entering. A MACD bullish crossover means momentum just flipped up — a green entry signal. Bearish crossover means momentum is turning down.",
            "tip": "Technical indicators work best in trending markets. In choppy sideways markets they generate false signals. Always combine with Kevin's fundamental thesis — the technicals tell you when, Kevin tells you what.",
            "links": [{"label": "RSI explained (Investopedia)", "url": "https://www.investopedia.com/terms/r/rsi.asp"}, {"label": "MACD explained (Investopedia)", "url": "https://www.investopedia.com/terms/m/macd.asp"}]
        },
        "options-chain": {
            "title": "Options Chain",
            "what": "Shows all available call and put options for a stock, sorted by strike price and expiry date. Kevin frequently recommends specific options plays — this lets you see the current prices before acting.",
            "how": "Enter a ticker and click Load. Pick an expiry from the dropdown. CALLS (green, left) profit when the stock goes up. PUTS (red, right) profit when it goes down. The strike price is the agreed buy/sell price. High Open Interest (OI) means the contract is actively traded — easier to enter and exit.",
            "tip": "Note: This dashboard uses a Tradier sandbox account — prices are simulated. Always verify the actual bid/ask in your real brokerage before placing an options order. Options pricing changes fast.",
            "links": [{"label": "How to read an options chain", "url": "https://www.investopedia.com/terms/o/optionchain.asp"}, {"label": "Options basics for beginners", "url": "https://www.investopedia.com/options-basics-tutorial-4583012"}]
        },
        "news": {
            "title": "Ticker News",
            "what": "Latest news headlines for any stock from the Polygon financial data API. Shows the 5 most recent articles with source, publish time, and a summary.",
            "how": "Click any ticker badge or type a symbol and click Search. Read the headline and summary — do not click through to every article, just scan for anything surprising. Breaking news can override all technical and fundamental analysis instantly.",
            "tip": "Always check news before entering a position. A stock can look perfect on technicals and then gap down on a headline you did not see. Takes 30 seconds and can save you from a bad trade.",
            "links": [{"label": "How news moves stock prices", "url": "https://www.investopedia.com/articles/investing/060315/how-news-moves-markets.asp"}, {"label": "Understanding SEC filings", "url": "https://www.investopedia.com/terms/s/sec.asp"}]
        },
        "regime": {
            "title": "Market Regime",
            "what": "A quantitative classification of the current market environment based on SPY price data — BULL, NEUTRAL, CHOPPY, BEAR, or CRASH. Uses 50-day and 200-day moving averages plus 20-day annualized volatility.",
            "how": "In BULL regime: normal position sizing, favor calls. In CHOPPY/NEUTRAL: reduce size, wait for clearer signals. In BEAR/CRASH: defensive, favor puts or cash, tighten stops. The analyzer CronJob injects this into every Claude analysis so regime context is always factored in.",
            "tip": "Regime alone does not tell you what to buy — it tells you HOW to size and manage positions. Kevin's reports tell you WHAT. Combine both for better risk-adjusted trades.",
            "links": [{"label": "Bull vs bear market", "url": "https://www.investopedia.com/terms/b/bullmarket.asp"}, {"label": "Moving averages explained", "url": "https://www.investopedia.com/terms/m/movingaverage.asp"}]
        },
        "backtest": {
            "title": "Kevin's Call Backtest",
            "what": "Every closed trade from your track_record.txt enriched with the expected return (entry → target). Calculates average return, best/worst calls, and hit rate across all logged trades.",
            "how": "Maintain track_record.txt in your Synology alpha-reports folder. Format: date,ticker,call,entry,target,HIT/MISS,notes. The more trades you log, the more statistically meaningful the backtest becomes.",
            "tip": "MISS trades use a conservative 50% loss estimate since exact exit prices are rarely logged. The real edge from this data is identifying which TYPES of calls Kevin gets right most often — look for patterns in the ticker column.",
            "links": [{"label": "What is backtesting?", "url": "https://www.investopedia.com/terms/b/backtesting.asp"}, {"label": "How to keep a trade journal", "url": "https://www.investopedia.com/terms/p/papertrade.asp"}]
        },
        "correlation": {
            "title": "Correlation & Monte Carlo",
            "what": "Two risk tools. Correlation: monitors whether SOXL is moving in sync with QQQ (its normal relationship). Monte Carlo: runs 1,000 simulations of Kevin's historical calls in random order to show the range of possible outcomes.",
            "how": "Correlation break alert fires when the 20-day rolling correlation deviates >0.25 from the 60-day baseline — meaning SOXL is behaving unusually vs tech. Monte Carlo shows P(profit), median return, and the worst 5% scenario so you understand your real risk.",
            "tip": "A correlation break on SOXL/QQQ is often an early warning of sector rotation or a volatility event in semiconductors. When it fires, reduce SOXL position size until the correlation normalizes.",
            "links": [{"label": "Correlation in finance", "url": "https://www.investopedia.com/terms/c/correlation.asp"}, {"label": "Monte Carlo simulation", "url": "https://www.investopedia.com/terms/m/montecarlosimulation.asp"}]
        }
    })

    # Pre-build earnings card HTML
    _earnings = get_earnings_calendar()
    if not _earnings:
        _earn_html = '<span class="meta">No earnings data available.</span>'
    else:
        _earn_html = (
            '<div class="scroll-x">'
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<tr>'
            + "".join(
                '<th style="text-align:' + al + ';padding:4px 8px;color:#8b949e;border-bottom:1px solid #30363d">' + h + '</th>'
                for h, al in [("Ticker","left"),("Last Quarter","left"),("EPS","right"),
                               ("Next Report (est)","left"),("Days","right")]
            ) +
            '</tr>'
        )
        for e in _earnings:
            da = e["days_away"]
            if da is not None and da <= 7:
                date_col = '<b style="color:#ff6b6b">' + e["next_date"] + '</b>'
                day_badge = '<span style="background:#ff6b6b;color:#000;padding:1px 5px;border-radius:6px;font-size:10px;font-weight:bold">⚡ ' + str(da) + 'd</span>'
            elif da is not None and da <= 21:
                date_col = '<b style="color:#ff9800">' + e["next_date"] + '</b>'
                day_badge = '<span style="background:#ff9800;color:#000;padding:1px 5px;border-radius:6px;font-size:10px;font-weight:bold">' + str(da) + 'd</span>'
            else:
                date_col = e["next_date"]
                day_badge = ('<span class="meta">' + str(da) + 'd</span>') if da is not None else '<span class="meta">—</span>'
            _earn_html += (
                '<tr>'
                '<td style="padding:5px 8px;font-weight:bold;color:#00d4ff">' + e["ticker"] + '</td>'
                '<td style="padding:5px 8px;color:#8b949e">' + e["last_period"] + '</td>'
                '<td style="padding:5px 8px;text-align:right;color:#c9d1d9">' + e["last_eps"] + '</td>'
                '<td style="padding:5px 8px">' + date_col
                + ' <span style="color:#8b949e;font-size:10px">~est</span></td>'
                '<td style="padding:5px 8px;text-align:right">' + day_badge + '</td>'
                '</tr>'
            )
        _earn_html += '</table></div><div class="meta" style="font-size:11px;margin-top:6px">Dates are estimates (filing date +91d or period end +45d). Verify at earnings.com before trading.</div>'

    _news_tickers = ['SPY','QQQ','SOXL','META','MSFT','MRVL','TSLA','AXON','RKT']
    news_badges = "".join(
        '<span class="ticker" style="cursor:pointer" onclick="loadNews(\'' + t + '\')">' + t + '</span>'
        for t in _news_tickers
    )

    return f"""<html>
    <head>
      <title>Invest AI</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="refresh" content="300">
      <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        html{{scroll-behavior:smooth}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
              max-width:960px;margin:0 auto;padding:15px;background:#0d1117;color:#e6edf3;
              background-image:radial-gradient(ellipse at 10% 10%,rgba(0,212,255,.04) 0%,transparent 50%),
                               radial-gradient(ellipse at 90% 90%,rgba(179,136,255,.04) 0%,transparent 50%)}}
        /* ── Typography ─────────────────────────────── */
        h1{{color:#00d4ff;font-size:1.8em;margin-bottom:15px;text-shadow:0 0 24px rgba(0,212,255,.35)}}
        h3{{color:#00d4ff;margin-bottom:10px;display:flex;align-items:center;gap:4px}}
        /* ── Cards ──────────────────────────────────── */
        .card{{background:#161b22;padding:20px;border-radius:12px;margin:14px 0;
               border:1px solid #30363d;transition:box-shadow .25s,transform .2s}}
        @media(hover:hover){{.card:hover{{box-shadow:0 4px 24px rgba(0,212,255,.07);transform:translateY(-1px)}}}}
        /* ── Badges ─────────────────────────────────── */
        .badge{{background:#00d4ff;color:#000;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold}}
        .badge-green{{background:#00ff88;color:#000;padding:2px 8px;border-radius:10px;font-size:11px;
                      font-weight:bold;animation:livePulse 2.5s infinite}}
        @keyframes livePulse{{0%,100%{{box-shadow:0 0 0 0 rgba(0,255,136,.5)}}70%{{box-shadow:0 0 0 7px rgba(0,255,136,0)}}}}
        /* ── Ticker chips ───────────────────────────── */
        .ticker{{display:inline-block;background:#0d1117;padding:6px 12px;border-radius:20px;
                 margin:3px;font-size:13px;border:1px solid #30363d;transition:.15s}}
        .ticker:hover{{border-color:#00d4ff;color:#00d4ff}}
        /* ── Inputs ─────────────────────────────────── */
        input{{width:100%;padding:12px;font-size:16px;border-radius:8px;border:1px solid #30363d;
               margin:8px 0;background:#0d1117;color:#e6edf3;transition:border-color .15s;
               -webkit-appearance:none}}
        input:focus{{outline:none;border-color:#00d4ff}}
        select{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;
                padding:10px;font-size:14px;-webkit-appearance:none}}
        /* ── Buttons ────────────────────────────────── */
        button{{background:#00d4ff;color:#000;padding:12px;font-size:15px;border:none;
                border-radius:8px;cursor:pointer;width:100%;font-weight:bold;margin-top:5px;
                transition:background .15s,transform .1s;-webkit-tap-highlight-color:transparent}}
        button:hover{{background:#00b8d9}}
        button:active{{transform:scale(.97)}}
        /* ── Utility ────────────────────────────────── */
        .meta{{color:#8b949e;font-size:13px}}
        .grid{{display:grid;grid-template-columns:1fr 1fr;gap:15px}}
        #ans{{margin-top:12px;padding:15px;background:#0d1117;border-radius:8px;min-height:40px;
              line-height:1.7;white-space:pre-wrap;border:1px solid #30363d;font-size:14px}}
        .analysis{{font-size:13px;line-height:1.7;max-height:700px;overflow-y:auto;padding-right:5px}}
        .scroll-x{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
        /* ── Quick chips ────────────────────────────── */
        .chip{{flex-shrink:0;background:#0d1117;border:1px solid #30363d;color:#8b949e;
               padding:8px 14px;border-radius:20px;font-size:12px;cursor:pointer;white-space:nowrap;
               transition:.15s;-webkit-tap-highlight-color:transparent}}
        .chip:hover,.chip:active{{border-color:#00d4ff;color:#00d4ff;background:#0d1a26}}
        /* ── Mobile bottom bar ──────────────────────── */
        .quick-bar{{position:fixed;bottom:0;left:0;right:0;background:rgba(22,27,34,.96);
                    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
                    border-top:1px solid #30363d;padding:10px 14px;display:none;gap:7px;
                    z-index:200;overflow-x:auto;-webkit-overflow-scrolling:touch}}
        /* ── Help button ────────────────────────────── */
        .help-btn{{display:inline-flex;align-items:center;justify-content:center;
                   width:18px;height:18px;background:#30363d;color:#8b949e;border-radius:50%;
                   font-size:10px;font-weight:bold;cursor:pointer;border:none;
                   margin-left:7px;vertical-align:middle;line-height:1;flex-shrink:0;transition:.15s}}
        .help-btn:hover{{background:#00d4ff;color:#000}}
        /* ── Help/Info modal ────────────────────────── */
        .modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.78);
                        z-index:9999;align-items:center;justify-content:center;padding:15px;
                        backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}}
        .modal-overlay.open{{display:flex}}
        .modal-box{{background:#161b22;border:1px solid #30363d;border-radius:14px;
                    padding:22px 24px;max-width:500px;width:100%;max-height:88vh;
                    overflow-y:auto;position:relative;animation:slideUp .2s ease}}
        @keyframes slideUp{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:translateY(0)}}}}
        .modal-close{{position:absolute;top:10px;right:14px;background:none;border:none;
                      color:#8b949e;font-size:22px;cursor:pointer;line-height:1;width:auto;margin:0;padding:2px}}
        .modal-close:hover{{color:#e6edf3}}
        .modal-title{{color:#00d4ff;font-size:15px;font-weight:bold;margin-bottom:14px;padding-right:20px}}
        .modal-section{{margin-bottom:11px}}
        .modal-label{{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px}}
        .modal-text{{color:#c9d1d9;font-size:13px;line-height:1.65}}
        .modal-tip{{background:#0d1117;border-left:3px solid #ff9800;padding:9px 12px;border-radius:4px;
                    color:#c9d1d9;font-size:12px;line-height:1.6;margin-top:10px}}
        .modal-links{{margin-top:12px;display:flex;flex-direction:column;gap:7px}}
        .modal-links a{{color:#00d4ff;font-size:12px;text-decoration:none}}
        .modal-links a:hover{{text-decoration:underline}}
        /* ── Scrollbars ─────────────────────────────── */
        ::-webkit-scrollbar{{width:4px;height:4px}}
        ::-webkit-scrollbar-track{{background:#0d1117}}
        ::-webkit-scrollbar-thumb{{background:#30363d;border-radius:2px}}
        ::-webkit-scrollbar-thumb:hover{{background:#00d4ff}}
        /* ── Mobile ─────────────────────────────────── */
        @media(max-width:600px){{
          body{{padding:10px;padding-bottom:72px}}
          .card{{padding:14px;margin:8px 0;border-radius:10px}}
          h1{{font-size:1.45em;margin-bottom:10px}}
          h3{{font-size:13px}}
          input{{padding:10px;font-size:16px}}
          button{{padding:11px;font-size:14px}}
          .ticker{{padding:5px 10px;font-size:12px;margin:2px}}
          .help-btn{{width:20px;height:20px}}
          .grid{{grid-template-columns:1fr;gap:8px}}
          table{{font-size:11px !important}}
          .meta{{font-size:12px}}
          .quick-bar{{display:flex}}
          .analysis{{max-height:400px}}
          #ans{{font-size:13px}}
        }}
        @media(min-width:601px){{
          .grid{{grid-template-columns:1fr 1fr}}
        }}
      </style>
    </head>
    <body>
      <h1>📊 Invest AI</h1>

      <div id="historyPanel" style="display:none;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:15px;margin-bottom:15px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="color:#00d4ff;font-size:14px">📋 Your Recent Activity</h3>
          <button onclick="toggleHistory()" style="background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer">×</button>
        </div>
        <div id="historyContent" style="font-size:12px;color:#8b949e;max-height:250px;overflow-y:auto"></div>
      </div>

      <div class="card">
        {profile_bar}
        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:8px">
          <div>
            {session_badge}
            <button class="help-btn" onclick="showHelp('prices')">?</button>
            <span class="meta"> &nbsp;{et_time_str}</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="meta" style="font-size:11px">⏱ Auto-analysis every 10 min</span>
            <button id="runBtn" onclick="runAnalysis()" style="width:auto;margin:0;padding:6px 14px;font-size:12px;border-radius:20px;background:#00ff88;color:#000">▶ Run Now</button>
          </div>
        </div>
        <span class="meta"><b style="color:#e6edf3">{report_count}</b> reports &nbsp;|&nbsp; Latest: <b style="color:#e6edf3">{report_name or 'None'}</b></span>
        <div id="triggerStatus" style="display:none;margin-top:6px;padding:6px 10px;background:#0d1117;border-radius:6px;font-size:12px"></div>
        <div style="margin-top:12px">{price_html or '<span class="meta">Live prices loading...</span>'}</div>
      </div>

      <div class="card">
        <h3>📊 Kevin's Track Record <button class="help-btn" onclick="showHelp('track-record')">?</button></h3>
        {track_html}
      </div>

      <div class="card">
        <h3>📅 Earnings Calendar <button class="help-btn" onclick="showHelp('earnings')">?</button></h3>
        {_earn_html}
      </div>

      <div class="card">
        <h3>📐 Position Sizing <button class="help-btn" onclick="showHelp('position-sizing')">?</button></h3>
        <div class="grid" style="gap:10px">
          <div>
            <div class="meta" style="margin-bottom:4px">Account Size ($)</div>
            <input type="number" id="ps-account" value="25000" step="1000" oninput="calcPosition()" style="margin:0"/>
          </div>
          <div>
            <div class="meta" style="margin-bottom:4px">Risk per Trade (%)</div>
            <input type="number" id="ps-risk" value="2" step="0.5" min="0.1" max="20" oninput="calcPosition()" style="margin:0"/>
          </div>
        </div>
        <div class="grid" style="gap:10px;margin-top:8px">
          <div>
            <div class="meta" style="margin-bottom:4px">Entry Price ($)</div>
            <div style="display:flex;gap:6px">
              <input type="number" id="ps-entry" placeholder="22.50" step="0.01" oninput="calcPosition()" style="margin:0;flex:1"/>
              <input type="text"   id="ps-fill-ticker" placeholder="SOXL" style="margin:0;width:70px;padding:8px;font-size:13px"/>
              <button onclick="fillEntryFromPrice()" style="margin:0;width:50px;padding:8px;font-size:12px">Fill</button>
            </div>
          </div>
          <div>
            <div class="meta" style="margin-bottom:4px">Stop Loss ($)</div>
            <input type="number" id="ps-stop" placeholder="20.00" step="0.01" oninput="calcPosition()" style="margin:0"/>
          </div>
        </div>
        <div style="margin-top:8px">
          <div class="meta" style="margin-bottom:4px">Options Premium ($ per contract · optional)</div>
          <input type="number" id="ps-premium" placeholder="e.g. 1.50" step="0.01" oninput="calcPosition()" style="margin:0"/>
        </div>
        <div id="ps-result" style="margin-top:12px;padding:14px;background:#0d1117;border-radius:8px;border:1px solid #30363d;font-size:13px;line-height:1.8;min-height:44px">
          <span class="meta">Fill in the fields above to calculate position size.</span>
        </div>
      </div>

      <div class="grid">
        <div class="card">
          <h3>🤖 Ask Claude <button class="help-btn" onclick="showHelp('ask-claude')">?</button></h3>
          <input type="text" id="q" placeholder="Should I buy SOXL calls today?"/>
          <button onclick="ask()">Ask Claude</button>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px">
            <button class="chip" onclick="quickAsk('SOXL signal today?')">SOXL signal?</button>
            <button class="chip" onclick="quickAsk('What is the overall market sentiment right now?')">Market mood?</button>
            <button class="chip" onclick="quickAsk('What are Kevin\\'s top conviction picks this week?')">Top picks</button>
            <button class="chip" onclick="quickAsk('What stocks or sectors should I avoid right now?')">Avoid list</button>
            <button class="chip" onclick="quickAsk('Give me a week ahead outlook based on all reports.')">Week ahead</button>
          </div>
          <div id="ans"></div>
        </div>
        <div class="card">
          <h3>📈 Latest Analysis <button class="help-btn" onclick="showHelp('analysis')">?</button></h3>
          <div style="display:flex;justify-content:flex-end;margin-bottom:6px">
            <button onclick="copyAnalysis()" style="width:auto;padding:5px 12px;font-size:12px;background:#30363d;color:#8b949e;margin:0">📋 Copy</button>
          </div>
          <div class="analysis" id="analysisText">{analysis_html}</div>
        </div>
      </div>

      <div class="card">
        <h3>📰 Ticker News <button class="help-btn" onclick="showHelp('news')">?</button></h3>
        <div style="margin-bottom:10px">
          {news_badges}
        </div>
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <input type="text" id="news-ticker" placeholder="or type any ticker" style="margin:0;padding:8px"/>
          <button onclick="loadNews(document.getElementById('news-ticker').value)" style="width:90px;margin:0;padding:9px">Search</button>
        </div>
        <div id="news-feed" style="font-size:13px;color:#8b949e">Click a ticker above to load news.</div>
      </div>

      <div class="card">
        <h3>💼 Watchlist &amp; P&amp;L <button class="help-btn" onclick="showHelp('watchlist')">?</button></h3>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;align-items:center">
          <input type="text"   id="wl-ticker" placeholder="Ticker"  style="margin:0;width:75px;padding:8px;font-size:13px"/>
          <input type="number" id="wl-size"   placeholder="Qty"     style="margin:0;width:65px;padding:8px;font-size:13px" min="0" step="1"/>
          <input type="number" id="wl-entry"  placeholder="Entry $" style="margin:0;width:85px;padding:8px;font-size:13px" step="0.01"/>
          <select id="wl-type" style="padding:9px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;font-size:13px">
            <option value="stock">Shares</option>
            <option value="option">Contracts ×100</option>
          </select>
          <button onclick="addPosition()" style="margin:0;padding:9px 14px;font-size:13px">+ Add</button>
          <button onclick="loadWatchlist()" title="Refresh prices" style="margin:0;padding:9px;width:38px;font-size:15px;background:#161b22;color:#8b949e;border:1px solid #30363d">↻</button>
        </div>
        <div id="wl-table" class="scroll-x"></div>
        <div id="wl-total" style="margin-top:10px;font-size:14px;padding-top:8px;border-top:1px solid #30363d"></div>
      </div>

      <div class="card">
        <h3>📉 RSI &amp; MACD <button class="help-btn" onclick="showHelp('rsi-macd')">?</button></h3>
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <input type="text" id="ta-ticker" placeholder="SOXL" style="margin:0;flex:1;padding:8px"/>
          <button onclick="loadIndicators()" style="margin:0;width:100px;padding:9px">Analyze</button>
        </div>
        <div id="ta-result" style="font-size:13px;color:#8b949e">Enter a ticker and click Analyze.</div>
      </div>

      <div class="card">
        <h3>📋 Options Chain <button class="help-btn" onclick="showHelp('options-chain')">?</button></h3>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
          <input type="text" id="opt-ticker" placeholder="SOXL" style="width:90px;margin:0;padding:8px"/>
          <select id="opt-exp" style="flex:1;min-width:130px;padding:9px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;font-size:14px"></select>
          <button onclick="loadExpirations()" style="width:90px;margin:0;padding:9px">Load</button>
        </div>
        <div id="opt-table" class="scroll-x" style="font-size:12px;color:#8b949e">Enter a ticker and click Load.</div>
      </div>

      <div class="card">
        <h3>🌡️ Market Regime <button class="help-btn" onclick="showHelp('regime')">?</button></h3>
        <div id="regime-loading" style="color:#8b949e;font-size:13px">Loading...</div>
        <div id="regime-display" style="display:none">
          <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:10px">
            <div id="regime-badge" style="font-size:22px;font-weight:bold;white-space:nowrap"></div>
            <div style="flex:1">
              <div class="meta" style="margin-bottom:3px">SPY <span id="regime-spy" style="color:#e6edf3"></span> &nbsp;|&nbsp; from 200MA: <span id="regime-pct200"></span></div>
              <div class="meta" style="margin-bottom:3px">50MA: <span id="regime-ma50" style="color:#e6edf3"></span> &nbsp;|&nbsp; 200MA: <span id="regime-ma200" style="color:#e6edf3"></span></div>
              <div class="meta">20d Ann. Volatility: <span id="regime-vol"></span></div>
            </div>
          </div>
          <div id="regime-tip" style="font-size:12px;padding:7px 10px;border-radius:6px;margin-top:4px"></div>
        </div>
        <button onclick="loadRegime()" style="padding:6px 14px;font-size:12px;margin-top:8px">↻ Refresh</button>
      </div>

      <div class="card">
        <h3>📊 Kevin's Call Backtest <button class="help-btn" onclick="showHelp('backtest')">?</button></h3>
        <div id="backtest-display" style="color:#8b949e;font-size:13px">Loading...</div>
        <button onclick="loadBacktest()" style="padding:6px 14px;font-size:12px;margin-top:8px">↻ Refresh</button>
      </div>

      <div class="card">
        <h3>📡 Correlation &amp; Monte Carlo <button class="help-btn" onclick="showHelp('correlation')">?</button></h3>
        <div style="margin-bottom:14px">
          <div class="meta" style="margin-bottom:6px;font-weight:bold">SOXL / QQQ Rolling Correlation</div>
          <div id="corr-display" style="color:#8b949e;font-size:13px">Loading...</div>
        </div>
        <div>
          <div class="meta" style="margin-bottom:6px;font-weight:bold">Kevin's Signal Strength (Monte Carlo · 1,000 runs)</div>
          <div id="mc-display" style="color:#8b949e;font-size:13px">Loading...</div>
        </div>
        <button onclick="loadCorrelation();loadMonteCarlo()" style="padding:6px 14px;font-size:12px;margin-top:10px">↻ Refresh</button>
      </div>

      <script>
        function calcPosition() {{
          const account = parseFloat(document.getElementById('ps-account').value) || 0;
          const riskPct = parseFloat(document.getElementById('ps-risk').value)    || 0;
          const entry   = parseFloat(document.getElementById('ps-entry').value)   || 0;
          const stop    = parseFloat(document.getElementById('ps-stop').value)    || 0;
          const premium = parseFloat(document.getElementById('ps-premium').value) || 0;
          const el = document.getElementById('ps-result');

          if (!account || !riskPct || !entry || !stop) {{
            el.innerHTML = '<span class="meta">Fill in all fields above.</span>';
            return;
          }}
          const riskPerShare = Math.abs(entry - stop);
          if (riskPerShare === 0) {{ el.innerHTML = '<span style="color:#ff6b6b">Stop must differ from entry.</span>'; return; }}

          const dollarRisk = account * riskPct / 100;
          const shares     = Math.floor(dollarRisk / riskPerShare);
          const posValue   = shares * entry;
          const posPct     = (posValue / account * 100).toFixed(1);
          const direction  = entry > stop ? 'LONG' : 'SHORT';

          let html = '<span style="color:#00d4ff;font-weight:bold">' + direction + ' — Stocks</span><br>'
            + 'Shares: <b style="color:#e6edf3">' + shares.toLocaleString() + '</b>'
            + ' &nbsp;·&nbsp; Position value: <b style="color:#e6edf3">$' + posValue.toLocaleString(undefined, {{maximumFractionDigits:0}}) + '</b>'
            + ' <span class="meta">(' + posPct + '% of account)</span>'
            + '<br>Risk/share: <b style="color:#ff6b6b">$' + riskPerShare.toFixed(2) + '</b>'
            + ' &nbsp;·&nbsp; Total at risk: <b style="color:#ff6b6b">$' + dollarRisk.toLocaleString(undefined, {{maximumFractionDigits:0}}) + '</b>';

          if (premium > 0) {{
            const contracts = Math.floor(dollarRisk / (premium * 100));
            const optCost   = (contracts * premium * 100).toFixed(0);
            html += '<br><br><span style="color:#b388ff;font-weight:bold">Options</span><br>'
              + 'Contracts: <b style="color:#e6edf3">' + contracts + '</b>'
              + ' &nbsp;·&nbsp; Total premium paid: <b style="color:#e6edf3">$' + parseFloat(optCost).toLocaleString() + '</b>'
              + '<br><span class="meta">Max loss = full premium if call expires worthless</span>';
          }}
          el.innerHTML = html;
          localStorage.setItem('ps-account', account);
          localStorage.setItem('ps-risk', riskPct);
        }}

        async function fillEntryFromPrice() {{
          const ticker = (document.getElementById('ps-fill-ticker').value || 'SOXL').toUpperCase();
          try {{
            const r = await fetch('/prices');
            const d = await r.json();
            if (d[ticker] && d[ticker].price) {{
              document.getElementById('ps-entry').value = d[ticker].price;
              calcPosition();
            }} else {{
              alert(ticker + ' not found in live prices. Check ticker symbol.');
            }}
          }} catch(e) {{ alert('Error fetching price: ' + e.message); }}
        }}

        // Restore saved account/risk from last session
        (function() {{
          const a = localStorage.getItem('ps-account');
          const r = localStorage.getItem('ps-risk');
          if (a) document.getElementById('ps-account').value = a;
          if (r) document.getElementById('ps-risk').value = r;
        }})();

        async function loadWatchlist() {{
          const el    = document.getElementById('wl-table');
          const totEl = document.getElementById('wl-total');
          let positions = [];
          try {{
            const r = await fetch('/watchlist-server');
            positions = await r.json();
          }} catch(e) {{ el.innerHTML = '<span class="meta">Error loading watchlist.</span>'; return; }}
          if (!positions.length) {{
            el.innerHTML = '<span class="meta">No positions yet. Enter ticker, qty, entry price above and click + Add.</span>';
            totEl.innerHTML = '';
            return;
          }}
          const uniq = [];
          positions.forEach(function(p) {{ if (!uniq.includes(p.ticker)) uniq.push(p.ticker); }});
          let px = {{}};
          try {{
            const r = await fetch('/prices?tickers=' + uniq.join(','));
            px = await r.json();
          }} catch(e) {{}}

          const th = 'padding:5px 8px;color:#8b949e;border-bottom:1px solid #30363d';
          let html = '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            + '<tr>'
            + '<th style="' + th + ';text-align:left">Ticker</th>'
            + '<th style="' + th + ';text-align:right">Size</th>'
            + '<th style="' + th + ';text-align:right">Entry</th>'
            + '<th style="' + th + ';text-align:right">Now</th>'
            + '<th style="' + th + ';text-align:right">P&amp;L $</th>'
            + '<th style="' + th + ';text-align:right">P&amp;L %</th>'
            + '<th style="' + th + '"></th>'
            + '</tr>';

          let totalPnl = 0;
          let allPriced = true;
          positions.forEach(function(pos) {{
            const data   = px[pos.ticker];
            const now    = data && data.price ? data.price : null;
            const entry  = parseFloat(pos.entry);
            const size   = parseFloat(pos.size);
            const mult   = pos.type === 'option' ? 100 : 1;
            const sLabel = pos.type === 'option' ? size + ' cts' : size + ' sh';
            let pnlHtml = '<span class="meta">—</span>';
            let pctHtml = '';
            if (now) {{
              const pnl = (now - entry) * size * mult;
              const pct = (now - entry) / entry * 100;
              totalPnl += pnl;
              const col = pnl >= 0 ? '#00ff88' : '#ff6b6b';
              const arr = pnl >= 0 ? '▲' : '▼';
              pnlHtml = '<b style="color:' + col + '">' + (pnl >= 0 ? '+' : '-') + '$' + Math.abs(pnl).toFixed(0) + '</b>';
              pctHtml = '<span style="color:' + col + '">' + arr + Math.abs(pct).toFixed(1) + '%</span>';
            }} else {{ allPriced = false; }}
            html += '<tr>'
              + '<td style="padding:5px 8px;font-weight:bold;color:#00d4ff">' + pos.ticker + '</td>'
              + '<td style="padding:5px 8px;text-align:right;color:#c9d1d9">' + sLabel + '</td>'
              + '<td style="padding:5px 8px;text-align:right;color:#c9d1d9">$' + entry.toFixed(2) + '</td>'
              + '<td style="padding:5px 8px;text-align:right;color:#e6edf3">' + (now ? '$' + now : '<span class="meta">—</span>') + '</td>'
              + '<td style="padding:5px 8px;text-align:right">' + pnlHtml + '</td>'
              + '<td style="padding:5px 8px;text-align:right">' + pctHtml + '</td>'
              + '<td style="padding:5px 8px;text-align:right">'
              + '<span style="cursor:pointer;color:#8b949e;font-size:18px;line-height:1" onclick="removePosition(' + pos.id + ')">×</span>'
              + '</td>'
              + '</tr>';
          }});
          html += '</table>';
          el.innerHTML = html;
          if (positions.length) {{
            const col  = totalPnl >= 0 ? '#00ff88' : '#ff6b6b';
            const sign = totalPnl >= 0 ? '+' : '-';
            totEl.innerHTML = 'Total P&amp;L: <b style="color:' + col + '">' + sign + '$' + Math.abs(totalPnl).toFixed(0) + '</b>'
              + (allPriced ? '' : ' <span class="meta">(some prices unavailable)</span>');
          }}
        }}

        async function addPosition() {{
          const ticker = document.getElementById('wl-ticker').value.trim().toUpperCase();
          const size   = parseFloat(document.getElementById('wl-size').value);
          const entry  = parseFloat(document.getElementById('wl-entry').value);
          const type   = document.getElementById('wl-type').value;
          if (!ticker || !size || !entry) {{ alert('Ticker, quantity, and entry price are all required.'); return; }}
          await fetch('/watchlist-server', {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{ticker, size, entry, type}})
          }});
          document.getElementById('wl-ticker').value = '';
          document.getElementById('wl-size').value   = '';
          document.getElementById('wl-entry').value  = '';
          loadWatchlist();
        }}

        async function removePosition(id) {{
          await fetch('/watchlist-server/' + id, {{method: 'DELETE'}});
          loadWatchlist();
        }}

        async function toggleHistory() {{
          const panel = document.getElementById('historyPanel');
          if (panel.style.display === 'none') {{
            panel.style.display = 'block';
            const r = await fetch('/my-history?limit=30');
            const items = await r.json();
            if (!items.length) {{
              document.getElementById('historyContent').innerHTML = '<span class="meta">No activity yet.</span>';
              return;
            }}
            const icons = {{options:'📋',news:'📰',rsi:'📉',bars:'📉',ask:'🤖',prices:'💰'}};
            document.getElementById('historyContent').innerHTML = items.map(function(h) {{
              const ic = icons[h.action] || '•';
              return '<div style="padding:5px 0;border-bottom:1px solid #30363d">'
                + '<span style="color:#8b949e">' + h.created_at.slice(0,16) + '</span> '
                + ic + ' <b style="color:#e6edf3">' + h.action.toUpperCase() + '</b>'
                + (h.ticker ? ' <span style="color:#00d4ff">' + h.ticker + '</span>' : '')
                + (h.query  ? ' <span style="color:#c9d1d9">' + h.query + '</span>' : '')
                + (h.summary? '<br><span style="color:#8b949e;font-size:11px">' + h.summary + '</span>' : '')
                + '</div>';
            }}).join('');
          }} else {{
            panel.style.display = 'none';
          }}
        }}

        loadWatchlist();

        function calcEMA(data, period) {{
          const k = 2 / (period + 1);
          const ema = [data.slice(0, period).reduce(function(s, v) {{ return s + v; }}, 0) / period];
          for (let i = period; i < data.length; i++) {{
            ema.push(data[i] * k + ema[ema.length - 1] * (1 - k));
          }}
          return ema;
        }}

        function calcRSI(closes, period) {{
          const deltas = closes.slice(1).map(function(c, i) {{ return c - closes[i]; }});
          let avgGain = deltas.slice(0, period).filter(function(d) {{ return d > 0; }})
                          .reduce(function(s, d) {{ return s + d; }}, 0) / period;
          let avgLoss = deltas.slice(0, period).filter(function(d) {{ return d < 0; }})
                          .reduce(function(s, d) {{ return s + Math.abs(d); }}, 0) / period;
          const rsi = [];
          rsi.push(100 - 100 / (1 + (avgLoss === 0 ? 1e9 : avgGain / avgLoss)));
          for (let i = period; i < deltas.length; i++) {{
            const g = deltas[i] > 0 ? deltas[i] : 0;
            const l = deltas[i] < 0 ? Math.abs(deltas[i]) : 0;
            avgGain = (avgGain * (period - 1) + g) / period;
            avgLoss = (avgLoss * (period - 1) + l) / period;
            rsi.push(100 - 100 / (1 + (avgLoss === 0 ? 1e9 : avgGain / avgLoss)));
          }}
          return rsi;
        }}

        function calcMACD(closes, fast, slow, sig) {{
          const eFast = calcEMA(closes, fast);
          const eSlow = calcEMA(closes, slow);
          const offset = eFast.length - eSlow.length;
          const macd   = eSlow.map(function(v, i) {{ return eFast[i + offset] - v; }});
          const signal = calcEMA(macd, sig);
          const hOff   = macd.length - signal.length;
          const hist   = signal.map(function(v, i) {{ return macd[i + hOff] - v; }});
          return {{ macd: macd.slice(-hist.length), signal: signal, hist: hist }};
        }}

        async function loadIndicators() {{
          const ticker = (document.getElementById('ta-ticker').value || 'SOXL').toUpperCase();
          const el = document.getElementById('ta-result');
          el.innerText = 'Fetching bars for ' + ticker + '...';
          try {{
            const r = await fetch('/bars?ticker=' + ticker + '&limit=60');
            const d = await r.json();
            if (d.error || !d.bars || d.bars.length < 35) {{
              el.innerText = d.error || 'Not enough data (need 35+ bars).';
              return;
            }}
            const closes = d.bars.map(function(b) {{ return b.c; }});
            const rsiArr  = calcRSI(closes, 14);
            const macdRes = calcMACD(closes, 12, 26, 9);
            const rsi  = rsiArr[rsiArr.length - 1];
            const macd = macdRes.macd[macdRes.macd.length - 1];
            const sig  = macdRes.signal[macdRes.signal.length - 1];
            const hist = macdRes.hist[macdRes.hist.length - 1];
            const phist= macdRes.hist[macdRes.hist.length - 2];

            let rsiLabel, rsiCol;
            if      (rsi < 30) {{ rsiLabel = 'Oversold — watch for a bounce entry';       rsiCol = '#00ff88'; }}
            else if (rsi < 45) {{ rsiLabel = 'Recovering from oversold';                   rsiCol = '#7ec8e3'; }}
            else if (rsi < 55) {{ rsiLabel = 'Neutral — no strong edge either way';        rsiCol = '#8b949e'; }}
            else if (rsi < 70) {{ rsiLabel = 'Bullish momentum — trend is healthy';        rsiCol = '#00d4ff'; }}
            else if (rsi < 80) {{ rsiLabel = 'Overbought — wait for pullback before entry';rsiCol = '#ff9800'; }}
            else               {{ rsiLabel = 'Strongly overbought — high reversal risk';   rsiCol = '#ff6b6b'; }}

            let macdLabel, macdCol;
            const xUp   = hist > 0 && phist <= 0;
            const xDown = hist < 0 && phist >= 0;
            if      (xUp)               {{ macdLabel = '🚀 Bullish crossover — momentum just flipped up';    macdCol = '#00ff88'; }}
            else if (xDown)             {{ macdLabel = '⚠️ Bearish crossover — momentum just flipped down';  macdCol = '#ff6b6b'; }}
            else if (hist > 0 && macd > 0) {{ macdLabel = 'Strong bullish — above signal and zero line';    macdCol = '#00d4ff'; }}
            else if (hist > 0 && macd <= 0){{ macdLabel = 'Recovering — above signal but below zero';       macdCol = '#7ec8e3'; }}
            else if (hist < 0 && macd > 0) {{ macdLabel = 'Weakening — below signal, still above zero';     macdCol = '#ff9800'; }}
            else                           {{ macdLabel = 'Bearish — below signal and zero line';            macdCol = '#ff6b6b'; }}

            const rsiW  = Math.min(Math.max(rsi, 0), 100).toFixed(0);
            const rsiBarCol = rsi < 30 ? '#00ff88' : rsi > 70 ? '#ff6b6b' : '#00d4ff';
            const histCol   = hist >= 0 ? '#00ff88' : '#ff6b6b';
            const histSign  = hist >= 0 ? '+' : '';

            el.innerHTML =
              '<div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:12px">'
              + '<div>'
              +   '<div class="meta" style="margin-bottom:4px">RSI (14-period)</div>'
              +   '<div style="font-size:32px;font-weight:bold;color:' + rsiCol + '">' + rsi.toFixed(1) + '</div>'
              +   '<div style="background:#0d1117;border-radius:4px;height:8px;margin:6px 0;position:relative">'
              +     '<div style="position:absolute;left:30%;width:1px;height:100%;background:#30363d"></div>'
              +     '<div style="position:absolute;left:70%;width:1px;height:100%;background:#30363d"></div>'
              +     '<div style="background:' + rsiBarCol + ';width:' + rsiW + '%;height:100%;border-radius:4px"></div>'
              +   '</div>'
              +   '<div class="meta" style="font-size:10px">0 ←  30(OS) ·· 70(OB)  → 100</div>'
              +   '<div style="color:' + rsiCol + ';font-size:12px;margin-top:6px">' + rsiLabel + '</div>'
              + '</div>'
              + '<div>'
              +   '<div class="meta" style="margin-bottom:4px">MACD (12·26·9)</div>'
              +   '<div style="font-size:13px;margin-bottom:6px">'
              +     '<span style="color:#00d4ff">MACD&nbsp;' + macd.toFixed(3) + '</span>'
              +     '&nbsp;·&nbsp;<span style="color:#ff9800">Sig&nbsp;' + sig.toFixed(3) + '</span>'
              +     '&nbsp;·&nbsp;<span style="color:' + histCol + '">Hist&nbsp;' + histSign + hist.toFixed(3) + '</span>'
              +   '</div>'
              +   '<div style="color:' + macdCol + ';font-size:12px">' + macdLabel + '</div>'
              + '</div>'
              + '</div>'
              + '<div style="padding:8px 12px;background:#0d1117;border-radius:8px;font-size:11px;color:#8b949e">'
              +   closes.length + ' daily bars · Last close <b style="color:#e6edf3">$' + closes[closes.length-1].toFixed(2)
              +   '</b> on ' + d.bars[d.bars.length-1].t
              + '</div>';
          }} catch(e) {{ el.innerText = 'Error: ' + e.message; }}
        }}

        async function loadExpirations() {{
          const ticker = (document.getElementById('opt-ticker').value || 'SOXL').toUpperCase();
          document.getElementById('opt-table').innerText = 'Fetching expirations...';
          try {{
            const r = await fetch('/options/expirations?ticker=' + ticker);
            const d = await r.json();
            const sel = document.getElementById('opt-exp');
            sel.innerHTML = (d.expirations || []).map(e => `<option value="${{e}}">${{e}}</option>`).join('');
            if (d.expirations && d.expirations.length) loadChain();
            else document.getElementById('opt-table').innerText = 'No expirations found for ' + ticker;
          }} catch(e) {{ document.getElementById('opt-table').innerText = 'Error: ' + e.message; }}
        }}

        async function loadChain() {{
          const ticker = (document.getElementById('opt-ticker').value || 'SOXL').toUpperCase();
          const exp = document.getElementById('opt-exp').value;
          if (!exp) return;
          document.getElementById('opt-table').innerText = 'Loading chain...';
          try {{
            const r = await fetch('/options?ticker=' + ticker + '&expiration=' + exp);
            const d = await r.json();
            if (d.error) {{ document.getElementById('opt-table').innerText = d.error; return; }}
            const th = 'padding:4px 8px;color:#8b949e;border-bottom:1px solid #30363d;text-align:right';
            const td = 'padding:3px 8px;text-align:right';
            let html = `<table style="width:100%;border-collapse:collapse">
              <tr>
                <th style="${{th}};color:#00ff88">Last</th>
                <th style="${{th}};color:#00ff88">Bid</th>
                <th style="${{th}};color:#00ff88">Ask</th>
                <th style="${{th}};color:#00ff88">Vol</th>
                <th style="${{th}};color:#00ff88">OI</th>
                <th style="${{th}};text-align:center">Strike</th>
                <th style="${{th}};color:#ff6b6b">OI</th>
                <th style="${{th}};color:#ff6b6b">Vol</th>
                <th style="${{th}};color:#ff6b6b">Bid</th>
                <th style="${{th}};color:#ff6b6b">Ask</th>
                <th style="${{th}};color:#ff6b6b">Last</th>
              </tr>`;
            const rows = Math.max(d.calls.length, d.puts.length);
            for (let i = 0; i < rows; i++) {{
              const c = d.calls[i] || {{}};
              const p = d.puts[i]  || {{}};
              const strike = c.strike || p.strike || '';
              html += `<tr>
                <td style="${{td}}">${{c.last ?? '-'}}</td>
                <td style="${{td}}">${{c.bid  ?? '-'}}</td>
                <td style="${{td}}">${{c.ask  ?? '-'}}</td>
                <td style="${{td}}">${{c.volume ?? '-'}}</td>
                <td style="${{td}}">${{c.open_interest ?? '-'}}</td>
                <td style="${{td}};font-weight:bold;color:#e6edf3;text-align:center">${{strike}}</td>
                <td style="${{td}}">${{p.open_interest ?? '-'}}</td>
                <td style="${{td}}">${{p.volume ?? '-'}}</td>
                <td style="${{td}}">${{p.bid  ?? '-'}}</td>
                <td style="${{td}}">${{p.ask  ?? '-'}}</td>
                <td style="${{td}}">${{p.last ?? '-'}}</td>
              </tr>`;
            }}
            html += '</table>';
            document.getElementById('opt-table').innerHTML = html;
          }} catch(e) {{ document.getElementById('opt-table').innerText = 'Error: ' + e.message; }}
        }}

        document.getElementById('opt-exp').addEventListener('change', loadChain);

        async function loadNews(ticker) {{
          if (!ticker) return;
          ticker = ticker.toUpperCase();
          document.getElementById('news-ticker').value = ticker;
          document.getElementById('news-feed').innerHTML = 'Loading news for ' + ticker + '...';
          try {{
            const r = await fetch('/news?ticker=' + ticker);
            const d = await r.json();
            if (!d.news || !d.news.length) {{
              document.getElementById('news-feed').innerText = 'No news found for ' + ticker;
              return;
            }}
            document.getElementById('news-feed').innerHTML = d.news.map(n => `
              <div style="border-bottom:1px solid #30363d;padding:10px 0">
                <a href="${{n.url}}" target="_blank" style="color:#00d4ff;text-decoration:none;font-weight:bold;font-size:13px">${{n.title}}</a>
                <div style="color:#8b949e;font-size:11px;margin-top:3px">${{n.source}} &nbsp;·&nbsp; ${{n.published}}</div>
                <div style="color:#c9d1d9;font-size:12px;margin-top:4px">${{n.summary}}</div>
              </div>`).join('');
          }} catch(e) {{ document.getElementById('news-feed').innerText = 'Error: ' + e.message; }}
        }}

        async function ask() {{
          const q = document.getElementById('q').value;
          if (!q) return;
          document.getElementById('ans').innerText = 'Claude is thinking...';
          try {{
            const r = await fetch('/ask', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{question: q}})
            }});
            const d = await r.json();
            document.getElementById('ans').innerText = d.answer || d.detail || 'No response';
          }} catch(e) {{
            document.getElementById('ans').innerText = 'Error: ' + e.message;
          }}
        }}
        document.getElementById('q').addEventListener('keypress', function(e) {{
          if (e.key === 'Enter') ask();
        }});

        let _statusPoll = null;

        async function runAnalysis() {{
          const btn = document.getElementById('runBtn');
          const status = document.getElementById('triggerStatus');
          btn.disabled = true;
          btn.innerText = '⏳ Starting...';
          btn.style.background = '#ff9800';
          status.style.display = 'block';
          status.style.color = '#ff9800';
          status.innerText = 'Requesting analysis...';
          try {{
            const r = await fetch('/trigger-analysis', {{method:'POST'}});
            const d = await r.json();
            if (d.status === 'already_running') {{
              status.innerText = '⚠️ Analysis already running — started ' + (d.started || '');
              btn.innerText = '⏳ Running...';
              _pollStatus();
              return;
            }}
            btn.innerText = '⏳ Analyzing...';
            status.innerText = '🔄 Claude is analyzing the latest report... (takes ~30–60 sec)';
            _pollStatus();
          }} catch(e) {{
            btn.disabled = false; btn.innerText = '▶ Run Now'; btn.style.background = '#00ff88';
            status.style.color = '#ff6b6b';
            status.innerText = '❌ Failed to start: ' + e.message;
          }}
        }}

        function _pollStatus() {{
          if (_statusPoll) clearInterval(_statusPoll);
          _statusPoll = setInterval(async function() {{
            try {{
              const r = await fetch('/analysis-status');
              const d = await r.json();
              const btn = document.getElementById('runBtn');
              const status = document.getElementById('triggerStatus');
              if (!d.running) {{
                clearInterval(_statusPoll);
                btn.disabled = false; btn.innerText = '▶ Run Now'; btn.style.background = '#00ff88';
                if (d.error) {{
                  status.style.color = '#ff6b6b';
                  status.innerText = '❌ Error: ' + d.error;
                }} else {{
                  status.style.color = '#00ff88';
                  status.innerText = '✅ Done at ' + (d.finished || '') + ' — ' + (d.result || 'Analysis saved. Reload to see it.');
                  // Reload the analysis section without full page reload
                  setTimeout(function() {{ window.location.reload(); }}, 2500);
                }}
              }} else {{
                status.innerText = '🔄 Claude is analyzing... started ' + (d.started || '');
              }}
            }} catch(e) {{ clearInterval(_statusPoll); }}
          }}, 4000);
        }}

        // Resume polling if analysis was already running when page loaded
        (async function() {{
          try {{
            const r = await fetch('/analysis-status');
            const d = await r.json();
            if (d.running) {{
              const btn = document.getElementById('runBtn');
              const status = document.getElementById('triggerStatus');
              btn.disabled = true; btn.innerText = '⏳ Analyzing...'; btn.style.background = '#ff9800';
              status.style.display = 'block'; status.style.color = '#ff9800';
              status.innerText = '🔄 Analysis in progress (started ' + (d.started || '') + ')';
              _pollStatus();
            }}
          }} catch(e) {{}}
        }})();

        function quickAsk(q) {{
          document.getElementById('q').value = q;
          document.getElementById('q').scrollIntoView({{behavior:'smooth',block:'center'}});
          ask();
        }}

        function copyAnalysis() {{
          const el = document.getElementById('analysisText');
          const text = el ? el.innerText : '';
          if (!text) return;
          navigator.clipboard.writeText(text).then(function() {{
            const btn = event.target;
            const orig = btn.innerText;
            btn.innerText = '✅ Copied!';
            setTimeout(function() {{ btn.innerText = orig; }}, 2000);
          }}).catch(function() {{ alert('Copy failed — select text manually'); }});
        }}

        // ── Item 4: Market Regime ──────────────────────────────────────────
        async function loadRegime() {{
          document.getElementById('regime-loading').style.display = 'block';
          document.getElementById('regime-display').style.display = 'none';
          try {{
            const d = await (await fetch('/regime')).json();
            if (d.error) {{ document.getElementById('regime-loading').innerText = '⚠️ ' + d.error; return; }}
            document.getElementById('regime-loading').style.display = 'none';
            document.getElementById('regime-display').style.display = 'block';
            const colors = {{'BULL':'#00ff88','CRASH':'#ff6b6b','BEAR':'#ff9800','CHOPPY':'#ffd700','NEUTRAL':'#ffd700'}};
            const c = colors[d.regime] || '#8b949e';
            document.getElementById('regime-badge').innerHTML = '<span style="color:' + c + '">' + d.icon + ' ' + d.regime + '</span>';
            document.getElementById('regime-spy').innerText = '$' + d.spy;
            const p = d.pct_from_200;
            document.getElementById('regime-pct200').innerHTML =
              '<span style="color:' + (p >= 0 ? '#00ff88' : '#ff6b6b') + '">' + (p >= 0 ? '+' : '') + p + '%</span>';
            document.getElementById('regime-ma50').innerText  = '$' + d.ma50;
            document.getElementById('regime-ma200').innerText = '$' + d.ma200;
            const vc = d.ann_vol > 35 ? '#ff6b6b' : d.ann_vol > 20 ? '#ffd700' : '#00ff88';
            document.getElementById('regime-vol').innerHTML = '<span style="color:' + vc + '">' + d.ann_vol + '%</span>';
            const tips = {{
              'BULL':    ['background:#00ff8822;border:1px solid #00ff88', '🟢 Normal sizing. Favor calls on Kevin picks.'],
              'NEUTRAL': ['background:#ffd70022;border:1px solid #ffd700', '🟡 Neutral. Standard sizing, wait for clear signals.'],
              'CHOPPY':  ['background:#ffd70022;border:1px solid #ffd700', '🟡 Choppy. Reduce position size, avoid over-trading.'],
              'BEAR':    ['background:#ff980022;border:1px solid #ff9800', '🟠 Bear market. Defensive posture, tighten stops.'],
              'CRASH':   ['background:#ff6b6b22;border:1px solid #ff6b6b', '🔴 Crash regime. Protect capital. Consider puts or cash.'],
            }};
            const [tipStyle, tipText] = tips[d.regime] || ['', ''];
            document.getElementById('regime-tip').setAttribute('style', tipStyle + ';padding:7px 10px;border-radius:6px;margin-top:4px;font-size:12px;color:#e6edf3');
            document.getElementById('regime-tip').innerText = tipText;
          }} catch(e) {{ document.getElementById('regime-loading').innerText = 'Load failed: ' + e; }}
        }}

        // ── Item 2: Kevin's Call Backtest ──────────────────────────────────
        async function loadBacktest() {{
          document.getElementById('backtest-display').innerText = 'Loading...';
          try {{
            const d = await (await fetch('/backtest')).json();
            if (!d.trades || d.trades.length === 0) {{
              document.getElementById('backtest-display').innerHTML =
                '<span class="meta">No closed trades yet. Add HIT/MISS entries to track_record.txt on Synology.</span>';
              return;
            }}
            const s = d.summary;
            const avgC = s.avg_return >= 0 ? '#00ff88' : '#ff6b6b';
            let html = '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px">'
              + '<span class="meta">Trades: <b style="color:#e6edf3">' + s.trade_count + '</b></span>'
              + '<span class="meta">Hit rate: <b style="color:#00ff88">' + s.hit_rate + '%</b></span>'
              + '<span class="meta">Avg return: <b style="color:' + avgC + '">' + (s.avg_return >= 0 ? '+' : '') + s.avg_return + '%</b></span>'
              + '<span class="meta">Best: <b style="color:#00ff88">+' + s.best + '%</b></span>'
              + '<span class="meta">Worst: <b style="color:#ff6b6b">' + s.worst + '%</b></span>'
              + '</div>'
              + '<div class="scroll-x"><table style="width:100%;border-collapse:collapse;font-size:11px">'
              + '<tr>' + ['Date','Ticker','Call','Entry','Target','Expected','Outcome'].map(function(h) {{
                  return '<th style="text-align:left;padding:3px 6px;color:#8b949e;border-bottom:1px solid #30363d">' + h + '</th>';
                }}).join('') + '</tr>';
            d.trades.forEach(function(t) {{
              const rc  = t.expected_return !== null ? (t.expected_return >= 0 ? '#00ff88' : '#ff6b6b') : '#8b949e';
              const oc  = t.outcome === 'HIT' ? '#00ff88' : '#ff6b6b';
              const ret = t.expected_return !== null ? ((t.expected_return >= 0 ? '+' : '') + t.expected_return + '%') : '-';
              html += '<tr>'
                + '<td style="padding:3px 6px;color:#8b949e">'              + t.date            + '</td>'
                + '<td style="padding:3px 6px;color:#00d4ff;font-weight:bold">' + t.ticker     + '</td>'
                + '<td style="padding:3px 6px;color:#c9d1d9">'              + t.call            + '</td>'
                + '<td style="padding:3px 6px;color:#c9d1d9">$'             + (t.entry  || '-') + '</td>'
                + '<td style="padding:3px 6px;color:#c9d1d9">$'             + (t.target || '-') + '</td>'
                + '<td style="padding:3px 6px;color:' + rc + '">'           + ret               + '</td>'
                + '<td style="padding:3px 6px"><span style="background:' + oc
                +    ';color:#000;padding:1px 6px;border-radius:6px;font-size:10px;font-weight:bold">'
                +    t.outcome + '</span></td>'
                + '</tr>';
            }});
            html += '</table></div>';
            document.getElementById('backtest-display').innerHTML = html;
          }} catch(e) {{ document.getElementById('backtest-display').innerText = 'Load failed: ' + e; }}
        }}

        // ── Item 5: Correlation Break Detector ────────────────────────────
        async function loadCorrelation() {{
          document.getElementById('corr-display').innerText = 'Loading...';
          try {{
            const d = await (await fetch('/correlation')).json();
            if (d.error) {{ document.getElementById('corr-display').innerText = '⚠️ ' + d.error; return; }}
            const cc = Math.abs(d.current_corr) > 0.7 ? '#00ff88' : Math.abs(d.current_corr) > 0.4 ? '#ffd700' : '#ff6b6b';
            const dc = d.break_detected ? '#ff6b6b' : '#00ff88';
            let html = '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">'
              + '<span class="meta">20-day corr: <b style="color:' + cc + '">' + d.current_corr + '</b></span>'
              + '<span class="meta">Baseline (' + d.baseline_days + 'd): <b style="color:#e6edf3">' + d.baseline_corr + '</b></span>'
              + '<span class="meta">Δ: <b style="color:' + dc + '">' + (d.delta >= 0 ? '+' : '') + d.delta + '</b></span>'
              + '</div>';
            if (d.break_detected) {{
              html += '<div style="background:#ff6b6b22;border:1px solid #ff6b6b;border-radius:6px;'
                   +  'padding:8px;font-size:12px;color:#ff6b6b">'
                   +  '⚠️ Correlation break — SOXL is moving differently from QQQ. '
                   +  'Possible sector rotation or volatility spike. Reduce SOXL size until normal.</div>';
            }} else {{
              html += '<div style="color:#8b949e;font-size:12px">✅ Normal — SOXL tracking QQQ as expected.</div>';
            }}
            document.getElementById('corr-display').innerHTML = html;
          }} catch(e) {{ document.getElementById('corr-display').innerText = 'Load failed: ' + e; }}
        }}

        // ── Item 6: Monte Carlo Signal Strength ───────────────────────────
        async function loadMonteCarlo() {{
          document.getElementById('mc-display').innerText = 'Running 1,000 simulations...';
          try {{
            const d = await (await fetch('/monte-carlo')).json();
            if (d.message) {{ document.getElementById('mc-display').innerHTML = '<span class="meta">' + d.message + '</span>'; return; }}
            if (d.error)   {{ document.getElementById('mc-display').innerText = '⚠️ ' + d.error;   return; }}
            const pc = d.prob_profit >= 70 ? '#00ff88' : d.prob_profit >= 50 ? '#ffd700' : '#ff6b6b';
            const rc = d.median_return >= 0 ? '#00ff88' : '#ff6b6b';
            let html = '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px">'
              + '<span class="meta">Trades: <b style="color:#e6edf3">' + d.trade_count + '</b></span>'
              + '<span class="meta">P(profit): <b style="color:' + pc + '">' + d.prob_profit + '%</b></span>'
              + '<span class="meta">Median: <b style="color:' + rc + '">' + (d.median_return >= 0 ? '+' : '') + d.median_return + '%</b></span>'
              + '<span class="meta">Worst 5%: <b style="color:#ff6b6b">' + d.worst_5pct + '%</b></span>'
              + '<span class="meta">Best 5%: <b style="color:#00ff88">+' + d.best_5pct + '%</b></span>'
              + '</div>';
            const strength = d.prob_profit >= 65 ? '🟢 Strong signal' : d.prob_profit >= 50 ? '🟡 Moderate signal' : '🔴 Weak signal';
            html += '<div style="font-size:12px;color:#8b949e">' + strength
              + ' — Kevin calls are profitable in ' + d.prob_profit + '% of simulated trade sequences.</div>';
            document.getElementById('mc-display').innerHTML = html;
          }} catch(e) {{ document.getElementById('mc-display').innerText = 'Load failed: ' + e; }}
        }}

        // Auto-load new sections on page load
        loadRegime();
        loadBacktest();
        loadCorrelation();
        loadMonteCarlo();
      </script>
      <div class="quick-bar" id="quickBar">
        <span class="chip" onclick="quickAsk('SOXL signal today — RSI, MACD, and Kevin conviction?')">⚡ SOXL</span>
        <span class="chip" onclick="quickAsk('Market mood and top 3 actions from latest report?')">📊 Mood</span>
        <span class="chip" onclick="quickAsk('Kevin top picks this week with conviction level?')">🎯 Picks</span>
        <span class="chip" onclick="quickAsk('Stocks to avoid right now and why?')">🚫 Avoid</span>
        <span class="chip" onclick="quickAsk('Options strategy for this week based on reports?')">📋 Options</span>
        <span class="chip" onclick="document.getElementById('ta-ticker').value='SOXL';loadIndicators()">📉 RSI/MACD</span>
        <span class="chip" onclick="window.scrollTo(0,0)">⬆️ Top</span>
      </div>

      <div class="modal-overlay" id="helpModal" onclick="if(event.target===this)closeHelp()">
        <div class="modal-box">
          <button class="modal-close" onclick="closeHelp()">&#215;</button>
          <div class="modal-title" id="helpTitle"></div>
          <div class="modal-section">
            <div class="modal-label">What it is</div>
            <div class="modal-text" id="helpWhat"></div>
          </div>
          <div class="modal-section">
            <div class="modal-label">How to use it</div>
            <div class="modal-text" id="helpHow"></div>
          </div>
          <div class="modal-tip" id="helpTip"></div>
          <div class="modal-links" id="helpLinks"></div>
        </div>
      </div>

      <script>
        const HELP = {_help_json};

        function showHelp(id) {{
          const h = HELP[id];
          if (!h) return;
          document.getElementById('helpTitle').innerText = h.title;
          document.getElementById('helpWhat').innerText  = h.what;
          document.getElementById('helpHow').innerText   = h.how;
          document.getElementById('helpTip').innerText   = '💡 Pro tip: ' + h.tip;
          document.getElementById('helpLinks').innerHTML = (h.links || [])
            .map(function(l) {{ return '<a href="' + l.url + '" target="_blank" rel="noopener">&#128218; ' + l.label + '</a>'; }})
            .join('');
          document.getElementById('helpModal').classList.add('open');
        }}
        function closeHelp() {{ document.getElementById('helpModal').classList.remove('open'); }}
        document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeHelp(); }});
      </script>
    </body>
    </html>"""

@app.post("/ask")
async def ask(query: Query, request: Request):
    pid = current_profile_id(request)
    reports = get_all_reports()
    if not reports:
        raise HTTPException(status_code=404, detail="No reports found.")

    prices = get_live_prices(query.tickers)
    price_context = json.dumps(prices, indent=2) if prices else "Unavailable"

    history      = build_history_context(reports)
    latest_name, latest_content = reports[-1]["name"], reports[-1]["content"]

    trades = get_track_record()
    if trades:
        closed   = [t for t in trades if t["outcome"] in ("HIT", "MISS")]
        hits     = [t for t in closed if t["outcome"] == "HIT"]
        hit_rate = round(len(hits) / len(closed) * 100) if closed else 0
        trade_lines = "\n".join(
            f"- {t['date']} | {t['ticker']} | {t['call']} | entry:{t['entry'] or '?'} "
            f"target:{t['target'] or '?'} | {t['outcome']} | {t['notes']}"
            for t in trades[-15:]
        )
        track_context = (
            f"KEVIN'S HISTORICAL CALLS ({hit_rate}% hit rate, {len(closed)} closed trades):\n"
            f"{trade_lines}\n\n"
        )
    else:
        track_context = ""

    user_context = db_build_user_context(pid)

    # Stable context: history + today's report + track record — cache this block.
    # It changes at most once per day (when a new report is uploaded).
    stable_block = (
        f"HISTORICAL CONTEXT ({len(reports)-1} reports from archive):\n{history}\n\n"
        f"TODAY'S REPORT ({latest_name}):\n{latest_content}\n\n"
        f"{track_context}"
    )

    # Volatile context: live prices, user profile, and the question — never cache.
    volatile_block = (
        f"{user_context}"
        f"LIVE MARKET DATA:\n{price_context}\n\n"
        f"Question: {query.question}\n\n"
        "Answer directly and actionably. Reference the user's current positions and recent lookups "
        "when relevant — e.g. if they have SOXL in their watchlist or recently checked SOXL RSI, factor that in.\n"
        "Reference Kevin's hit rate and past calls on this ticker if available.\n"
        "Not personalized financial advice."
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system=[{
            "type": "text",
            "text": "You are an expert investment analyst with access to months of Alpha Reports from Kevin.",
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": stable_block,   "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": volatile_block},
        ]}],
    )
    answer = r.content[0].text
    db_log(pid, "ask", query=query.question[:120], summary=answer[:200])
    return {"answer": answer, "report": latest_name, "live_prices": prices}

@app.get("/prices")
async def prices(tickers: str = ""):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers \
                  else ["SPY", "QQQ", "SOXL", "META", "MSFT", "MRVL", "TSLA", "AXON", "RKT"]
    return get_live_prices(ticker_list)

@app.get("/reports")
async def reports():
    r = get_all_reports()
    return {"count": len(r), "reports": [x["name"] for x in r]}

@app.get("/health")
async def health():
    name, _ = get_latest_report()
    return {"status": "ok", "latest_report": name}

FISCAL_PERIOD_ENDS = {"Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31)}

def get_earnings_calendar():
    from datetime import timedelta
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        return []
    stock_tickers = ["META", "MSFT", "TSLA", "MRVL", "AXON", "RKT"]
    today = datetime.utcnow().date()
    results = []
    for ticker in stock_tickers:
        try:
            r = requests.get(
                "https://api.polygon.io/vX/reference/financials",
                params={"ticker": ticker, "limit": 1, "timeframe": "quarterly",
                        "order": "desc", "apiKey": key},
                timeout=5
            )
            items = r.json().get("results", [])
            if not items:
                continue
            latest = items[0]
            fiscal   = latest.get("fiscal_period", "")
            fy       = int(latest.get("fiscal_year", 0) or 0)
            filing   = latest.get("filing_date")
            eps_raw  = (latest.get("financials") or {}).get("income_statement", {}) \
                             .get("basic_earnings_per_share", {}).get("value")
            eps_str  = f"${round(eps_raw, 2):+.2f}" if eps_raw is not None else "—"

            # Estimate next report: prefer actual filing_date + 91 days;
            # fall back to inferring period end from fiscal label + 45 days
            if filing:
                next_date = datetime.strptime(filing, "%Y-%m-%d").date() + timedelta(days=91)
                source = "est"
            elif fiscal in FISCAL_PERIOD_ENDS and fy:
                m, d = FISCAL_PERIOD_ENDS[fiscal]
                period_end = datetime(fy, m, d).date()
                # If that period end is in the past use next fiscal year's equivalent
                if period_end < today - timedelta(days=180):
                    period_end = datetime(fy + 1, m, d).date()
                next_date = period_end + timedelta(days=45)
                source = "est"
            else:
                next_date = None
                source = "unknown"

            # Advance past estimates forward by quarters until the date is upcoming
            if next_date:
                while next_date < today:
                    next_date += timedelta(days=91)
            days_away = (next_date - today).days if next_date else None
            results.append({
                "ticker":      ticker,
                "last_period": f"{fiscal} {fy}" if fy else fiscal,
                "last_eps":    eps_str,
                "next_date":   next_date.strftime("%b %d") if next_date else "unknown",
                "next_full":   next_date.isoformat() if next_date else None,
                "days_away":   days_away,
                "source":      source,
            })
        except Exception:
            pass
    return sorted(results, key=lambda x: (x["days_away"] is None, x["days_away"] or 999))

@app.get("/earnings")
async def earnings():
    return {"earnings": get_earnings_calendar()}

@app.get("/track-record")
async def track_record_api():
    trades = get_track_record()
    closed   = [t for t in trades if t["outcome"] in ("HIT", "MISS")]
    hits     = [t for t in closed if t["outcome"] == "HIT"]
    hit_rate = round(len(hits) / len(closed) * 100) if closed else 0
    return {"trades": trades, "stats": {"total": len(trades), "hit_rate": hit_rate, "closed": len(closed), "open": len(trades) - len(closed)}}

@app.get("/news")
async def ticker_news(request: Request, ticker: str = "SPY", limit: int = 5):
    pid    = current_profile_id(request)
    result = get_ticker_news(ticker, limit)
    db_log(pid, "news", ticker=ticker,
           summary=result[0]["title"][:100] if result else "no results")
    return {"ticker": ticker, "news": result}

@app.get("/options/expirations")
async def options_expirations(ticker: str = "SOXL"):
    return {"ticker": ticker, "expirations": get_options_expirations(ticker)}

@app.get("/options")
async def options_chain(request: Request, ticker: str = "SOXL", expiration: str = ""):
    pid = current_profile_id(request)
    if not expiration:
        exps = get_options_expirations(ticker)
        if not exps:
            return {"error": "No expirations found", "calls": [], "puts": []}
        expiration = exps[0]
    result = get_options_chain(ticker, expiration)
    db_log(pid, "options", ticker=ticker, query=expiration,
           summary=f"{len(result.get('calls',[]))} calls, {len(result.get('puts',[]))} puts")
    return {"ticker": ticker, "expiration": expiration, **result}

@app.get("/bars")
async def bars_with_log(request: Request, ticker: str = "SOXL", limit: int = 60):
    pid = current_profile_id(request)
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return {"error": "ALPACA_API_KEY not configured"}
    try:
        from datetime import timedelta
        start = (datetime.utcnow() - timedelta(days=120)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "limit": limit, "adjustment": "split",
                    "start": start, "sort": "desc"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10
        )
        raw = r.json().get("bars", [])
        raw.reverse()
        result = {"ticker": ticker, "bars": [{"t": b["t"][:10], "o": b["o"], "h": b["h"],
                  "l": b["l"], "c": b["c"], "v": b["v"]} for b in raw]}
        if raw:
            last_close = raw[-1].get("c", 0)
            db_log(pid, "rsi", ticker=ticker, summary=f"last close ${last_close}")
        return result
    except Exception as e:
        return {"error": str(e)}

@app.post("/trigger-analysis")
async def trigger_analysis(request: Request):
    global _trigger_state
    with _trigger_lock:
        if _trigger_state["running"]:
            return {"status": "already_running", "started": _trigger_state["started"]}

    def _run():
        global _trigger_state
        _trigger_state = {
            "running": True,
            "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished": None, "result": None, "error": None
        }
        try:
            # Build env for the subprocess — pass all current env vars through
            env = os.environ.copy()
            proc = subprocess.run(
                ["python3", "/analyzer/analyzer.py"],
                capture_output=True, text=True, timeout=240, env=env
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode == 0:
                # Grab last meaningful line of stdout
                lines = [l for l in out.splitlines() if l.strip()]
                _trigger_state["result"] = lines[-1] if lines else "Analysis complete."
            else:
                _trigger_state["error"] = (err or out)[:300]
        except subprocess.TimeoutExpired:
            _trigger_state["error"] = "Analysis timed out after 4 minutes."
        except Exception as e:
            _trigger_state["error"] = str(e)
        finally:
            _trigger_state["running"]  = False
            _trigger_state["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "started": _trigger_state["started"]}

@app.get("/analysis-status")
async def analysis_status():
    return _trigger_state

@app.get("/regime")
async def regime_api():
    return get_regime_data()

@app.get("/backtest")
async def backtest_api():
    return get_backtest_data()

@app.get("/correlation")
async def correlation_api():
    return get_correlation_data()

@app.get("/monte-carlo")
async def monte_carlo_api():
    return run_monte_carlo()

@app.get("/debug")
async def debug():
    return {"reports_dir": REPORTS_DIR, "all_files": list_all_files(), "results_dir_exists": os.path.exists(RESULTS_DIR)}
