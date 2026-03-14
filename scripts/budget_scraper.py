import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────
# Budget Builder actual DOM column layout (10 cells per data row):
#
#  0  : Tag          e.g. "RUS"
#  1  : Price        e.g. "27.7"
#  2  : Pts          season rolling total (may be "-" mid-week)
#  3  : Pts R1       points from most recent completed race
#  4  : xPts         expected points
#  5  : -0.3 Odds    e.g. "10% (≤-6)"
#  6  : -0.1 Odds    e.g. "1% (-5)"
#  7  : +0.1 Odds    e.g. "2% (11)"
#  8  : +0.3 Odds    e.g. "87% (28)"
#  9  : R2 xΔ$       e.g. "+0.23"
#
# Tables:  index 0 = Drivers,  index 1 = Constructors
# Tier rows: single <td colspan=N> with text like "Tier A (>=18.5M)"
# ─────────────────────────────────────────────────────────────────


def parse_pct(raw: str) -> int | None:
    """Extract leading integer percentage from a string like '87% (≤28)'."""
    if not raw:
        return None
    m = re.match(r"(\d+)%", raw.strip())
    return int(m.group(1)) if m else None


def clean_val(raw: str) -> str:
    """Strip stray currency symbols and whitespace."""
    return raw.replace("$", "").strip()


async def scrape_table(table, table_index: int) -> list[dict]:
    """
    Scrape one <table> element.
    Returns list of entry dicts, each with a 'tier' key ('A' or 'B').

    Actual Budget Builder column layout (10 cells per data row):
      0  : Tag          e.g. "RUS"
      1  : Price        e.g. "27.7"
      2  : Pts          season rolling total (may be "-" mid-week)
      3  : Pts R1       points from most recent completed race
      4  : xPts         expected points
      5  : -0.3 Odds    e.g. "10% (≤-6)"
      6  : -0.1 Odds    e.g. "1% (-5)"
      7  : +0.1 Odds    e.g. "2% (11)"
      8  : +0.3 Odds    e.g. "87% (28)"
      9  : R2 xΔ$       e.g. "+0.23"
    """
    entries = []
    current_tier       = None
    current_tier_label = None   # full label e.g. "Tier A (>=18.5M)"

    rows = await table.locator("tbody tr").all()
    for row in rows:
        cells = await row.locator("td").all()
        texts = [(await cell.inner_text()).strip() for cell in cells]

        # ── Tier header row: single cell spanning all columns ──
        if len(texts) == 1:
            m = re.search(r"Tier\s+([AB])", texts[0], re.IGNORECASE)
            if m:
                current_tier       = m.group(1).upper()
                current_tier_label = texts[0].strip()
            continue

        # ── Need at least 10 columns for a data row ──
        if len(texts) < 10:
            continue

        tag       = texts[0]
        price     = clean_val(texts[1])
        pts       = texts[2]      # season rolling total (or "-")
        pts_r1    = texts[3]      # most recent race pts
        xpts      = texts[4]      # expected pts
        odds_m03  = texts[5]      # -0.3 odds  "10% (≤-6)"
        odds_m01  = texts[6]      # -0.1 odds
        odds_p01  = texts[7]      # +0.1 odds
        odds_p03  = texts[8]      # +0.3 odds
        r2_change = texts[9]      # R2 xΔ$  "+0.23"

        # Skip blank or column-header placeholder rows
        if not tag or tag in ("-", "DR", "CR", "Pts", "xPts", "$"):
            continue

        entry = {
            "name":         tag,
            "tier":         current_tier,
            "tier_label":   current_tier_label,
            "price":        price,
            "pts":          pts,
            "pts_r1":       pts_r1,
            "xpts":         xpts,
            "odds_m03":     odds_m03,
            "odds_m01":     odds_m01,
            "odds_p01":     odds_p01,
            "odds_p03":     odds_p03,
            # Pre-computed % ints for easy frontend sorting
            "odds_m03_pct": parse_pct(odds_m03),
            "odds_m01_pct": parse_pct(odds_m01),
            "odds_p01_pct": parse_pct(odds_p01),
            "odds_p03_pct": parse_pct(odds_p03),
            "r2_change":    r2_change,
        }
        entries.append(entry)

    return entries


async def run_scraper():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        data_output = {
            "last_updated":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_url":       "https://f1fantasytools.com/budget-builder",
            "simulation_label": "",
            "drivers":          [],
            "constructors":     [],
        }

        try:
            print("Fetching Budget Builder data …")
            await page.goto(
                "https://f1fantasytools.com/budget-builder",
                wait_until="networkidle",
                timeout=60_000,
            )
            await page.wait_for_selector("table", timeout=30_000)

            # ── Grab simulation label e.g. "China, Post-SQ v2." ──
            try:
                sim_label = await page.evaluate(
                    "() => { const s = document.querySelector('select'); "
                    "return s ? s.options[s.selectedIndex].text.trim() : ''; }"
                )
                if not sim_label:
                    sim_el    = page.locator("select").first
                    sim_label = (await sim_el.input_value()).strip()
                data_output["simulation_label"] = sim_label
            except Exception as e:
                print(f"  ⚠️  Could not read simulation label: {e}")

            # ── Scrape Drivers (table 0) and Constructors (table 1) ──
            tables = await page.locator("table").all()
            if len(tables) < 2:
                raise RuntimeError(
                    f"Expected ≥2 tables on page, found {len(tables)}"
                )

            data_output["drivers"]      = await scrape_table(tables[0], 0)
            data_output["constructors"] = await scrape_table(tables[1], 1)

            d_count = len(data_output["drivers"])
            c_count = len(data_output["constructors"])
            print(f"  ✅  Drivers: {d_count} rows   Constructors: {c_count} rows")

            # If we got nothing, print debug rows to diagnose column layout changes
            if d_count == 0:
                print("  ⚠️  No driver rows — printing first 4 rows for diagnosis:")
                rows = await tables[0].locator("tbody tr").all()
                for i, row in enumerate(rows[:4]):
                    cells = await row.locator("td").all()
                    texts = [(await c.inner_text()).strip() for c in cells]
                    print(f"     Row {i} ({len(texts)} cols): {texts}")

        except Exception as e:
            print(f"Scraper error: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

        with open("f1_budget_data.json", "w", encoding="utf-8") as f:
            json.dump(data_output, f, indent=4, ensure_ascii=False)
        print("✅  f1_budget_data.json written.")


if __name__ == "__main__":
    asyncio.run(run_scraper())
