import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────
# f1fantasytools.com/budget-builder — "Required Points" view
#
# This scraper is designed to be SEASON-RESILIENT:
#   - Columns grow as races complete (R0, R1, R2 … Rn)
#   - Price-change labels differ per tier (Tier A: ±0.3/±0.1,
#     Tier B: ±0.6/±0.2)
#   - We detect ALL columns dynamically from the tier header row
#     and the DR/CR subheader row, so adding new race columns
#     never breaks anything.
#
# Data model stored per entry:
#   name, tier, tier_label, price,
#   race_pts: { "R0": "-", "R1": "50", "R2": "32", ... }  ← grows each race
#   req_pts:  { "-0.3": "≤-17", "-0.1": "-16", "+0.1": "1", "+0.3": "17" }
#   price_changes: ["-0.3", "-0.1", "+0.1", "+0.3"]       ← from tier header
# ─────────────────────────────────────────────────────────────────

DRIVER_TAGS = {
    "VER","NOR","PIA","RUS","ANT","LEC","HAM","ALO","STR","GAS","COL",
    "SAI","ALB","BEA","OCO","LAW","HUL","BOT","HAD","BOR","LIN","PER"
}
CONSTRUCTOR_TAGS = {
    "MER","FER","RED","MCL","AMR","ALP","WIL","HAA","AUD","VRB","CAD","AST"
}


def clean_price(raw: str) -> str:
    return raw.replace("$", "").strip()


def is_round_label(t: str) -> bool:
    """True for column headers like R0, R1, R2 … R24."""
    return bool(re.match(r'^R\d+$', t.strip()))


def is_price_change(t: str) -> bool:
    """True for values like -0.3, -0.1, +0.1, +0.3, -0.6, +0.6 etc."""
    return bool(re.match(r'^[+\-]\d+\.\d+$', t.strip()))


async def identify_and_scrape(table) -> tuple[str | None, list[dict]]:
    """
    Fully dynamic column detection — works regardless of how many
    race-points columns (R0, R1, R2 …) have been added so far.

    Per-tier state (reset each time a new tier header row is seen):
      col_map: maps column index → field name
        - index 0        → "name"  (tag)
        - index 1        → "price" ($)
        - indices 2..N   → round labels  "R0", "R1", "R2" … (from tier header)
        - indices N+1..M → price-change labels  "-0.3", "-0.1", "+0.1", "+0.3"
                           (also from tier header)
    """
    table_type        = None
    current_tier      = None
    current_tier_label= None
    current_col_map   = {}   # col_idx -> field_name
    current_pc_labels = []   # e.g. ["-0.3", "-0.1", "+0.1", "+0.3"]
    entries           = []

    all_rows = await table.locator("tr").all()

    for row in all_rows:
        cells = await row.locator("td, th").all()
        texts = [(await c.inner_text()).strip() for c in cells]
        # Flatten any newlines inside cells
        texts = [re.sub(r'\s+', ' ', t).strip() for t in texts]
        if not texts:
            continue

        t0 = texts[0]

        # ── Tier group header row ────────────────────────────────
        # e.g. ["Tier A (>=18.5M)", "R0", "R1", "-0.3", "-0.1", "+0.1", "+0.3"]
        # or later in season: ["Tier A...", "R0","R1","R2","R3", "-0.3","-0.1","+0.1","+0.3"]
        tier_m = re.search(r'Tier\s+([AB])', t0, re.IGNORECASE)
        if tier_m:
            current_tier        = tier_m.group(1).upper()
            current_tier_label  = t0
            current_col_map     = {}
            current_pc_labels   = []

            # col 0 = tag (DR/CR), col 1 = $ — always fixed
            # Starting from col 2: scan for Rn labels then price-change labels
            round_cols = []
            pc_cols    = []
            for i, t in enumerate(texts[2:], start=2):
                if is_round_label(t):
                    round_cols.append((i, t))
                elif is_price_change(t):
                    pc_cols.append((i, t))

            current_col_map[0] = "name"
            current_col_map[1] = "price"
            for idx, lbl in round_cols:
                current_col_map[idx] = lbl          # e.g. "R0", "R1", "R2"
            for idx, lbl in pc_cols:
                current_col_map[idx] = lbl          # e.g. "-0.3", "+0.1"
            current_pc_labels = [lbl for _, lbl in pc_cols]

            print(f"  Tier {current_tier}: col_map={current_col_map}, pc_labels={current_pc_labels}")
            continue

        # ── Column sub-header row (DR/CR | $ | Pts | Pts | …) ───
        if t0 in ("DR", "CR"):
            table_type = "drivers" if t0 == "DR" else "constructors"
            continue

        # ── Skip short rows ──────────────────────────────────────
        if len(texts) < 3:
            continue

        # ── Data row ─────────────────────────────────────────────
        tag = t0
        if not re.match(r'^[A-Z]{2,5}$', tag):
            continue
        if tag not in DRIVER_TAGS and tag not in CONSTRUCTOR_TAGS:
            continue

        # Build entry using col_map for maximum resilience
        entry = {
            "name":          tag,
            "tier":          current_tier,
            "tier_label":    current_tier_label,
            "price_changes": list(current_pc_labels),
            "price":         clean_price(texts[1]) if len(texts) > 1 else "",
            "race_pts":      {},   # { "R0": "50", "R1": "32", … }
            "req_pts":       {},   # { "-0.3": "≤-17", "+0.1": "1", … }
        }

        for col_idx, field in current_col_map.items():
            if col_idx >= len(texts):
                continue
            val = texts[col_idx]
            if field in ("name", "price"):
                continue
            elif is_round_label(field):
                entry["race_pts"][field] = val
            elif is_price_change(field):
                entry["req_pts"][field] = val

        entries.append(entry)

    # Guess type from tags if not detected
    if table_type is None and entries:
        tags   = {e["name"] for e in entries}
        d_hits = len(tags & DRIVER_TAGS)
        c_hits = len(tags & CONSTRUCTOR_TAGS)
        table_type = "drivers" if d_hits >= c_hits else "constructors"
        print(f"  ⚠️  Table type guessed: {table_type} (d={d_hits}, c={c_hits})")

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

            # ── Select "Required Points" view ────────────────────
            try:
                for sel in await page.locator("select").all():
                    for opt in await sel.locator("option").all():
                        txt = (await opt.inner_text()).strip()
                        if "required" in txt.lower():
                            await sel.select_option(label=txt)
                            print(f"  ✅ View set to: {txt}")
                            await page.wait_for_timeout(1200)
                            break
            except Exception as e:
                print(f"  ⚠️  Dropdown: {e}")

            await page.wait_for_timeout(800)

            # ── Scrape all tables ────────────────────────────────
            tables = await page.locator("table").all()
            print(f"  Found {len(tables)} table(s)")

            drivers_list      = []
            constructors_list = []

            for ti, table in enumerate(tables):
                rows = await table.locator("tr").all()
                print(f"\n  === Table {ti} ({len(rows)} rows) ===")
                for i, row in enumerate(rows[:6]):
                    cells = await row.locator("td, th").all()
                    txts  = [(await c.inner_text()).strip() for c in cells]
                    print(f"    row {i}: {txts}")

                ttype, entries = await identify_and_scrape(table)
                print(f"  → type={ttype}, entries={len(entries)}")

                if ttype == "drivers":
                    drivers_list.extend(entries)
                elif ttype == "constructors":
                    constructors_list.extend(entries)

            # Deduplicate (keep first occurrence per name)
            seen_d, seen_c = set(), set()
            data_output["drivers"]      = [e for e in drivers_list      if e["name"] not in seen_d and not seen_d.add(e["name"])]
            data_output["constructors"] = [e for e in constructors_list  if e["name"] not in seen_c and not seen_c.add(e["name"])]

            print(f"\n  ✅ Drivers ({len(data_output['drivers'])}):      {[e['name'] for e in data_output['drivers']]}")
            print(f"  ✅ Constructors ({len(data_output['constructors'])}): {[e['name'] for e in data_output['constructors']]}")

            # Show what race columns were captured
            if data_output["drivers"]:
                sample = data_output["drivers"][0]
                print(f"  Race pts columns: {list(sample['race_pts'].keys())}")
                print(f"  Req pts columns:  {list(sample['req_pts'].keys())}")

            if len(data_output["drivers"]) < 10:
                print("  ⚠️  WARNING: fewer than 10 drivers scraped")
            if len(data_output["constructors"]) < 5:
                print("  ⚠️  WARNING: fewer than 5 constructors scraped")

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
