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
    Scrape one <table> (index 0 = Drivers, index 1 = Constructors).

    Reads <thead> first to detect actual column positions, so it works
    regardless of how many columns the site is currently showing.

    Expected column names the site uses:
        DR / CR       → name
        $             → price
        Pts  (no R)   → pts  (season total, R0)
        Pts R1        → pts_r1
        xPts          → xpts
        -0.3          → odds_m03
        -0.1          → odds_m01
        +0.1          → odds_p01
        +0.3          → odds_p03
        xΔ$ / R2      → r2_change
    """
    entries        = []
    current_tier   = None
    current_tier_label = None

    # ── Detect column positions from <thead> ──────────────────────
    col_map = {}  # field_name -> column index
    try:
        all_header_rows = await table.locator("thead tr").all()
        # Use the last header row — it has the most granular labels
        for hrow in all_header_rows:
            cells = await hrow.locator("th, td").all()
            for i, cell in enumerate(cells):
                raw = (await cell.inner_text()).strip()
                t   = raw.lower().replace("\n", " ").strip()
                if t in ("dr", "cr"):                              col_map["name"]      = i
                elif t == "$":                                     col_map["price"]     = i
                elif t in ("pts", "r0", "pts r0"):                col_map.setdefault("pts", i)
                elif "r1" in t and "pts" in t:                    col_map["pts_r1"]    = i
                elif "xpts" in t or t == "r2 xpts" or (t.startswith("x") and "pt" in t):
                    col_map["xpts"] = i
                elif "-0.3" in t or "−0.3" in t:                  col_map["odds_m03"]  = i
                elif "-0.1" in t or "−0.1" in t:                  col_map["odds_m01"]  = i
                elif "+0.1" in t:                                  col_map["odds_p01"]  = i
                elif "+0.3" in t:                                  col_map["odds_p03"]  = i
                elif "xδ" in t or "xΔ" in raw or ("r2" in t and "x" in t):
                    col_map["r2_change"] = i

        print(f"  📋  Table {table_index} column map: {col_map}")
    except Exception as e:
        print(f"  ⚠️  Header detection failed: {e}")

    # Fallback to known 10-column layout if detection got < 4 fields
    if len(col_map) < 4:
        print("  ℹ️  Falling back to default 10-col layout")
        col_map = {
            "name": 0, "price": 1, "pts": 2, "pts_r1": 3, "xpts": 4,
            "odds_m03": 5, "odds_m01": 6, "odds_p01": 7, "odds_p03": 8,
            "r2_change": 9,
        }

    def pick(texts: list[str], key: str, default: str = "") -> str:
        idx = col_map.get(key)
        return texts[idx] if idx is not None and idx < len(texts) else default

    # ── Scrape body rows ──────────────────────────────────────────
    rows = await table.locator("tbody tr").all()
    for row in rows:
        cells = await row.locator("td").all()
        texts = [(await c.inner_text()).strip() for c in cells]

        # Tier header — single cell spanning all cols
        if len(texts) == 1:
            m = re.search(r"Tier\s+([AB])", texts[0], re.IGNORECASE)
            if m:
                current_tier       = m.group(1).upper()
                current_tier_label = texts[0].strip()
            continue

        # Need at least 6 cells for a data row
        if len(texts) < 6:
            continue

        tag = pick(texts, "name") or texts[0]

        # Skip column-header ghost rows
        if not tag or tag in ("-", "DR", "CR", "Pts", "xPts", "$", "R0", "R1", "R2"):
            continue

        odds_m03  = pick(texts, "odds_m03")
        odds_m01  = pick(texts, "odds_m01")
        odds_p01  = pick(texts, "odds_p01")
        odds_p03  = pick(texts, "odds_p03")

        entry = {
            "name":         tag,
            "tier":         current_tier,
            "tier_label":   current_tier_label,
            "price":        clean_val(pick(texts, "price")),
            "pts":          pick(texts, "pts"),
            "pts_r1":       pick(texts, "pts_r1"),
            "xpts":         pick(texts, "xpts"),
            "odds_m03":     odds_m03,
            "odds_m01":     odds_m01,
            "odds_p01":     odds_p01,
            "odds_p03":     odds_p03,
            "odds_m03_pct": parse_pct(odds_m03),
            "odds_m01_pct": parse_pct(odds_m01),
            "odds_p01_pct": parse_pct(odds_p01),
            "odds_p03_pct": parse_pct(odds_p03),
            "r2_change":    pick(texts, "r2_change"),
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
                print("  ⚠️  No driver rows — printing thead + first 4 tbody rows:")
                try:
                    hrows = await tables[0].locator("thead tr").all()
                    for i, hr in enumerate(hrows):
                        ths = await hr.locator("th, td").all()
                        htexts = [(await t.inner_text()).strip() for t in ths]
                        print(f"     thead row {i} ({len(htexts)} cols): {htexts}")
                except Exception as he:
                    print(f"     Could not read thead: {he}")
                rows = await tables[0].locator("tbody tr").all()
                for i, row in enumerate(rows[:4]):
                    cells = await row.locator("td").all()
                    texts = [(await c.inner_text()).strip() for c in cells]
                    print(f"     tbody row {i} ({len(texts)} cols): {texts}")

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
