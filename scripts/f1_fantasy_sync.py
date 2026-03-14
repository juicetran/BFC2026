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

WHY PLAYWRIGHT FOR AUTH:
  The F1 API auth endpoint (api.formula1.com) is protected by Distil Networks
  bot-detection. Plain HTTP requests from datacenter IPs (GitHub Actions = Azure)
  get a 403 "Pardon Our Interruption" CAPTCHA page regardless of headers.
  Playwright runs a real Chromium browser which passes the JS challenge and
  obtains a valid session token. All subsequent API calls use that token via
  aiohttp (fast async HTTP) — only auth needs the browser.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────
BASE        = "https://fantasy-api.formula1.com/f1/2026"
AUTH_URL    = "https://api.formula1.com/v2/account/subscriber/authenticate/by-password"

EMAIL       = os.environ.get("F1_FANTASY_EMAIL", "")
PASSWORD    = os.environ.get("F1_FANTASY_PASSWORD", "")
LEAGUE_ID   = os.environ.get("F1_FANTASY_LEAGUE_ID", "")

TEAM_TAG_MAP = {
    1:'MER', 2:'FER', 3:'RED', 4:'MCL', 5:'AMR',
    6:'ALP', 7:'WIL', 8:'HAA', 9:'AUD', 10:'VRB', 11:'CAD',
}

PLAYER_MAP = {
    # "232016281": "kevcedes",
    # "191134213": "grahhh",
    # "178339760": "leclaren",
    # "178336081": "juice",
    # "178798446": "thumbi",
}

# ─────────────────────────────────────────────────────────────────
# AUTH — uses Playwright browser to bypass bot-detection
# ─────────────────────────────────────────────────────────────────
async def authenticate_with_browser() -> str:
    """
    Uses Playwright's APIRequestContext (context.request.post) to authenticate.

    WHY NOT page.evaluate() fetch:
      fetch() inside a browser page is subject to CORS. api.formula1.com does
      not allow cross-origin requests from account.formula1.com in a headless
      context, so it throws a network error → status=0.

    WHY context.request.post WORKS:
      Playwright's APIRequestContext makes requests at the network level through
      the real Chromium process — same TLS fingerprint, IP, and browser headers
      as a real user — but it is NOT subject to browser CORS restrictions.
      This bypasses Distil Networks bot-detection on datacenter IPs.
    """
    print("  Launching browser for auth …")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Australia/Sydney",
            extra_http_headers={"accept-language": "en-US,en;q=0.9"},
        )

        # Visit F1 account page first to establish cookies + pass JS challenge
        print("  Visiting F1 account page …")
        page = await context.new_page()
        try:
            await page.goto(
                "https://account.formula1.com/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  ⚠️  Page visit warning (continuing): {e}")

        # Use APIRequestContext — network-level POST, not subject to CORS
        print("  Posting auth via APIRequestContext …")
        response = await context.request.post(
            "https://api.formula1.com/v2/account/subscriber/authenticate/by-password",
            headers={
                "Content-Type":   "application/json",
                "apikey":         "fCUCjWrKPu9ylJwRAv8BpGLEgiAuThx7",
                "origin":         "https://account.formula1.com",
                "referer":        "https://account.formula1.com/",
                "authority":      "api.formula1.com",
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            },
            data=json.dumps({
                "Login":               EMAIL,
                "Password":            PASSWORD,
                "DistributionChannel": "d861e38f-05ea-4063-8776-a7e2b6d885a4",
            }),
        )

        status = response.status
        body   = await response.text()
        await browser.close()

    print(f"  Auth response status: {status}")
    print(f"  Auth response preview: {body[:200]}")

    if status != 200:
        raise RuntimeError(f"Auth failed {status}: {body[:500]}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"Auth response not JSON: {body[:500]}")

    token = (
        data.get("data", {}).get("subscriptionToken")
        or data.get("subscriptionToken")
        or data.get("token")
    )
    if not token:
        raise RuntimeError(f"No token found in response: {data}")

    print("  ✅ Authenticated.")
    return token


# ─────────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────────
async def api_get(session: aiohttp.ClientSession, path: str, token: str) -> dict:
    url = f"{BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "apikey":        "fCUCjWrKPu9ylJwRAv8BpGLEgiAuThx7",
    }
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
        "id":             d.get("id"),
        "short_name":     d.get("short_name", ""),
        "full_name":      f"{d.get('first_name','')} {d.get('last_name','')}".strip(),
        "team":           d.get("team_name", ""),
        "team_tag":       TEAM_TAG_MAP.get(d.get("team_id"), ""),
        "price":          round(d.get("price", 0) / 1_000_000, 1),
        "total_points":   d.get("total_points", 0),
        "points_this_gw": d.get("score", 0),
    }


def clean_constructor(c: dict) -> dict:
    return {
        "id":             c.get("id"),
        "short_name":     c.get("short_name", c.get("name", "")),
        "full_name":      c.get("name", ""),
        "team_tag":       TEAM_TAG_MAP.get(c.get("id"), ""),
        "price":          round(c.get("price", 0) / 1_000_000, 1),
        "total_points":   c.get("total_points", 0),
        "points_this_gw": c.get("score", 0),
    }


# ─────────────────────────────────────────────────────────────────
# MAIN SYNC
# ─────────────────────────────────────────────────────────────────
async def sync(session: aiohttp.ClientSession, token: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── 1. Gameweeks ─────────────────────────────────────────────
    all_gws_raw = await api_get(session, "/gameweeks", token)
    all_gws     = (
        all_gws_raw.get("gameweeks", all_gws_raw)
        if isinstance(all_gws_raw, dict) else all_gws_raw
    )

    # ── 2. Overall players ────────────────────────────────────────
    players_raw = await api_get(session, "/players", token)
    all_players = players_raw.get("players", [])
    drivers_all = [clean_driver(p)      for p in all_players if not p.get("is_constructor", False)]
    constrs_all = [clean_constructor(p) for p in all_players if     p.get("is_constructor", False)]
    print(f"  Players: {len(drivers_all)} drivers, {len(constrs_all)} constructors")

    # ── 3. Per-gameweek stats ─────────────────────────────────────
    gw_records = []
    for gw in (all_gws if isinstance(all_gws, list) else []):
        gw_id    = gw.get("id") or gw.get("gameweek_id")
        gw_round = gw.get("race_id") or gw.get("round") or gw_id
        gw_label = gw.get("name") or f"R{gw_round}"
        if not gw.get("finished", False):
            continue
        gw_data    = await api_get(session, f"/players?gameweek={gw_id}", token)
        gw_players = gw_data.get("players", [])
        gw_records.append({
            "round":        gw_round,
            "gp":           gw_label,
            "date":         gw.get("deadline_date", "")[:10],
            "drivers":      [clean_driver(p)      for p in gw_players if not p.get("is_constructor", False)],
            "constructors": [clean_constructor(p) for p in gw_players if     p.get("is_constructor", False)],
        })

    with open("f1_fantasy.json", "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": ts,
            "overall":      {"drivers": drivers_all, "constructors": constrs_all},
            "gameweeks":    gw_records,
        }, f, indent=2, ensure_ascii=False)
    print(f"✅ f1_fantasy.json — {len(drivers_all)} drivers, {len(constrs_all)} constructors, {len(gw_records)} GWs")

    # ── 4. League teams ───────────────────────────────────────────
    if not LEAGUE_ID:
        print("⚠️  F1_FANTASY_LEAGUE_ID not set — skipping league sync.")
        return

    league_raw       = await api_get(session, f"/leagues/{LEAGUE_ID}", token)
    league_standings = league_raw.get("standings", league_raw.get("league", {}).get("standings", []))

    teams_output = {
        "last_updated": ts,
        "league_id":    LEAGUE_ID,
        "league_name":  league_raw.get("name", "Baby Formula Championship"),
        "players":      [],
        "rounds":       [],
    }

    for entry in league_standings:
        uid    = str(entry.get("user_id") or entry.get("id", ""))
        our_id = PLAYER_MAP.get(uid, uid)
        teams_output["players"].append({
            "id":          our_id,
            "name":        entry.get("team_name") or entry.get("name") or our_id,
            "emoji":       "👤",
            "f1_user_id":  uid,
        })

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
            uid       = str(entry.get("user_id") or entry.get("id", ""))
            our_id    = PLAYER_MAP.get(uid, uid)
            picks_raw = await api_get(session, f"/picks/{uid}?gameweek={gw_id}", token)
            picks     = picks_raw.get("picks", [])

            pick_list = []
            for p in picks:
                is_con = p.get("is_constructor", False)
                pid    = p.get("player_id") or p.get("id")
                match  = next((x for x in all_players if x.get("id") == pid), {})
                pick_list.append({
                    "id":           pid,
                    "short_name":   match.get("short_name", p.get("short_name", "")),
                    "full_name":    f"{match.get('first_name','')} {match.get('last_name','')}".strip() or p.get("name",""),
                    "team":         match.get("team_name", ""),
                    "team_tag":     TEAM_TAG_MAP.get(match.get("id") if is_con else match.get("team_id"), ""),
                    "type":         "constructor" if is_con else "driver",
                    "is_star":      p.get("is_captain") or p.get("is_star") or False,
                    "price":        round((p.get("price") or match.get("price", 0)) / 1_000_000, 1),
                    "price_change": round((p.get("selling_price", 0) - p.get("purchase_price", p.get("price", 0))) / 1_000_000, 2) if finished else None,
                    "points":       p.get("points") if finished else None,
                })

            round_record["teams"].append({
                "player_id":        our_id,
                "total_points":     sum(pk["points"] or 0 for pk in pick_list) if finished else None,
                "team_value":       round(sum(pk["price"] or 0 for pk in pick_list), 1),
                "budget_remaining": round((picks_raw.get("budget") or picks_raw.get("bank", 0)) / 1_000_000, 1),
                "picks":            pick_list,
            })

        teams_output["rounds"].append(round_record)

    with open("f1_teams.json", "w", encoding="utf-8") as f:
        json.dump(teams_output, f, indent=2, ensure_ascii=False)
    print(f"✅ f1_teams.json — {len(teams_output['players'])} players, {len(teams_output['rounds'])} rounds")


async def main():
    if not EMAIL or not PASSWORD:
        print("❌ F1_FANTASY_EMAIL / F1_FANTASY_PASSWORD not set.")
        sys.exit(1)

    # Auth via real browser (bypasses bot-detection)
    token = await authenticate_with_browser()

    # All subsequent calls use fast async HTTP
    async with aiohttp.ClientSession() as session:
        await sync(session, token)


if __name__ == "__main__":
    asyncio.run(main())
