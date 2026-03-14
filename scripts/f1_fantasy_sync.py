"""
f1_fantasy_sync.py
──────────────────
Pulls data from the official F1 Fantasy API and writes two JSON files:

  f1_fantasy.json  — driver / constructor prices & points (overall + per GW)
  f1_teams.json    — Baby Formula Championship league team picks per round

Secrets expected as environment variables (set in GitHub Actions):
  F1_FANTASY_EMAIL     — your login email
  F1_FANTASY_PASSWORD  — your login password
  F1_FANTASY_LEAGUE_ID — your private league ID (numeric string)

API reference: https://documenter.getpostman.com/view/11462073/TzY68Dsi
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp

# ─────────────────────────────────────────────────────────────────
BASE        = "https://fantasy-api.formula1.com/f1/2026"
AUTH_URL    = "https://api.formula1.com/v2/account/subscriber/authenticate/by-password"
IMAGE_BASE  = "https://fantasy-api.formula1.com"

EMAIL       = os.environ.get("F1_FANTASY_EMAIL", "")
PASSWORD    = os.environ.get("F1_FANTASY_PASSWORD", "")
LEAGUE_ID   = os.environ.get("F1_FANTASY_LEAGUE_ID", "")

# Map F1 team IDs → short tag (update if F1 changes IDs each season)
TEAM_TAG_MAP = {
    1:'MER', 2:'FER', 3:'RED', 4:'MCL', 5:'AMR',
    6:'ALP', 7:'WIL', 8:'HAA', 9:'AUD', 10:'VRB', 11:'CAD',
}

# Baby Formula Championship players — map F1 Fantasy user_id → our player_id
# Fill these in once you know each person's F1 Fantasy user ID
PLAYER_MAP = {
    # "232016281": "kevcedes",
    # "191134213": "grahhh racing",
    # "178339760": "leclaren f1",
    # "178336081": "racing juice",
    # "178798446": "thumbi",
}

# ─────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────
async def authenticate(session: aiohttp.ClientSession) -> str:
    """Returns a Bearer token."""
    payload = {
        "Login":               EMAIL,
        "Password":            PASSWORD,
        "DistributionChannel": "d861e38f-05ea-4063-8776-a7e2b6d885a4",  # required field
    }
    headers = {
        # Must match a real browser request — F1 API rejects bot-like requests
        "Content-Type":     "application/json",
        "apikey":           "fCUCjWrKPu9ylJwRAv8BpGLEgiAuThx7",
        "authority":        "api.formula1.com",
        "origin":           "https://account.formula1.com",
        "referer":          "https://account.formula1.com/",
        "user-agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "accept":           "application/json, text/javascript, */*; q=0.01",
        "accept-language":  "en-US,en;q=0.9",
        "sec-fetch-site":   "same-site",
        "sec-fetch-mode":   "cors",
        "sec-fetch-dest":   "empty",
        "pragma":           "no-cache",
        "cache-control":    "no-cache",
    }
    async with session.post(AUTH_URL, json=payload, headers=headers) as r:
        if r.status != 200:
            raise RuntimeError(f"Auth failed {r.status}: {await r.text()}")
        data = await r.json()
    token = data.get("data", {}).get("subscriptionToken") or data.get("subscriptionToken")
    if not token:
        raise RuntimeError(f"No token in auth response: {data}")
    print("✅ Authenticated.")
    return token


async def api_get(session: aiohttp.ClientSession, path: str, token: str) -> dict:
    url     = f"{BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            print(f"  ⚠️  GET {path} → {r.status}")
            return {}
        return await r.json()


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
def clean_driver(d: dict) -> dict:
    return {
        "id":           d.get("id"),
        "short_name":   d.get("short_name", ""),
        "full_name":    f"{d.get('first_name','')} {d.get('last_name','')}".strip(),
        "team":         d.get("team_name", ""),
        "team_tag":     TEAM_TAG_MAP.get(d.get("team_id"), ""),
        "price":        round(d.get("price", 0) / 1_000_000, 1),
        "total_points": d.get("total_points", 0),
        "points_this_gw": d.get("score", 0),
    }


def clean_constructor(c: dict) -> dict:
    return {
        "id":           c.get("id"),
        "short_name":   c.get("short_name", c.get("name", "")),
        "full_name":    c.get("name", ""),
        "team_tag":     TEAM_TAG_MAP.get(c.get("id"), ""),
        "price":        round(c.get("price", 0) / 1_000_000, 1),
        "total_points": c.get("total_points", 0),
        "points_this_gw": c.get("score", 0),
    }


# ─────────────────────────────────────────────────────────────────
# MAIN SYNC
# ─────────────────────────────────────────────────────────────────
async def sync(session: aiohttp.ClientSession, token: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── 1. Game info (gameweeks list) ────────────────────────────
    game_info = await api_get(session, "/game", token)
    gameweeks_raw = game_info.get("current_gameweek") or []
    # Full gameweek list
    all_gws_raw  = await api_get(session, "/gameweeks", token)
    all_gws      = all_gws_raw.get("gameweeks", all_gws_raw) if isinstance(all_gws_raw, dict) else all_gws_raw

    # ── 2. Overall driver / constructor stats ────────────────────
    players_raw  = await api_get(session, "/players", token)
    drivers_all  = [clean_driver(d)      for d in players_raw.get("players", [])
                    if d.get("is_constructor") is False or "driver" in str(d.get("type","")).lower()]
    constrs_all  = [clean_constructor(c) for c in players_raw.get("players", [])
                    if d.get("is_constructor") is True  or "constructor" in str(d.get("type","")).lower()
                    for d in [c]]  # rename for clarity

    # Re-split cleanly using a flag field
    all_players  = players_raw.get("players", [])
    drivers_all  = [clean_driver(p)      for p in all_players if not p.get("is_constructor", False)]
    constrs_all  = [clean_constructor(p) for p in all_players if     p.get("is_constructor", False)]

    # ── 3. Per-gameweek stats ────────────────────────────────────
    gw_records = []
    for gw in (all_gws if isinstance(all_gws, list) else []):
        gw_id    = gw.get("id") or gw.get("gameweek_id")
        gw_round = gw.get("race_id") or gw.get("round") or gw_id
        gw_label = gw.get("name") or f"R{gw_round}"
        finished = gw.get("finished", False)
        if not finished:
            continue   # skip unfinished gameweeks
        gw_data = await api_get(session, f"/players?gameweek={gw_id}", token)
        gw_players = gw_data.get("players", [])
        gw_records.append({
            "round":        gw_round,
            "gp":           gw_label,
            "date":         gw.get("deadline_date", "")[:10],
            "drivers":      [clean_driver(p)      for p in gw_players if not p.get("is_constructor", False)],
            "constructors": [clean_constructor(p) for p in gw_players if     p.get("is_constructor", False)],
        })

    fantasy_output = {
        "last_updated": ts,
        "overall": {
            "drivers":      drivers_all,
            "constructors": constrs_all,
        },
        "gameweeks": gw_records,
    }
    with open("f1_fantasy.json", "w", encoding="utf-8") as f:
        json.dump(fantasy_output, f, indent=2, ensure_ascii=False)
    print(f"✅ f1_fantasy.json written — {len(drivers_all)} drivers, {len(constrs_all)} constructors, {len(gw_records)} gameweeks.")

    # ── 4. League teams ──────────────────────────────────────────
    if not LEAGUE_ID:
        print("⚠️  F1_FANTASY_LEAGUE_ID not set — skipping league team sync.")
        return

    league_raw = await api_get(session, f"/leagues/{LEAGUE_ID}", token)
    league_standings = league_raw.get("standings", league_raw.get("league", {}).get("standings", []))

    teams_output = {
        "last_updated": ts,
        "league_id":    LEAGUE_ID,
        "league_name":  league_raw.get("name", "Baby Formula Championship"),
        "players":      [],
        "rounds":       [],
    }

    # Build player list from league standings
    for entry in league_standings:
        uid = str(entry.get("user_id") or entry.get("id", ""))
        our_id = PLAYER_MAP.get(uid, uid)
        teams_output["players"].append({
            "id":    our_id,
            "name":  entry.get("team_name") or entry.get("name") or our_id,
            "emoji": "👤",
            "f1_user_id": uid,
        })

    # For each completed gameweek, fetch each player's team
    for gw in (all_gws if isinstance(all_gws, list) else []):
        gw_id    = gw.get("id") or gw.get("gameweek_id")
        gw_round = gw.get("race_id") or gw.get("round") or gw_id
        finished = gw.get("finished", False)

        round_record = {
            "round":     gw_round,
            "gp":        gw.get("name", f"R{gw_round}"),
            "confirmed": finished,
            "teams":     [],
        }

        for entry in league_standings:
            uid     = str(entry.get("user_id") or entry.get("id", ""))
            our_id  = PLAYER_MAP.get(uid, uid)
            picks_raw = await api_get(session, f"/picks/{uid}?gameweek={gw_id}", token)
            picks   = picks_raw.get("picks", [])

            pick_list = []
            for p in picks:
                is_constructor = p.get("is_constructor", False)
                pid = p.get("player_id") or p.get("id")
                # Find matching stats from players list
                match = next((x for x in all_players if x.get("id") == pid), {})
                entry_clean = {
                    "id":          pid,
                    "short_name":  match.get("short_name", p.get("short_name", "")),
                    "full_name":   f"{match.get('first_name','')} {match.get('last_name','')}".strip() or p.get("name",""),
                    "team":        match.get("team_name", ""),
                    "team_tag":    TEAM_TAG_MAP.get(match.get("team_id") or match.get("id") if is_constructor else match.get("team_id"), ""),
                    "type":        "constructor" if is_constructor else "driver",
                    "is_star":     p.get("is_captain") or p.get("is_star") or False,
                    "price":       round((p.get("price") or match.get("price", 0)) / 1_000_000, 1),
                    "price_change": round((p.get("selling_price", 0) - p.get("purchase_price", p.get("price", 0))) / 1_000_000, 2) if finished else None,
                    "points":      p.get("points") if finished else None,
                }
                pick_list.append(entry_clean)

            total_pts = sum(pk["points"] or 0 for pk in pick_list) if finished else None
            budget_rem = round((picks_raw.get("budget") or picks_raw.get("bank", 0)) / 1_000_000, 1)
            team_val   = round(sum((pk["price"] or 0) for pk in pick_list), 1)

            round_record["teams"].append({
                "player_id":        our_id,
                "total_points":     total_pts,
                "team_value":       team_val,
                "budget_remaining": budget_rem,
                "picks":            pick_list,
            })

        teams_output["rounds"].append(round_record)

    with open("f1_teams.json", "w", encoding="utf-8") as f:
        json.dump(teams_output, f, indent=2, ensure_ascii=False)
    print(f"✅ f1_teams.json written — {len(teams_output['players'])} players, {len(teams_output['rounds'])} rounds.")


async def main():
    if not EMAIL or not PASSWORD:
        print("❌ F1_FANTASY_EMAIL / F1_FANTASY_PASSWORD not set. Exiting.")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        token = await authenticate(session)
        await sync(session, token)


if __name__ == "__main__":
    asyncio.run(main())
