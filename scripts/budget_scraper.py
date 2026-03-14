import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────
# Budget Builder column layout (after removing any hidden/grouped
# header rows, each data row has these cells in order):
#
#  0  : Tag (e.g. RUS, ANT, MER, FER …)
#  1  : $ price
#  2  : Pts (actual points)
#  3  : xPts (expected points)
#  4  : Odds text for -0.3  e.g. "10% (5)"
#  5  : Odds text for -0.1  e.g. "1% (-5)"
#  6  : Odds text for +0.1  e.g. "2% (11)"
#  7  : Odds text for +0.3  e.g. "87% (26)"
#  8  : R2 price-change     e.g. "+0.23"
#
# The page has two tables: drivers (index 0), constructors (index 1).
# Tier rows contain a single <td colspan=...> with text "Tier A" or
# "Tier B" — we track the current tier across rows.
# ─────────────────────────────────────────────────────────────────

def parse_pct(raw: str) -> int | None:
    """Extract leading integer percentage from a string like '87% (26)'."""
    m = re.match(r"(\d+)%", raw.strip())
    return int(m.group(1)) if m else None


async def scrape_table(table, table_index: int) -> list[dict]:
    """
    Scrape one <table> element.  Returns a list of entry dicts.
    table_index 0 → drivers, 1 → constructors.
    """
    entries = []
    current_tier = None

    rows = await table.locator("tbody tr").all()
    for row in rows:
        cells = await row.locator("td").all()
        texts = []
        for cell in cells:
            texts.append((await cell.inner_text()).strip())

        # ── Tier header row (single wide cell like "Tier A (>=18.5M)") ──
        if len(texts) == 1:
            m = re.search(r"Tier\s+([AB])", texts[0], re.IGNORECASE)
            if m:
                current_tier = m.group(1).upper()
            continue

        # ── Need at least 9 columns for a data row ──
        if len(texts) < 9:
            continue

        tag      = texts[0]
        price    = texts[1]
        pts      = texts[2]
        xpts     = texts[3]
        odds_m03 = texts[4]   # "-0.3" odds column
        odds_m01 = texts[5]   # "-0.1" odds column
        odds_p01 = texts[6]   # "+0.1" odds column
        odds_p03 = texts[7]   # "+0.3" odds column
        r2_change = texts[8]  # R2 Δ$ e.g. "+0.23" or "-0.30"

        # Skip blank / header-looking rows
        if not tag or tag in ("-", "DR", "CR"):
            continue

        entry = {
            "name":      tag,
            "tier":      current_tier,
            "price":     price,
            "pts":       pts,
            "xpts":      xpts,
            "odds_m03":  odds_m03,
            "odds_m01":  odds_m01,
            "odds_p01":  odds_p01,
            "odds_p03":  odds_p03,
            # Pre-compute bare % ints for easy frontend sorting
            "odds_m03_pct": parse_pct(odds_m03),
            "odds_m01_pct": parse_pct(odds_m01),
            "odds_p01_pct": parse_pct(odds_p01),
            "odds_p03_pct": parse_pct(odds_p03),
            "r2_change": r2_change,
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

            # ── Grab simulation label (e.g. "China, Post-SQ v2") ──
            try:
                sim_el = page.locator("select >> nth=0")
                sim_label = (await sim_el.input_value()).strip()
                if not sim_label:
                    sim_label = await sim_el.inner_text()
                data_output["simulation_label"] = sim_label.strip()
            except Exception:
                pass

            # ── Scrape tables ──
            tables = await page.locator("table").all()
            if len(tables) < 2:
                raise RuntimeError(f"Expected ≥2 tables, found {len(tables)}")

            data_output["drivers"]      = await scrape_table(tables[0], 0)
            data_output["constructors"] = await scrape_table(tables[1], 1)

            print(
                f"  Drivers:      {len(data_output['drivers'])} rows\n"
                f"  Constructors: {len(data_output['constructors'])} rows"
            )

        except Exception as e:
            print(f"Scraper error: {e}")
        finally:
            await browser.close()

        with open("f1_data.json", "w", encoding="utf-8") as f:
            json.dump(data_output, f, indent=4, ensure_ascii=False)
        print("✅  f1_data.json written.")


if __name__ == "__main__":
    asyncio.run(run_scraper())