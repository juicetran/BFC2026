import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

SKIP_TAGS = {"-", "DR", "CR", "Pts", "xPts", "$", "R0", "R1", "R2",
             "Odds (pts)", "Odds(pts)", "xΔ$", ""}


def clean_price(raw: str) -> str:
    return raw.replace("$", "").strip()


async def set_required_points_view(page) -> None:
    """Select 'Required Points' from the dropdown."""
    try:
        # Try the visible dropdown
        await page.wait_for_selector("select, [role='listbox'], button", timeout=5000)
        # Look for a select with Required Points option
        selects = await page.locator("select").all()
        for sel in selects:
            opts = await sel.locator("option").all()
            for opt in opts:
                txt = (await opt.inner_text()).strip()
                if "required" in txt.lower():
                    await sel.select_option(label=txt)
                    print(f"  ✅ Selected: {txt}")
                    await page.wait_for_timeout(1000)
                    return
        # Fallback: click dropdown button and select
        btns = await page.locator("button").all()
        for btn in btns:
            txt = (await btn.inner_text()).strip()
            if "required" in txt.lower() or "points" in txt.lower():
                await btn.click()
                await page.wait_for_timeout(500)
                return
    except Exception as e:
        print(f"  ⚠️  Could not set Required Points view: {e}")


async def scrape_one_table(table) -> tuple[str, list[dict]]:
    """
    Scrape one table. Returns (table_type, entries) where
    table_type is 'drivers' or 'constructors' based on DR/CR header.
    
    Column layout (Required Points view):
      DR/CR | $ | R0 Pts | R1 Pts | -0.3 Pts | -0.1 Pts | +0.1 Pts | +0.3 Pts
    """
    entries = []
    current_tier = None
    current_tier_label = None
    table_type = None

    all_rows = await table.locator("tr").all()

    for row in all_rows:
        cells = await row.locator("td, th").all()
        texts = [(await c.inner_text()).strip() for c in cells]
        if not texts:
            continue

        joined = " ".join(texts)

        # ── Detect table type from DR/CR header ──────────────────
        if "DR" in texts and "$" in texts:
            table_type = "drivers"
            continue
        if "CR" in texts and "$" in texts:
            table_type = "constructors"
            continue

        # ── Tier header (single spanning cell) ──────────────────
        if len(texts) == 1:
            m = re.search(r"Tier\s+([AB])", texts[0], re.IGNORECASE)
            if m:
                current_tier = m.group(1).upper()
                current_tier_label = texts[0].strip()
            continue

        # ── Group header row: "Tier A (>=18.5M) | R0 | R1 | -0.3 | ..." ─
        # These rows have the tier label + column group names
        if texts and re.search(r"Tier\s+[AB]", texts[0], re.IGNORECASE):
            m = re.search(r"Tier\s+([AB])", texts[0], re.IGNORECASE)
            if m:
                current_tier = m.group(1).upper()
                current_tier_label = texts[0].strip()
            continue

        # ── Skip rows with fewer than 4 cells ───────────────────
        if len(texts) < 4:
            continue

        # ── Skip pure header/label rows ─────────────────────────
        # A data row always starts with a 2-3 char driver tag or constructor tag
        tag = texts[0].strip()
        if not tag or tag in SKIP_TAGS:
            continue
        # Skip if it looks like a header row (all entries are labels)
        if tag in ("DR", "CR", "Pts", "R0", "R1", "-0.3", "-0.1", "+0.1", "+0.3"):
            continue
        # Tags are short codes like RUS, ANT, MER, VRB etc
        if len(tag) > 5 or not re.match(r'^[A-Z]{2,5}$', tag):
            continue

        # ── Data row — columns are positional ───────────────────
        # 0: tag, 1: price, 2: R0 pts, 3: R1 pts,
        # 4: -0.3 req, 5: -0.1 req, 6: +0.1 req, 7: +0.3 req
        def g(i, default=""):
            return texts[i].strip() if i < len(texts) else default

        entries.append({
            "name":       tag,
            "tier":       current_tier,
            "tier_label": current_tier_label,
            "price":      clean_price(g(1)),
            "pts_r0":     g(2),   # season total (R0)
            "pts_r1":     g(3),   # last race (R1)
            "pts_m03":    g(4),   # required for -$0.3
            "pts_m01":    g(5),   # required for -$0.1
            "pts_p01":    g(6),   # required for +$0.1
            "pts_p03":    g(7),   # required for +$0.3
        })

    return table_type, entries


async def run_scraper():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        data_output = {
            "last_updated":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_url":    "https://f1fantasytools.com/budget-builder",
            "view":          "required_points",
            "drivers":       [],
            "constructors":  [],
        }

        try:
            print("Loading Budget Builder …")
            await page.goto(
                "https://f1fantasytools.com/budget-builder",
                wait_until="networkidle",
                timeout=60_000,
            )
            await page.wait_for_selector("table", timeout=30_000)
            print("  ✅ Page loaded")

            # Set Required Points view
            await set_required_points_view(page)
            await page.wait_for_timeout(1500)

            # Find all tables and identify drivers vs constructors
            tables = await page.locator("table").all()
            print(f"  Found {len(tables)} table(s)")

            drivers_entries = []
            constructors_entries = []

            for ti, table in enumerate(tables):
                # Dump first few rows to understand structure
                rows = await table.locator("tr").all()
                print(f"\n  === Table {ti} ({len(rows)} rows) ===")
                for i, row in enumerate(rows[:5]):
                    cells = await row.locator("td, th").all()
                    txts = [(await c.inner_text()).strip() for c in cells]
                    print(f"    row {i}: {txts}")

                table_type, entries = await scrape_one_table(table)
                print(f"  Table {ti}: type={table_type}, entries={len(entries)}")

                if table_type == "drivers":
                    drivers_entries.extend(entries)
                elif table_type == "constructors":
                    constructors_entries.extend(entries)
                else:
                    # Try to guess from content
                    tags = [e["name"] for e in entries]
                    driver_tags = {"VER","NOR","PIA","RUS","ANT","LEC","HAM","ALO",
                                   "STR","GAS","COL","SAI","ALB","BEA","OCO","LAW",
                                   "HUL","BOT","HAD","BOR","LIN","PER"}
                    constr_tags = {"MER","FER","RED","MCL","AMR","ALP","WIL",
                                   "HAA","AUD","VRB","CAD","AST"}
                    d_hits = sum(1 for t in tags if t in driver_tags)
                    c_hits = sum(1 for t in tags if t in constr_tags)
                    print(f"    Guessing: driver_hits={d_hits}, constr_hits={c_hits}")
                    if d_hits > c_hits:
                        drivers_entries.extend(entries)
                    elif c_hits > d_hits:
                        constructors_entries.extend(entries)

            # Deduplicate by name (keep first occurrence)
            seen_d, seen_c = set(), set()
            data_output["drivers"]      = [e for e in drivers_entries     if e["name"] not in seen_d and not seen_d.add(e["name"])]
            data_output["constructors"] = [e for e in constructors_entries if e["name"] not in seen_c and not seen_c.add(e["name"])]

            print(f"\n  ✅ Final: Drivers={len(data_output['drivers'])}, Constructors={len(data_output['constructors'])}")
            print(f"  Driver tags:      {[e['name'] for e in data_output['drivers']]}")
            print(f"  Constructor tags: {[e['name'] for e in data_output['constructors']]}")

        except Exception as e:
            print(f"Scraper error: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

        with open("f1_budget_data.json", "w", encoding="utf-8") as f:
            json.dump(data_output, f, indent=4, ensure_ascii=False)
        print("\n✅  f1_budget_data.json written.")


if __name__ == "__main__":
    asyncio.run(run_scraper())
