"""
f1_fantasy_sync.py
──────────────────
Dual-file sync. Writes TWO files:

  history.json   ← append-only permanent archive of every confirmed round
  f1_teams.json  ← live snapshot: current prices + overall standings + active/upcoming picks only

history.json format (mirrors seed $RATXXXX.json, extended with teams[]/drivers[]/constructors[]):
  ├── _meta             — league metadata + season
  ├── rounds[]          — one entry per CONFIRMED round (never overwritten once written)
  │   ├── round, key, label, flag, gp, date, confirmed=true
  │   ├── standings[]   — points/race_points/cumulative_points per player
  │   ├── teams[]       — full picks per player (same schema as f1_teams rounds.teams[])
  │   ├── drivers[]     — F1 player-pool snapshot (LATEST confirmed round only)
  │   └── constructors[]
  └── players{}         — display metadata (name/owner/emoji/color)

f1_teams.json format (slim live snapshot only):
  ├── last_updated / league_id / league_name
  ├── f1_players              — CURRENT driver + constructor prices & points
  ├── players[]               — overall standings (cur_points / cur_rank)
  └── rounds[]                — ONLY the current live/upcoming round (unconfirmed picks + prov pts)
                                 OR the latest confirmed round when between race weekends

KEY RULES:
  • confirmed rounds → archived once to history.json, NEVER re-fetched or modified
  • f1_teams.json.rounds[] contains at most 1-2 entries (the live/next round only)
  • history.json is the source of truth for all confirmed historical data

AUTH: scripts/f1_session.json  →  { "guid": "...", "raw_cookies": "..." }
REQUIRES: pip install httpx
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx

BASE          = "https://fantasy.formula1.com"
SESSION_FILE  = Path(__file__).parent / "f1_session.json"
LEAGUE_ID     = os.environ.get("F1_FANTASY_LEAGUE_ID", "")
OUTPUT_FILE   = Path(__file__).parent.parent / "f1_teams.json"
HISTORY_FILE  = Path(__file__).parent.parent / "history.json"

# ── Map F1 Fantasy social_id → internal player key ───────────────────────────
PLAYER_MAP = {
    "232016281": "kevcedes",
    "191134213": "grahhh",
    "178339760": "leclaren",
    "178336081": "juice",
    "178798446": "thumbi",
}

# ── Static display metadata — safe to edit ────────────────────────────────────
PLAYER_META = {
    "kevcedes": {"name": "Kevcedes",      "owner": "Kevin Liang",    "emoji": "🗿", "color": "#f4a100"},
    "grahhh":   {"name": "GRAHHH racing", "owner": "Vivian Nguyen",  "emoji": "🐥", "color": "#ff4757"},
    "leclaren": {"name": "LeClaren F1",   "owner": "Selina Le Khac", "emoji": "👽", "color": "#3a86ff"},
    "juice":    {"name": "RACING JUICE",  "owner": "Justin Tran",    "emoji": "👑", "color": "#2ec4b6"},
    "thumbi":   {"name": "Thumbi",        "owner": "Thomas George",  "emoji": "🍄", "color": "#c77dff"},
}

# ── GP name overrides (API sometimes returns venue strings) ───────────────────
GP_NAMES = {
    1: "Australian Grand Prix",   2: "Chinese Grand Prix",
    3: "Japanese Grand Prix",     4: "Bahrain Grand Prix",
    5: "Saudi Arabian Grand Prix",6: "Miami Grand Prix",
    7: "Canadian Grand Prix",     8: "Monaco Grand Prix",
    9: "Spanish Grand Prix",     10: "Austrian Grand Prix",
   11: "British Grand Prix",     12: "Belgian Grand Prix",
   13: "Hungarian Grand Prix",   14: "Dutch Grand Prix",
   15: "Italian Grand Prix",     16: "Spanish Grand Prix (Madrid)",
   17: "Azerbaijan Grand Prix",  18: "Singapore Grand Prix",
   19: "United States Grand Prix",20: "Mexico City Grand Prix",
   21: "São Paulo Grand Prix",   22: "Las Vegas Grand Prix",
   23: "Qatar Grand Prix",       24: "Abu Dhabi Grand Prix",
}

# ── GP flag emojis ────────────────────────────────────────────────────────────
GP_FLAGS = {
    1:"🇦🇺", 2:"🇨🇳", 3:"🇯🇵", 4:"🇧🇭", 5:"🇸🇦", 6:"🇺🇸",
    7:"🇨🇦", 8:"🇲🇨", 9:"🇪🇸",10:"🇦🇹",11:"🇬🇧",12:"🇧🇪",
   13:"🇭🇺",14:"🇳🇱",15:"🇮🇹",16:"🇪🇸",17:"🇦🇿",18:"🇸🇬",
   19:"🇺🇸",20:"🇲🇽",21:"🇧🇷",22:"🇺🇸",23:"🇶🇦",24:"🇦🇪",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_session() -> tuple[str, str]:
    if not SESSION_FILE.exists():
        print("❌ scripts/f1_session.json not found.")
        print()
        print('Create it: { "guid": "your-guid", "raw_cookies": "cookie-string" }')
        print()
        print("Get cookies:")
        print("  Chrome → fantasy.formula1.com → F12 → Network")
        print("  Filter 'getusergamedaysv1' → Refresh → right-click → Copy as cURL (bash)")
        print("  Paste the -b '...' value as raw_cookies")
        sys.exit(1)

    s = json.loads(SESSION_FILE.read_text())
    guid        = s.get("guid", "")
    raw_cookies = s.get("raw_cookies", "")
    if not raw_cookies and "cookies" in s:
        raw_cookies = "; ".join(f"{c['name']}={c['value']}" for c in s["cookies"])
    if not guid:
        print("❌ 'guid' missing from f1_session.json"); sys.exit(1)
    if not raw_cookies:
        print("❌ 'raw_cookies' missing from f1_session.json"); sys.exit(1)
    return guid, raw_cookies


def headers(raw_cookies: str) -> dict:
    return {
        "accept":             "application/json, text/plain, */*",
        "accept-language":    "en-US,en;q=0.9",
        "referer":            "https://fantasy.formula1.com/en/",
        "user-agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec-ch-ua":          '"' + 'Not-A.Brand' + '";v="99", "Chromium";v="124"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-origin",
        "cookie":             raw_cookies,
    }


async def get(client: httpx.AsyncClient, url: str, label: str = "") -> dict:
    try:
        r = await client.get(url)
    except Exception as e:
        print(f"  ⚠️  {label or url[:60]}: {e}"); return {}

    if r.status_code == 401:
        print("\n  ❌ 401 — cookies expired.\n")
        print("  Refresh: Chrome → fantasy.formula1.com → F12 → Network")
        print("  Filter 'getusergamedaysv1' → Reload → Copy as cURL (bash)")
        print("  Paste -b value into scripts/f1_session.json as raw_cookies\n")
        sys.exit(1)

    if r.status_code != 200:
        print(f"  ⚠️  HTTP {r.status_code} {label or url[:60]}"); return {}
    try:
        return r.json()
    except Exception:
        return {}


def load_existing() -> dict:
    if OUTPUT_FILE.exists():
        try:
            return json.loads(OUTPUT_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def load_history() -> dict:
    """Load history.json — the permanent archive of confirmed rounds."""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {"_meta": {}, "rounds": [], "players": {}}


def round_pts(cumul: float | None, prev: float | None) -> float | None:
    """round_points = cumulative - previous_cumulative (or just cumulative for R1)."""
    if cumul is None:
        return None
    return round(cumul - (prev or 0), 1)


def has_valid_points(r: dict) -> bool:
    """True if the round has real non-null point data (not all-null)."""
    return any(
        s.get("round_points") is not None
        for s in r.get("standings", [])
    )


def history_round_to_teams_format(hr: dict) -> dict:
    """Convert a history.json round entry back to f1_teams internal round format.
    This allows history rounds to serve as the confirmed-round cache."""
    standings = [
        {
            "player_id":         s.get("player_id", ""),
            "player_key":        s["player_key"],
            "round_points":      s.get("points") or s.get("race_points") or 0,
            "cumulative_points": s.get("cumulative_points") or 0,
            "team_value":        s.get("team_value") or 0,
        }
        for s in hr.get("standings", [])
    ]
    return {
        "round":     hr["round"],
        "gp":        hr.get("gp", f"Round {hr['round']}"),
        "date":      hr.get("date", ""),
        "confirmed": True,
        "standings": standings,
        "teams":     hr.get("teams", []),
    }


def build_history_round(
    round_data: dict,
    f1_players_snapshot: dict | None = None,
) -> dict:
    """Convert an f1_teams internal round to history.json archive format."""
    rnum  = round_data["round"]
    gp    = round_data.get("gp", f"Round {rnum}")
    flag  = GP_FLAGS.get(rnum, "🏁")
    date  = round_data.get("date", "")
    label = f"R{str(rnum).zfill(2)} · {gp} {flag}"

    teams = round_data.get("teams", [])
    s_map = {s["player_id"]: s for s in round_data.get("standings", [])}

    standings_out = []
    for t in sorted(teams, key=lambda x: -(x.get("round_points") or 0)):
        pid  = t["player_id"]
        pkey = t["player_key"]
        meta = PLAYER_META.get(pkey, {})
        standings_out.append({
            "player_id":         pid,
            "player_key":        pkey,
            "player_name":       meta.get("name", pkey),
            "owner":             meta.get("owner", ""),
            "points":            t.get("round_points") or 0,
            "race_points":       t.get("round_points") or 0,
            "cumulative_points": t.get("cumulative_points") or 0,
            "team_value":        t.get("team_value") or 0,
        })

    entry: dict = {
        "round":     rnum,
        "key":       f"R{str(rnum).zfill(2)}",
        "label":     label,
        "flag":      flag,
        "gp":        gp,
        "date":      date,
        "confirmed": True,
        "standings": standings_out,
        "teams":     teams,
    }

    # Include F1 player-pool snapshot only for the latest confirmed round,
    # where points_this_gw accurately reflects that round's per-driver points.
    if f1_players_snapshot:
        entry["drivers"]      = f1_players_snapshot.get("drivers", [])
        entry["constructors"] = f1_players_snapshot.get("constructors", [])

    return entry


# ─────────────────────────────────────────────────────────────────────────────
# Main sync
# ─────────────────────────────────────────────────────────────────────────────

async def sync(force: bool = False):
    guid, raw_cookies = load_session()
    print(f"  Session: GUID {guid[:8]}…")
    if force:
        print("  ⚠️  --force: re-fetching all rounds (ignoring cache)")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Load history.json as the authoritative confirmed-round cache ──────────
    existing_history  = load_history()
    hist_rounds_map: dict[int, dict] = {
        r["round"]: r for r in existing_history.get("rounds", [])
    }

    # Build cached_confirmed (f1_teams internal format) from history.json.
    # Golden rule: once a round is in history.json it is NEVER re-fetched.
    cached_confirmed: dict[int, dict] = {}
    if not force:
        for rnum, hr in hist_rounds_map.items():
            converted = history_round_to_teams_format(hr)
            has_picks = any(t.get("picks") for t in converted.get("teams", []))
            if has_picks and has_valid_points(converted):
                cached_confirmed[rnum] = converted

    if cached_confirmed:
        print(f"  Cached confirmed rounds (from history.json): {sorted(cached_confirmed)}")

    async with httpx.AsyncClient(headers=headers(raw_cookies), timeout=60, follow_redirects=True) as c:

        # ── 1. Schedule ────────────────────────────────────────────────────────
        print("  Fetching schedule…")
        sched = await get(c, f"{BASE}/feeds/v2/schedule/raceday_en.json", "schedule")
        matchdays: dict[int, dict] = {}
        for fx in sched.get("Data", {}).get("fixtures", []):
            mdid = fx.get("MatchdayId") or fx.get("GamedayId")
            if not mdid:
                continue
            mdid = int(mdid)
            if mdid not in matchdays:
                matchdays[mdid] = {
                    "round":    mdid,
                    "gp":       GP_NAMES.get(mdid) or fx.get("Venue") or fx.get("RaceName") or f"Round {mdid}",
                    "date":     fx.get("GameDate", "")[:10],
                    "finished": int(fx.get("GDIsLocked", 0)) == 1,
                }
        print(f"  Matchdays: {len(matchdays)}")

        # ── 2. F1 player pool ─────────────────────────────────────────────────
        print("  Fetching F1 player pool…")
        raw = await get(c, f"{BASE}/feeds/drivers/2_en.json", "f1-players")
        drivers_out:     list[dict] = []
        constrs_out:     list[dict] = []
        player_lkp:      dict[str, dict] = {}
        constructor_ids: set[str] = set()

        for pl in raw.get("Data", {}).get("Value", []):
            pid   = str(pl.get("PlayerId", ""))
            skill = pl.get("Skill", 1)
            e = {
                "id":             pid,
                "short_name":     (pl.get("DriverTLA") or pl.get("DisplayName") or "").strip(),
                "full_name":      (pl.get("FUllName")  or pl.get("FullName")    or "").strip(),
                "team":           (pl.get("TeamName")  or "").strip(),
                "team_tag":       (pl.get("DriverTLA") or "").strip(),
                "price":          round(float(pl.get("Value", 0)), 1),
                "total_points":   float(pl.get("OverallPpints") or pl.get("OverallPoints") or 0),
                "points_this_gw": float(pl.get("GamedayPoints") or 0),
            }
            player_lkp[pid] = e
            if skill == 2:
                constrs_out.append(e); constructor_ids.add(pid)
            else:
                drivers_out.append(e)
        print(f"  F1 players: {len(drivers_out)} drivers, {len(constrs_out)} constructors")

        # ── 3. Early exit if no league ID ─────────────────────────────────────
        if not LEAGUE_ID:
            print("  ⚠️  F1_FANTASY_LEAGUE_ID not set — only updating f1_players.")
            existing = load_existing()
            out = {
                "last_updated": ts,
                "league_id":    "",
                "league_name":  "Baby Formula Championship",
                "f1_players":   {"drivers": drivers_out, "constructors": constrs_out},
                "players":      existing.get("players", []),
                "rounds":       existing.get("rounds",  []),
            }
            OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
            print("✅ f1_teams.json updated (f1_players only)")
            return

        # ── 4. League info ─────────────────────────────────────────────────────
        import time
        info_raw = await get(c, f"{BASE}/services/user/league/getleagueinfo/{LEAGUE_ID}?buster={int(time.time()*1000)}", "league-info")
        info_val = info_raw.get("Data", {}).get("Value", {})
        league_name = unquote(info_val.get("leagueName", "Baby Formula Championship"))
        numeric_id  = info_val.get("leagueId", "")

        buster2 = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        lb_raw  = await get(c, f"{BASE}/feeds/leaderboard/privateleague/list_1_{numeric_id}_0_1.json?buster={buster2}", "leaderboard")
        lb      = lb_raw.get("Value", {}).get("leaderboard", [])
        print(f"  League '{league_name}': {len(lb)} players")

        league_players: list[dict] = []
        for entry in lb:
            uid  = str(entry.get("social_id", ""))
            pkey = PLAYER_MAP.get(uid, uid)
            meta = PLAYER_META.get(pkey, {})
            league_players.append({
                "id":          uid,
                "player_key":  pkey,
                "name":        meta.get("name")  or unquote(entry.get("team_name", uid)),
                "owner":       meta.get("owner") or "",
                "emoji":       meta.get("emoji") or "👤",
                "color":       meta.get("color") or "#aaaaaa",
                "f1_user_id":  uid,
                "guid":        entry.get("user_guid", ""),
                "cur_points":  float(entry.get("cur_points") or 0),
                "cur_rank":    int(entry.get("cur_rank") or 0),
            })

        print()
        print("  Player map status:")
        for p in league_players:
            tag = "✅" if p["id"] in PLAYER_MAP else "❌ ADD TO PLAYER_MAP"
            print(f"    {tag}  \"{p['id']}\": \"{p['player_key']}\",  # {p['name']}")
        print()

        # ── 5. Completed matchdays ─────────────────────────────────────────────
        gd_raw  = await get(c, f"{BASE}/services/user/gameplay/{guid}/getusergamedaysv1/1", "gamedays")
        gd_list = gd_raw.get("Data", {}).get("Value", [])
        if not gd_list:
            print("  ❌ Empty gamedays response — cookies may be expired."); sys.exit(1)

        completed: set[int] = {
            int(k) for k, v in gd_list[0].get("mddetails", {}).items()
            if v.get("mds") == 3
        }
        print(f"  Completed rounds: {sorted(completed)}")

        all_mdids  = sorted(matchdays)
        next_round = next((m for m in all_mdids if m not in completed), None)

        # Fetch rounds that are completed but not yet cached in history,
        # plus the next upcoming round for picks preview.
        to_fetch: set[int] = {m for m in completed if m not in cached_confirmed}
        if next_round and next_round not in cached_confirmed:
            to_fetch.add(next_round)

        print(f"  Fetching picks for rounds: {sorted(to_fetch) or 'none (all cached)'}")

        # ── 6. Build rounds ────────────────────────────────────────────────────
        overall_cur_pts: dict[str, float] = {p["id"]: float(p.get("cur_points") or 0) for p in league_players}

        prev_cumul:  dict[str, float] = {}
        rounds_out:  list[dict] = []

        for mdid in all_mdids:
            md        = matchdays.get(mdid, {"round": mdid, "gp": f"Round {mdid}", "date": "", "finished": False})
            confirmed = mdid in completed

            # ── Reuse cached round from history — NEVER re-fetch ─────────────
            if mdid in cached_confirmed:
                r = dict(cached_confirmed[mdid])
                r["gp"] = md["gp"]
                rounds_out.append(r)
                for s in r.get("standings", []):
                    prev_cumul[s["player_id"]] = s.get("cumulative_points") or 0
                continue

            # ── Empty placeholder for future rounds ───────────────────────────
            if mdid not in to_fetch:
                rounds_out.append({
                    "round": mdid, "gp": md["gp"], "date": md["date"],
                    "confirmed": False, "standings": [], "teams": [],
                })
                continue

            # ── Fetch this round ──────────────────────────────────────────────
            print(f"  → R{mdid:02d} {md['gp']}…")
            rec: dict = {
                "round": mdid, "gp": md["gp"], "date": md["date"],
                "confirmed": confirmed, "standings": [], "teams": [],
            }
            cumul_this: dict[str, float] = {}

            for player in league_players:
                pid    = player["id"]
                p_guid = player.get("guid", "")

                if not p_guid:
                    rec["teams"].append({
                        "player_id": pid, "player_key": player["player_key"],
                        "round_points": None, "cumulative_points": None,
                        "team_value": 0, "budget_remaining": 0, "picks": [],
                    })
                    continue

                t_raw  = await get(c, f"{BASE}/services/user/gameplay/{p_guid}/getteam/1/{mdid}/1/1", f"picks R{mdid} {player['name']}")
                tval   = t_raw.get("Data", {}).get("Value", {})
                teams  = tval.get("userTeam", [])
                team   = teams[0] if teams else {}
                picks_raw = team.get("playerid", [])

                is_latest = confirmed and (mdid == max(completed, default=0))

                if confirmed and is_latest:
                    cumul = overall_cur_pts.get(pid)
                elif confirmed:
                    mdpts_raw = team.get("mdpoints")
                    if mdpts_raw is not None:
                        cumul = round((prev_cumul.get(pid) or 0) + float(mdpts_raw), 1)
                    else:
                        print(f"    ⚠️  Older confirmed R{mdid} not cached and mdpoints missing for {player['name']}")
                        cumul = None
                else:
                    cumul = None

                prev_pts = prev_cumul.get(pid) or 0
                rpts = round(cumul - prev_pts, 1) if cumul is not None else None
                cumul_this[pid] = cumul or 0

                picks: list[dict] = []
                for pk in picks_raw:
                    pk_id = str(pk.get("id", ""))
                    ple   = player_lkp.get(pk_id, {})
                    picks.append({
                        "id":           pk_id,
                        "short_name":   ple.get("short_name", ""),
                        "full_name":    ple.get("full_name",  ""),
                        "team":         ple.get("team",       ""),
                        "team_tag":     ple.get("team_tag",   ""),
                        "type":         "constructor" if pk_id in constructor_ids else "driver",
                        "is_star":      bool(pk.get("iscaptain") or pk.get("ismgcaptain")),
                        "price":        ple.get("price", 0),
                        "round_points": None,
                    })

                rec["teams"].append({
                    "player_id":         pid,
                    "player_key":        player["player_key"],
                    "round_points":      rpts,
                    "cumulative_points": cumul,
                    "team_value":        float(team.get("teamval") or 0),
                    "budget_remaining":  float(team.get("teambal") or 0),
                    "picks":             picks,
                })

            sort_key = "round_points" if confirmed else "cumulative_points"
            rec["standings"] = sorted(
                [
                    {
                        "player_id":         t["player_id"],
                        "player_key":        t["player_key"],
                        "round_points":      t["round_points"],
                        "cumulative_points": t["cumulative_points"],
                        "team_value":        t["team_value"],
                    }
                    for t in rec["teams"] if t.get("picks")
                ],
                key=lambda x: x.get(sort_key) or 0,
                reverse=True,
            )

            for pid, cv in cumul_this.items():
                if cv:
                    prev_cumul[pid] = cv

            rounds_out.append(rec)
            print(f"    ✓ {len(rec['teams'])} teams, confirmed={confirmed}")

        # ── 7. Archive newly confirmed rounds to history.json ─────────────────
        # Only append rounds that are:
        #   a) confirmed (mds==3)
        #   b) NOT already in history.json (never overwrite)
        latest_confirmed_num = max(completed, default=0)
        new_history_rounds: list[dict] = []

        for r in rounds_out:
            rnum = r["round"]
            if not r.get("confirmed"):
                continue
            if rnum in hist_rounds_map:
                continue  # already archived — do NOT touch it
            # New confirmed round: archive with optional f1_players snapshot
            is_latest   = (rnum == latest_confirmed_num)
            snapshot    = {"drivers": drivers_out, "constructors": constrs_out} if is_latest else None
            new_history_rounds.append(build_history_round(r, snapshot))

        if new_history_rounds:
            all_history_rounds = sorted(
                list(existing_history.get("rounds", [])) + new_history_rounds,
                key=lambda r: r["round"],
            )
            players_meta = {
                p["player_key"]: {
                    "id":    p["player_key"],
                    "name":  p["name"],
                    "owner": p["owner"],
                    "emoji": p["emoji"],
                    "color": p["color"],
                }
                for p in league_players
            }
            updated_history = {
                "_meta": {
                    "league_id":        LEAGUE_ID,
                    "league_name":      league_name,
                    "season":           2026,
                    "last_updated":     ts,
                    "rounds_completed": len(completed),
                },
                "rounds":  all_history_rounds,
                "players": players_meta,
            }
            HISTORY_FILE.write_text(
                json.dumps(updated_history, indent=2, ensure_ascii=False), encoding='utf-8'
            )
            nums = [r["round"] for r in new_history_rounds]
            print(f"\n✅ history.json  —  archived {len(new_history_rounds)} new round(s): {nums}")
        else:
            print("  history.json  —  no new confirmed rounds to archive")

        # ── 8. Write slim f1_teams.json (live snapshot only) ──────────────────
        # rounds[] contains ONLY:
        #   • Unconfirmed/provisional rounds (live race weekend + provisional pts)
        #   • If no live round: latest confirmed round (so current picks are browsable)
        # All confirmed historical rounds live exclusively in history.json.
        unconfirmed_rounds = [
            r for r in rounds_out
            if not r.get("confirmed") and r.get("teams")
        ]
        if not unconfirmed_rounds:
            # Between race weekends: include latest confirmed round for team pick display
            confirmed_with_picks = sorted(
                [
                    r for r in rounds_out
                    if r.get("confirmed") and any(t.get("picks") for t in r.get("teams", []))
                ],
                key=lambda r: r["round"],
            )
            live_rounds = [confirmed_with_picks[-1]] if confirmed_with_picks else []
        else:
            live_rounds = unconfirmed_rounds

        out = {
            "last_updated": ts,
            "league_id":    LEAGUE_ID,
            "league_name":  league_name,
            "f1_players":   {"drivers": drivers_out, "constructors": constrs_out},
            "players":      league_players,
            "rounds":       live_rounds,
        }
        OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')

        live_cnt = len(live_rounds)
        print(f"✅ f1_teams.json  —  {len(league_players)} players · {live_cnt} live round(s) · {len(completed)} completed total")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="F1 Fantasy Sync")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all rounds, ignoring cached confirmed data")
    args = parser.parse_args()

    print("F1 Fantasy Sync")
    print("=" * 40)

    import platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(sync(force=args.force))
        print()
        print("SYNC COMPLETE")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
