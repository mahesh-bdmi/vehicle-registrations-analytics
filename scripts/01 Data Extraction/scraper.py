#!/usr/bin/env python3
"""
Vahan Dashboard API Scraper
===========================
Faster alternative to scraper.py. Replays the PrimeFaces AJAX/xhtml
protocol directly with plain HTTP requests — no browser, no Playwright.

How it works:
  1. GET the dashboard page to extract the JSF ViewState token.
  2. AJAX POST to select state  → server returns updated RTO + Y-axis options.
  3. AJAX POST to set Y-axis and X-axis.
  4. For each year: AJAX POST to select year, then click Refresh.
  5. Parse the rendered HTML table from CDATA sections in the XML response.
  6. Paginate if the table has multiple pages (25 rows/page).
  7. Save as CSV per (state, rto, yaxis, xaxis, year).

Output format: CSV (rows = table rows, columns = table headers).
Output layout mirrors scraper.py so both tools share the same --out dir.

Usage:
  python3 scripts/api.py --list-options
  python3 scripts/api.py --yaxis "Vehicle Category" --xaxis "Fuel"
  python3 scripts/api.py --yaxis "Vehicle Category" --xaxis "Fuel" --state "Kerala" --year 2025
  python3 scripts/api.py --yaxis "Vehicle Category" --xaxis "Fuel" --state "Kerala" --all-rtos --start-year 2020
  python3 scripts/api.py --yaxis "Maker" --xaxis "Fuel" --state "Kerala" --rto "TRIVANDRUM" "KOLLAM"
"""

import argparse
import contextlib
import csv
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from queue import Empty, Queue

import httpx
from bs4 import BeautifulSoup

VAHAN_URL = "https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml"
FORM_ID = "masterLayout_formlogin"
_NULL_LOCK = contextlib.nullcontext()
ROWS_PER_PAGE = 25

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_AJAX_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/xml, text/xml, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://vahan.parivahan.gov.in",
    "Referer": VAHAN_URL,
    "Faces-Request": "partial/ajax",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

# Input names that are stable across page versions — used to exclude them
# when dynamically searching for the state / display-type dropdown names.
_KNOWN_INPUT_NAMES = {
    "selectedRto_input",
    "yaxisVar_input",
    "xaxisVar_input",
    "selectedYearType_input",
    "selectedYear_input",
}

# Fallback Refresh IDs used when dynamic discovery fails
_REFRESH_IDS_FALLBACK = ["j_idt66", "j_idt65"]
_SUBFILTER_REFRESH_ID_FALLBACK = "j_idt71"


# ── Utility ───────────────────────────────────────────────────────────────────


def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "_" for c in text).strip("_")


def clean_state_name(label: str) -> str:
    """
    Some page versions append an RTO count to the state label, e.g.
    'Kerala(87)'. Strip that suffix so the CSV 'State' column reads as
    'Kerala' instead of the raw label. No-op if the suffix isn't present.
    """
    return re.sub(r"\(\d+\)\s*$", "", label).strip()


def now_iso() -> str:
    """UTC timestamp for the FetchedAt column — unambiguous regardless of
    where the script runs, unlike a local timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_placeholder_rto(label: str) -> bool:
    """
    Some RTO dropdown entries have a blank name, e.g. '- - TG112( 26-FEB-2026 )'
    (compare to a real entry: 'MULUGU - TG37( 27-FEB-2026 )'). These are
    unassigned/placeholder office codes with no registration data for any
    year — skip them entirely rather than querying every year for nothing.
    """
    name_part = label.split(" - ")[0].strip()
    return name_part in ("", "-")


def strip_serial_column(headers: list[str], rows: list[list[str]]):
    """Drop a leading S.No / Sr.No / Sl.No serial-number column if present —
    it's meaningless once rows from many states/RTOs/years are combined."""
    idx = None
    for i, h in enumerate(headers):
        h_norm = h.strip().lower().replace(".", "").replace(" ", "")
        if h_norm in {"sno", "srno", "slno", "serialno"}:
            idx = i
            break
    if idx is None:
        return headers, rows
    new_headers = headers[:idx] + headers[idx + 1 :]
    new_rows = [r[:idx] + r[idx + 1 :] for r in rows]
    return new_headers, new_rows


# ── ViewState helpers ─────────────────────────────────────────────────────────


def extract_viewstate(html: str) -> str:
    for pat in [
        r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="javax\.faces\.ViewState"',
    ]:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise ValueError("javax.faces.ViewState not found in page HTML")


def extract_viewstate_xml(xml: str) -> str:
    m = re.search(
        r'<update\s+id="javax\.faces\.ViewState"><!\[CDATA\[(.*?)\]\]></update>',
        xml,
    )
    return m.group(1) if m else ""


# ── Option discovery ──────────────────────────────────────────────────────────


def parse_options(soup: BeautifulSoup, select_name: str) -> dict[str, str]:
    """Return {value: display_label} for all options in <select name=select_name>."""
    sel = soup.find("select", {"name": select_name})
    if not sel:
        return {}
    return {
        o.get("value", ""): o.get_text(strip=True)
        for o in sel.find_all("option")
        if o.get("value", "")
    }


def parse_options_from_xml(xml: str, select_name: str) -> dict[str, str]:
    """Extract select options from CDATA sections in a PrimeFaces AJAX response."""
    for cd in re.findall(r"<!\[CDATA\[(.*?)\]\]>", xml, re.DOTALL):
        soup = BeautifulSoup(cd, "html.parser")
        opts = parse_options(soup, select_name)
        if opts:
            return opts
    return {}


def parse_checkbox_options(soup: BeautifulSoup, table_id: str) -> dict[str, str]:
    """
    Return {value: display_label} for a PrimeFaces multi-checkbox group
    (<table id=table_id class="ui-selectmanycheckbox">) — used for the
    Vehicle Category (VhCatg), Fuel (fuel), and Vehicle Class (VhClass)
    filter panel on the left side of the dashboard.
    """
    table = soup.find("table", id=table_id)
    if not table:
        return {}
    opts = {}
    for inp in table.find_all("input", type="checkbox"):
        value = inp.get("value")
        cb_id = inp.get("id")
        label = table.find("label", attrs={"for": cb_id})
        if value and label:
            opts[value] = label.get_text(strip=True)
    return opts


def match_multiple(
    options: dict[str, str], queries: list[str]
) -> list[tuple[str, str]]:
    """Resolve several label queries against a {value: label} map, warning on any that don't match."""
    matched = []
    for q in queries:
        m = match_option(options, q)
        if m:
            matched.append(m)
            print(f"    matched: {m[1]!r} (value={m[0]})")
        else:
            print(f"    [WARN] '{q}' not found in this filter's options — skipping")
    return matched


def find_state_input_name(soup: BeautifulSoup) -> str | None:
    """
    Locate the hidden <select> name for the state dropdown.

    Definitive discriminator (confirmed by inspecting the live page HTML):
      - The state select always has value="-1" for the "All States" aggregate
        option, followed by 2-char alpha-only uppercase state codes (AN, AP…).
      - The display-type select (T/L/C/A) has no "-1" option.
      - No other select on the page matches both conditions.

    Falls back to known IDs seen across page versions.
    """
    for sel in soup.find_all("select"):
        name = sel.get("name", "")
        if not name or name in _KNOWN_INPUT_NAMES:
            continue
        all_vals = [
            o.get("value", "") for o in sel.find_all("option") if o.get("value", "")
        ]
        if "-1" not in all_vals:
            continue
        state_codes = [v for v in all_vals if v != "-1"]
        if state_codes and all(v.isalpha() and v.isupper() for v in state_codes[:10]):
            return name
    # Fallbacks: IDs observed across different page versions (add new ones here)
    for fallback in [
        "j_idt36_input",
        "j_idt34_input",
        "j_idt41_input",
        "j_idt45_input",
    ]:
        if soup.find("select", {"name": fallback}):
            return fallback
    return None


def find_display_input_name(soup: BeautifulSoup, state_name: str | None) -> str | None:
    """
    Locate the <select> name for the display-type dropdown
    (options: T=Thousand, L=Lakh, C=Crore, A=Actual).
    """
    for sel in soup.find_all("select"):
        name = sel.get("name", "")
        if not name or name in _KNOWN_INPUT_NAMES or name == state_name:
            continue
        vals = {
            o.get("value", "") for o in sel.find_all("option") if o.get("value", "")
        }
        if vals and vals <= {"T", "L", "C", "A"}:
            return name
    for fallback in ["j_idt25_input", "j_idt22_input", "j_idt28_input"]:
        if soup.find("select", {"name": fallback}):
            return fallback
    return None


def find_refresh_ids(soup: BeautifulSoup) -> list[str]:
    """
    Discover MAIN Refresh button IDs from the page HTML — explicitly
    EXCLUDING the vehicle sub-filter panel's own Refresh button(s), which
    are a separate control (see find_subfilter_refresh_id()). Confirmed via
    live testing: clicking the main refresh resets whatever VhCatg/fuel/
    VhClass checkboxes were set, so the two must be triggered as distinct,
    deliberate steps rather than "try each until one returns a table."
    The IDs are dynamic (j_idt66 today, but can shift with JSF re-renders).
    Falls back to a known list that covers observed page versions.
    """
    subfilter_panel = soup.find(id="filterLayout")
    subfilter_btn_ids = (
        {btn.get("id", "") for btn in subfilter_panel.find_all("button")}
        if subfilter_panel
        else set()
    )
    ids = [
        btn.get("id", "")
        for btn in soup.find_all("button")
        if btn.get_text(strip=True).lower() == "refresh"
        and btn.get("id", "")
        and btn.get("id", "") not in subfilter_btn_ids
    ]
    return ids or _REFRESH_IDS_FALLBACK


def find_subfilter_refresh_id(soup: BeautifulSoup) -> str | None:
    """
    Find the Refresh button that lives INSIDE the vehicle sub-filter panel
    (id="filterLayout" — the left-side panel with Vehicle Category / Fuel /
    Vehicle Class checkboxes). This is a separate button from the main
    refresh. Per confirmed live testing: the main refresh resets whatever
    VhCatg/fuel/VhClass values were set server-side, so applying those
    filters requires triggering THIS button specifically, immediately after
    the main refresh (re-submitting the same form — our own `form` dict is
    never actually touched by the server's reset, only its own bean state
    is, so nothing needs to be re-set on our side before this second call).
    """
    panel = soup.find(id="filterLayout")
    if panel:
        for btn in panel.find_all("button"):
            if btn.get_text(strip=True).lower() == "refresh" and btn.get("id"):
                return btn.get("id")
    return _SUBFILTER_REFRESH_ID_FALLBACK


def match_option(options: dict[str, str], query: str) -> tuple[str, str] | None:
    """
    Case-insensitive partial match against display labels.
    options: {value: label}
    Returns (value, label) or None.
    """
    q = query.lower().strip()
    for val, label in options.items():
        if label.lower() == q:
            return val, label
    for val, label in options.items():
        if q in label.lower():
            return val, label
    return None


# ── AJAX POST ─────────────────────────────────────────────────────────────────


def ajax_post(
    client: httpx.Client,
    form: dict,
    source: str,
    event: str | None = None,
    execute: str | None = None,
    render: str = "@all",
) -> tuple[str, str]:
    """
    Send a PrimeFaces AJAX partial POST.
    Returns (response_text, updated_viewstate).
    """
    data = {
        "javax.faces.partial.ajax": "true",
        "javax.faces.source": source,
        "javax.faces.partial.execute": execute or source,
        "javax.faces.partial.render": render,
        **form,
    }
    if event:
        data["javax.faces.behavior.event"] = event
        data["javax.faces.partial.event"] = event
    else:
        data[source] = source  # button click: include the source key itself

    resp = client.post(VAHAN_URL, headers=_AJAX_HEADERS, data=data)
    resp.raise_for_status()
    new_vs = extract_viewstate_xml(resp.text)
    return resp.text, new_vs if new_vs else form["javax.faces.ViewState"]


# ── Table parsing ─────────────────────────────────────────────────────────────


def parse_table(resp_text: str) -> tuple[list[str], list[list[str]]]:
    """
    Extract headers and data rows from CDATA sections in a PrimeFaces AJAX response.

    Handles the Vahan-specific header/data column mismatch: the <th> row
    sometimes includes a phantom axis-label column at position 2 that the
    <td> rows omit. When detected, the phantom column is dropped and TOTAL
    is moved to the end to match the data layout.
    """
    raw_headers: list[str] = []
    all_rows: list[list[str]] = []

    for cd in re.findall(r"<!\[CDATA\[(.*?)\]\]>", resp_text, re.DOTALL):
        if "<th" not in cd.lower() and "<td" not in cd.lower():
            continue
        soup = BeautifulSoup(cd, "html.parser")

        ths = soup.find_all("th")
        if ths and len(ths) > 5:
            raw_headers = [th.get_text(strip=True) for th in ths]

        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if tds and len(tds) > 2:
                all_rows.append([td.get_text(strip=True) for td in tds])

    headers = raw_headers
    if (
        all_rows
        and raw_headers
        and len(raw_headers) == len(all_rows[0]) + 1
        and len(raw_headers) > 4
        and raw_headers[3] == "TOTAL"
    ):
        # Drop the phantom axis-label at index 2, move TOTAL to end
        headers = raw_headers[:2] + raw_headers[4:] + ["TOTAL"]

    return headers, all_rows


def paginate_table(
    client: httpx.Client, form: dict, initial_resp: str
) -> tuple[list[str], list[list[str]]]:
    """Collect all paginated rows from groupingTable. Returns (headers, all_rows)."""
    headers, all_rows = parse_table(initial_resp)

    if "ui-paginator" not in initial_resp:
        return headers, all_rows

    page = 1
    while page < 100:  # safety cap
        first = page * ROWS_PER_PAGE
        page += 1

        page_data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "groupingTable",
            "javax.faces.partial.execute": "groupingTable",
            "javax.faces.partial.render": "groupingTable",
            "javax.faces.behavior.event": "page",
            "javax.faces.partial.event": "page",
            "groupingTable_pagination": "true",
            "groupingTable_first": str(first),
            "groupingTable_rows": str(ROWS_PER_PAGE),
            "groupingTable_encodeFeature": "true",
            **form,
        }

        try:
            resp = client.post(VAHAN_URL, headers=_AJAX_HEADERS, data=page_data)
            resp.raise_for_status()
            new_vs = extract_viewstate_xml(resp.text)
            if new_vs:
                form["javax.faces.ViewState"] = new_vs

            _, new_rows = parse_table(resp.text)
            if not new_rows:
                break
            all_rows.extend(new_rows)
            print(f"      Page {page}: +{len(new_rows)} rows (total {len(all_rows)})")
            if len(new_rows) < ROWS_PER_PAGE:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"      Pagination error at page {page}: {e}")
            break

    return headers, all_rows


def strip_total_column(headers: list[str], rows: list[list[str]]):
    """Drop a trailing TOTAL column immediately after fetch, before any
    melting or buffering happens. It's dropped entirely (not carried through
    melted rows) — recomputable downstream via SUM(Value) grouped by
    category/year if ever needed, but not stored."""
    if headers and headers[-1].strip().upper() == "TOTAL":
        return headers[:-1], [r[:-1] for r in rows]
    return headers, rows


def melt_wide_row(headers: list[str], row: list[str]):
    """
    Transpose one wide-format row into long/tidy form. headers[0] is the
    Y-axis category column (e.g. 'FUEL'); everything else (e.g. month names
    for Month Wise) gets melted — one output row per melted column instead
    of one column per month. Call strip_total_column() first so TOTAL isn't
    present here at all.

    Returns (category_value, [(melt_key, melt_value), ...]).
    """
    if not headers or not row:
        return None, []
    return row[0], list(zip(headers[1:], row[1:]))


def is_zero_value(value: str) -> bool:
    """
    True only if `value` unambiguously parses as numeric zero. Handles
    comma-formatted numbers (e.g. '1,204'). Anything that fails to parse
    is treated as NOT zero (conservative — never silently drop a row just
    because its value looked unusual; only drop confirmed zeros).
    """
    try:
        return float(value.strip().replace(",", "")) == 0
    except (ValueError, AttributeError):
        return False


# ── Core scrape logic ─────────────────────────────────────────────────────────


def scrape_one_state(
    client,
    form,
    out_path,
    completed_combos,
    expected_header,
    years,
    yaxis_val,
    yaxis_label,
    xaxis_val,
    xaxis_label,
    refresh_ids,
    state_input_name,
    state_code,
    state_label,
    rto_mode,
    rto_queries,
    debug_dir=None,
    write_lock=None,
    vhclass_loop=None,
    subfilter_refresh_id=None,
) -> bool:
    """
    Run the RTO x Year download loop for one state, appending every result
    into the single shared CSV at `out_path`.

    state_code == "" / state_label == "all_states" means the all-states aggregate
    (no state selection step, RTO looping not applicable).
    rto_mode: "all" (every RTO), "specific" (rto_queries list), or "aggregate" (state total only).
    Returns True on success, False if the state selection itself failed.

    `completed_combos` is a set of (state, rto, vehicle_class_tag, year) tuples
    already present in out_path (loaded once at startup) — used to resume
    without re-fetching rows that are already saved. vehicle_class_tag is ""
    when vhclass_loop is None (matches every prior run's schema exactly).
    `expected_header` is a single-element list holding either None (nothing
    written yet this run) or the exact header row (as a list) that every
    subsequent write must match — this is what catches a header mismatch
    (e.g. re-running with different --yaxis/--xaxis, or with/without
    --vehicle-class ALL, into the same output file) instead of silently
    appending misaligned columns. (List because Python closures can't rebind
    an outer variable.)

    `vhclass_loop`: None for normal behavior (whatever VhClass filter, if
    any, is already baked into `form` applies as a single combined pass —
    no extra column). A list of (value, label) tuples means "loop each of
    these individually and tag every row with which class it came from" —
    this is what --vehicle-class ALL triggers, and adds a "Vehicle Class"
    column to the output.

    `subfilter_refresh_id`: the vehicle sub-filter panel's OWN Refresh
    button id (distinct from `refresh_ids`, the main refresh). Whenever any
    of VhCatg/fuel/VhClass are set in `form`, this gets triggered as a
    second, separate request right after the main refresh — confirmed via
    live testing that the main refresh clears those filters server-side, so
    the two must never be conflated into a single "try until one works" loop.

    IMPORTANT: `form` is mutated in place and its ViewState carried forward —
    this mirrors a real browser session clicking through states one after
    another. Do NOT reset it to a stale ViewState between states; the server
    tracks view state progression per-session.
    """
    state_ajax_resp = ""

    if state_code:
        if state_input_name:
            form[state_input_name] = state_code
        state_source_id = (
            state_input_name.replace("_input", "") if state_input_name else "j_idt34"
        )
        try:
            state_ajax_resp, vs = ajax_post(
                client,
                form,
                source=state_source_id,
                event="change",
                render="selectedRto yaxisVar",
            )
        except Exception as e:
            print(f"  [ERROR] Could not select state '{state_label}': {e}")
            return False
        form["javax.faces.ViewState"] = vs

        # State change can add extra Y-axis options (e.g. "Rto")
        new_yaxis = parse_options_from_xml(state_ajax_resp, "yaxisVar_input")
        if new_yaxis:
            refreshed = match_option(new_yaxis, yaxis_label)
            if refreshed:
                form["yaxisVar_input"] = refreshed[0]
        time.sleep(0.4)

    # ── Set Y-axis / X-axis ──────────────────────────────────────────────
    form["yaxisVar_input"] = yaxis_val
    _, vs = ajax_post(client, form, source="yaxisVar", event="change")
    form["javax.faces.ViewState"] = vs
    time.sleep(0.3)

    form["xaxisVar_input"] = xaxis_val
    _, vs = ajax_post(client, form, source="xaxisVar", event="change")
    form["javax.faces.ViewState"] = vs
    time.sleep(0.3)

    # ── Determine RTOs to iterate ────────────────────────────────────────
    rto_list: list[tuple[str, str]] = [("-1", "all_rtos")]

    if state_code and rto_mode in ("all", "specific"):
        rto_options = parse_options_from_xml(state_ajax_resp, "selectedRto_input")
        rto_options = {
            v: l
            for v, l in rto_options.items()
            if "All Vahan4" not in l
            and v not in ("-1", "", "0")
            and not is_placeholder_rto(l)
        }
        if rto_mode == "all":
            rto_list = list(rto_options.items())
            print(f"  Found {len(rto_list)} RTOs for {state_label}")
            if not rto_list:
                print(
                    f"  [WARN] No RTOs found for '{state_label}' — nothing to scrape at RTO level."
                )
                return True
        else:  # specific
            rto_list = []
            for query in rto_queries:
                m = match_option(rto_options, query)
                if m:
                    rto_list.append(m)
                    print(f"  RTO matched: {m[1]}  (code={m[0]})")
                else:
                    print(
                        f"  [WARN] RTO '{query}' not found in {state_label} — skipping"
                    )

    # ── Main loop: RTOs x Vehicle Classes x Years ──────────────────────────
    clean_state = (
        clean_state_name(state_label) if state_label != "all_states" else "all_states"
    )

    # Single pass with no class tag when not looping (vh_tag="" matches every
    # prior run's combo-key shape exactly, so old output files still resume fine).
    class_iterations = vhclass_loop if vhclass_loop else [(None, "")]

    for rto_code, rto_label in rto_list:
        form["selectedRto_input"] = rto_code
        if rto_code != "-1":
            print(f"\nRTO: {rto_label}  (code={rto_code})")

        # Buffer all years (x vehicle classes, if looping) for this RTO in
        # memory, write once at the end — cuts file-open + lock-acquisition
        # from once-per-fetch to once-per-RTO. Trade-off: if the process is
        # killed mid-RTO, everything fetched so far in this RTO is lost and
        # will be re-fetched on resume (previously each year was durable the
        # instant it was fetched). Given fetches-per-RTO is small (tens to a
        # few hundred with --vehicle-class ALL, not thousands), this is a
        # good trade for the I/O savings.
        #
        # Each entry stores the melted (long-format) rows for that
        # (vehicle_class, year), plus this fetch's category column name so a
        # genuine schema mismatch can still be detected — but a partial year
        # having fewer months than a completed one is NO LONGER a shape
        # difference once melted (it just produces fewer rows), so that
        # whole reconciliation problem goes away naturally.
        rto_fetches: list[tuple[int, str, str, str, list[list[str]]]] = (
            []
        )  # (year, fetched_at, category_col, vh_tag, melted_rows)
        rto_combo_keys: list[tuple[str, str, str, int]] = (
            []
        )  # (state, rto, vh_tag, year)

        for year in years:
            for vh_value, vh_label in class_iterations:
                if vh_value is not None:
                    form["VhClass"] = [vh_value]
                    print(f"\n  Vehicle Class: {vh_label}  (value={vh_value})")
                vh_tag = vh_label or ""
                combo_key = (clean_state, rto_label, vh_tag, year)
                if combo_key in completed_combos:
                    where = f"{clean_state} / {rto_label}" + (
                        f" / {vh_tag}" if vh_tag else ""
                    )
                    print(f"  [{year}] Skip (already in output file): {where}")
                    continue

                form["selectedYear_input"] = str(year)
                _, vs = ajax_post(client, form, source="selectedYear", event="change")
                form["javax.faces.ViewState"] = vs
                time.sleep(0.3)

                resp_text = None
                for refresh_id in refresh_ids:
                    try:
                        rt, vs = ajax_post(
                            client, form, source=refresh_id, execute="@all"
                        )
                        form["javax.faces.ViewState"] = vs
                        if "<th" in rt or "<td" in rt:
                            resp_text = rt
                            break
                    except Exception:
                        continue

                # Any of VhCatg/fuel/VhClass set? The main refresh above just
                # cleared them server-side (confirmed via live testing) — our
                # own `form` dict was never touched though, so re-submitting
                # it to the sub-panel's OWN refresh button re-applies them.
                # THIS response (not the main refresh's) is the actually-
                # filtered data.
                has_subfilters = any(form.get(k) for k in ("VhCatg", "fuel", "VhClass"))
                if has_subfilters and subfilter_refresh_id:
                    try:
                        rt2, vs = ajax_post(
                            client, form, source=subfilter_refresh_id, execute="@all"
                        )
                        form["javax.faces.ViewState"] = vs
                        if "<th" in rt2 or "<td" in rt2:
                            resp_text = rt2
                        else:
                            print(
                                f"  [WARN] Sub-filter refresh returned no table — "
                                f"falling back to unfiltered main-refresh data for this fetch"
                            )
                    except Exception as e:
                        print(
                            f"  [WARN] Sub-filter refresh failed ({e}) — "
                            f"falling back to unfiltered main-refresh data for this fetch"
                        )

                if not resp_text:
                    print(f"  [{year}] Refresh failed — no table in response")
                    continue

                headers, rows = parse_table(resp_text)
                if not headers:
                    # Common case: the state/RTO simply has no data for this year
                    # (e.g. requesting years before a state existed — Telangana
                    # was only carved out of Andhra Pradesh in 2014, so 2003-2013
                    # will always come back empty for it), or this vehicle class
                    # genuinely has zero registrations for this RTO/year.
                    # Distinguish that from a genuine parse failure so the log
                    # isn't misleadingly scary.
                    lower = resp_text.lower()
                    if any(
                        p in lower for p in ("no record", "no data", "not available")
                    ):
                        print(
                            f"  [{year}] No data available (normal for years before this "
                            f"state/RTO existed, or zero registrations for this filter)"
                        )
                    else:
                        print(
                            f"  [{year}] Could not parse table headers — unexpected response, worth checking manually"
                        )
                    if debug_dir:
                        os.makedirs(debug_dir, exist_ok=True)
                        dump_path = os.path.join(
                            debug_dir,
                            f"{safe_name(clean_state)}_{safe_name(rto_label)}_{safe_name(vh_tag)}_{year}.txt",
                        )
                        with open(dump_path, "w", encoding="utf-8") as f:
                            f.write(resp_text)
                        print(f"    → raw response saved to {dump_path}")
                    continue

                print(f"  [{year}] {len(rows)} rows, {len(headers)} columns")

                if "ui-paginator" in resp_text:
                    headers, rows = paginate_table(client, form, resp_text)

                if not rows:
                    print(f"  [{year}] No data rows found")
                    continue

                headers, rows = strip_serial_column(headers, rows)
                headers, rows = strip_total_column(headers, rows)

                category_col = headers[0]
                melted_rows: list[list[str]] = []
                zero_skipped = 0
                for row in rows:
                    _, pairs = melt_wide_row(headers, row)
                    for melt_key, melt_value in pairs:
                        if is_zero_value(melt_value):
                            zero_skipped += 1
                            continue
                        melted_rows.append([row[0], melt_key, melt_value])

                fetched_at = now_iso()
                rto_fetches.append(
                    (year, fetched_at, category_col, vh_tag, melted_rows)
                )
                rto_combo_keys.append(combo_key)
                print(
                    f"  [{year}] Fetched {len(rows)} rows → {len(melted_rows)} melted rows "
                    f"({zero_skipped} zero-value rows dropped, buffered, not yet written)"
                )
                time.sleep(0.5)

        # ── One write for the whole RTO ──────────────────────────────────────
        # Post-melt (and post-TOTAL-removal), the schema is fixed:
        # [category_col, <xaxis_label>, Value] (plus Vehicle Class if looping)
        # regardless of how many months a given year had — so this only needs
        # to check the category column name matches, not do per-column
        # reconciliation.
        if rto_fetches:
            lock_ctx = write_lock if write_lock is not None else _NULL_LOCK
            with lock_ctx:
                has_class_column = bool(vhclass_loop)

                if expected_header[0] is None:
                    # First write of this run (file was empty/new) — this
                    # becomes the standard every later write (including from
                    # other concurrent workers) is checked against.
                    _, _, category_col, _, _ = rto_fetches[0]
                    cols = ["State", "RTO"]
                    if has_class_column:
                        cols.append("Vehicle Class")
                    cols += ["Year", "FetchedAt", category_col, xaxis_label, "Value"]
                    expected_header[0] = cols
                    file_is_new = True
                else:
                    file_is_new = False

                header_has_class_column = "Vehicle_Class" in expected_header[0]
                if header_has_class_column != has_class_column:
                    print(
                        f"  [ERROR] {rto_label}: this run's --vehicle-class ALL setting doesn't "
                        f"match the existing output file's schema (one has a 'Vehicle Class' "
                        f"column, the other doesn't). Refusing to write to avoid corrupting "
                        f"{out_path}. Use a different --out-file."
                    )
                else:
                    canonical_category_col = expected_header[0][
                        -3
                    ]  # 3rd-from-last is always the category column

                    written_rows = []
                    skipped = []
                    for (
                        yr,
                        fetched_at,
                        category_col,
                        vh_tag,
                        melted_rows,
                    ) in rto_fetches:
                        if category_col != canonical_category_col:
                            skipped.append((vh_tag, yr))
                            continue
                        for melted_row in melted_rows:
                            written_rows.append(
                                (
                                    clean_state,
                                    rto_label,
                                    vh_tag,
                                    yr,
                                    fetched_at,
                                    melted_row,
                                )
                            )

                    if written_rows:
                        with open(out_path, "a", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            if file_is_new:
                                writer.writerow(expected_header[0])
                            for (
                                st,
                                rt_label,
                                vh_tag,
                                yr,
                                fetched_at,
                                melted_row,
                            ) in written_rows:
                                row = [st, rt_label]
                                if has_class_column:
                                    row.append(vh_tag)
                                row += [yr, fetched_at]
                                row += melted_row
                                writer.writerow(row)
                        written_tags = {
                            (vh_tag, yr) for _, _, vh_tag, yr, _, _ in written_rows
                        }
                        completed_combos.update(
                            k for k in rto_combo_keys if (k[2], k[3]) in written_tags
                        )
                        print(
                            f"  Saved {len(written_rows)} rows for {rto_label} "
                            f"({len(written_tags)} (class, year) combo(s)) → {out_path}"
                        )

                    if skipped:
                        # Genuinely different category column (e.g. a different
                        # --yaxis than whatever originally created this file).
                        # Not added to completed_combos, so these retry cleanly
                        # once the mismatch is resolved.
                        print(
                            f"  [ERROR] Shape mismatch for {rto_label}, (class, year) {skipped} — "
                            f"refused to avoid corrupting {out_path}."
                        )
                        print(
                            f"    Canonical category column : {canonical_category_col!r}"
                        )
                        print(
                            f"    Likely cause: --yaxis/--xaxis differs from whatever originally "
                            f"created this output file, or an older script version wrote it with "
                            f"a different column layout. Use a different --out-file, or start fresh."
                        )

    return True


def run_worker(
    worker_id,
    state_queue,
    out_path,
    completed_combos,
    expected_header,
    write_lock,
    progress_file,
    progress_lock,
    years,
    yaxis_val,
    yaxis_label,
    xaxis_val,
    xaxis_label,
    rto_mode,
    rto_queries,
    debug_dir,
    subfilter_values,
    vhclass_loop,
):
    """
    One concurrent worker: opens its OWN httpx.Client (own cookies/session,
    own JSF ViewState — sessions can't be shared across threads since the
    server tracks view state per-session), then pulls states off the shared
    queue one at a time until it's empty. Coordinates with other workers
    only through write_lock (shared output file) and progress_lock (shared
    resume checkpoint file) — otherwise runs fully independently.

    subfilter_values: {form_field_name: [values]} for the left-panel
    Vehicle Category / Fuel / Vehicle Class filters (e.g.
    {"VhClass": ["73", "74"]}) — resolved once up front in scrape() and
    applied identically in every worker's own form dict.
    vhclass_loop: same as scrape_one_state's — None, or a list of
    (value, label) tuples to loop individually with --vehicle-class ALL.
    """
    try:
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            verify=False,
            headers={"User-Agent": _UA, "Accept": "text/html"},
        ) as client:
            resp = client.get(VAHAN_URL)
            resp.raise_for_status()
            vs = extract_viewstate(resp.text)
            soup = BeautifulSoup(resp.text, "html.parser")

            state_input_name = find_state_input_name(soup)
            display_input_name = find_display_input_name(soup, state_input_name)
            refresh_ids = find_refresh_ids(soup)
            subfilter_refresh_id = find_subfilter_refresh_id(soup)

            form: dict = {
                FORM_ID: FORM_ID,
                "yaxisVar_input": yaxis_val,
                "xaxisVar_input": xaxis_val,
                "selectedRto_input": "-1",
                "selectedYearType_input": "C",
                "selectedYear_input": str(years[0]),
                "javax.faces.ViewState": vs,
            }
            if display_input_name:
                form[display_input_name] = "A"
            if state_input_name:
                form[state_input_name] = "-1"
            for field_name, values in subfilter_values.items():
                form[field_name] = values

            while True:
                try:
                    code, label = state_queue.get_nowait()
                except Empty:
                    break
                print(f"[worker {worker_id}] === STATE: {label} (code={code}) ===")
                try:
                    ok = scrape_one_state(
                        client,
                        form,
                        out_path,
                        completed_combos,
                        expected_header,
                        years,
                        yaxis_val,
                        yaxis_label,
                        xaxis_val,
                        xaxis_label,
                        refresh_ids,
                        state_input_name,
                        code,
                        label,
                        rto_mode,
                        rto_queries,
                        debug_dir=debug_dir,
                        write_lock=write_lock,
                        vhclass_loop=vhclass_loop,
                        subfilter_refresh_id=subfilter_refresh_id,
                    )
                    if ok:
                        with progress_lock:
                            with open(progress_file, "a") as f:
                                f.write(label + "\n")
                    else:
                        print(
                            f"[worker {worker_id}] [WARN] State '{label}' did not complete cleanly — will retry on next run."
                        )
                except Exception as e:
                    print(
                        f"[worker {worker_id}] [ERROR] State '{label}' raised an exception: {e} — continuing."
                    )
                    continue
    except Exception as e:
        print(f"[worker {worker_id}] [FATAL] Worker session crashed: {e}")


def scrape(args):
    current_year = datetime.now().year

    if args.start_year is not None:
        years = list(
            range(int(args.start_year), int(args.end_year or current_year) + 1)
        )
    elif args.year is not None:
        years = [int(args.year)]
    else:
        years = [current_year]

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, args.out_file)
    debug_dir = os.path.join(out_dir, "_debug_responses") if args.debug else None

    # ── Resume support: load (State, RTO, Vehicle Class, Year) combos ───────
    # already in the file. This MUST match combo_key's shape exactly
    # (clean_state, rto_label, vh_tag, year) below, or the membership check
    # silently never matches (a 4-tuple never equals a 3-tuple) and every
    # run re-fetches + re-appends everything, duplicating existing rows.
    completed_combos: set[tuple[str, str, str, int]] = set()
    expected_header: list = [
        None
    ]  # None until first header is known; then the exact list to validate against
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        with open(out_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            existing_header = next(reader, None)
            if existing_header:
                expected_header[0] = existing_header
                # "Vehicle Class" column is only present when this file was
                # written with --vehicle-class ALL (has_class_column). When
                # present it sits right after RTO, shifting Year to index 3;
                # otherwise Year is at index 2 and vh_tag is always "".
                file_has_class_col = "Vehicle Class" in existing_header
                year_idx = 3 if file_has_class_col else 2
                for row in reader:
                    if len(row) > year_idx:
                        try:
                            vh_tag_val = row[2] if file_has_class_col else ""
                            completed_combos.add(
                                (row[0], row[1], vh_tag_val, int(row[year_idx]))
                            )
                        except ValueError:
                            continue
        if completed_combos:
            print(
                f"Found existing output file with {len(completed_combos)} "
                f"(State, RTO, Year) combos already saved — will resume."
            )
            print(f"  Existing header: {expected_header[0]}")
            print(
                f"  New fetches must match this header exactly (same --yaxis/--xaxis) "
                f"or they'll be refused rather than silently misaligning columns."
            )

    with httpx.Client(
        timeout=60.0,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": _UA, "Accept": "text/html"},
    ) as client:

        # ── Step 1: Fetch page, extract ViewState and all option maps ──────
        print("Fetching Vahan dashboard…")
        resp = client.get(VAHAN_URL)
        resp.raise_for_status()
        vs = extract_viewstate(resp.text)
        soup = BeautifulSoup(resp.text, "html.parser")

        state_input_name = find_state_input_name(soup)
        display_input_name = find_display_input_name(soup, state_input_name)
        refresh_ids = find_refresh_ids(soup)

        # Exclude the "-1" aggregate entry so only named states are searchable/listable
        states_map = (
            {
                k: v
                for k, v in parse_options(soup, state_input_name).items()
                if k != "-1"
            }
            if state_input_name
            else {}
        )
        yaxis_map = parse_options(soup, "yaxisVar_input")
        xaxis_map = parse_options(soup, "xaxisVar_input")
        years_map = {
            k: v
            for k, v in parse_options(soup, "selectedYear_input").items()
            if k not in ("", "A")
        }

        # ── Validate requested years against what the site actually offers ──
        # (--start-year/--end-year are user-supplied and not checked against
        # the live dropdown until now — requesting a year the site doesn't
        # support would otherwise silently waste requests / return no data)
        if not args.list_options and years_map:
            valid_years = {int(y) for y in years_map if y.isdigit()}
            if valid_years:
                requested = set(years)
                out_of_range = sorted(requested - valid_years)
                in_range = sorted(requested & valid_years)
                if out_of_range:
                    print(
                        f"[WARN] Site's Year dropdown only offers {min(valid_years)}–{max(valid_years)}. "
                        f"Dropping unsupported year(s): {out_of_range}"
                    )
                if not in_range:
                    print(
                        f"[ERROR] None of the requested years are available. "
                        f"Site offers {min(valid_years)}–{max(valid_years)}."
                    )
                    sys.exit(1)
                years = in_range

        # ── List-options mode ──────────────────────────────────────────────
        if args.list_options:
            print("\n=== Vahan Dashboard Dropdown Options ===")
            print(f"\nStates ({len(states_map)}):")
            for code, label in states_map.items():
                print(f"  [{code}]  {label}")
            print("\nY-Axis Options:")
            for val, label in yaxis_map.items():
                print(f"  [{val}]  {label}")
            print("\nX-Axis Options:")
            for val, label in xaxis_map.items():
                print(f"  [{val}]  {label}")
            print("\nYear Options:")
            for val, label in years_map.items():
                print(f"  [{val}]  {label}")
            return

        # ── List-vehicle-classes mode ────────────────────────────────────────
        if args.list_vehicle_classes:
            print("\n=== Vehicle Sub-Filter Options (left panel) ===")
            for field_name, display_name in [
                ("VhCatg", "Vehicle Category"),
                ("fuel", "Fuel"),
                ("VhClass", "Vehicle Class"),
            ]:
                options = parse_checkbox_options(soup, field_name)
                print(f"\n{display_name} ({len(options)}):")
                for val, label in options.items():
                    print(f"  [{val}]  {label}")
            return

        # ── Resolve Y-axis / X-axis display names to form values ──────────
        yaxis_match = match_option(yaxis_map, args.yaxis)
        if not yaxis_match:
            print(f"[ERROR] Y-Axis '{args.yaxis}' not found. Run --list-options.")
            sys.exit(1)
        yaxis_val, yaxis_label = yaxis_match

        xaxis_match = match_option(xaxis_map, args.xaxis)
        if not xaxis_match:
            print(f"[ERROR] X-Axis '{args.xaxis}' not found. Run --list-options.")
            sys.exit(1)
        xaxis_val, xaxis_label = xaxis_match

        print(f"Y-Axis: {yaxis_label}  (form value: {yaxis_val})")
        print(f"X-Axis: {xaxis_label}  (form value: {xaxis_val})")

        # ── Resolve left-panel sub-filters: Vehicle Category / Fuel / Vehicle Class ──
        # These live in a separate multi-checkbox filter panel from the main
        # Y/X/State/RTO/Year controls. Confirmed via live testing: clicking
        # the MAIN refresh button resets these server-side, so applying them
        # requires a second, deliberate request to the sub-panel's OWN
        # refresh button (subfilter_refresh_id) after every main refresh —
        # handled in scrape_one_state(). Discovered once here, reused for
        # every state/RTO/year fetch (and passed to each concurrent worker).
        subfilter_refresh_id = find_subfilter_refresh_id(soup)
        print(f"Sub-filter panel refresh button: {subfilter_refresh_id!r}")

        subfilter_values: dict[str, list[str]] = {}
        vhclass_loop: list[tuple[str, str]] | None = None

        # --vehicle-class ALL is a distinct mode from --vehicle-class <label>...:
        # it loops every class individually (tagging each row with which class
        # it came from) rather than combining them into one filtered total.
        if (
            args.vehicle_class
            and len(args.vehicle_class) == 1
            and args.vehicle_class[0].strip().upper() == "ALL"
        ):
            vhclass_options = parse_checkbox_options(soup, "VhClass")
            if not vhclass_options:
                print(
                    "[ERROR] Could not find Vehicle Class filter options on the page."
                )
                sys.exit(1)
            vhclass_loop = list(vhclass_options.items())
            print(
                f"\n[NOTE] --vehicle-class ALL: looping {len(vhclass_loop)} vehicle classes "
                f"individually. This multiplies your total request count by "
                f"{len(vhclass_loop)}x on top of State x RTO x Year — for a large run this "
                f"can mean a very long time and a lot of load on the server. If you only "
                f"need a subset (e.g. truck/bus/van-style categories), pass their exact "
                f"labels to --vehicle-class instead — see --list-vehicle-classes for names.\n"
            )

        subfilter_specs = [
            ("VhCatg", args.vehicle_category, "Vehicle Category"),
            ("fuel", args.fuel, "Fuel"),
        ]
        if vhclass_loop is None:
            subfilter_specs.append(("VhClass", args.vehicle_class, "Vehicle Class"))

        for field_name, queries, display_name in subfilter_specs:
            if not queries:
                continue
            options = parse_checkbox_options(soup, field_name)
            if not options:
                print(
                    f"[WARN] Could not find '{display_name}' filter options on the page — skipping."
                )
                continue
            print(f"Resolving {display_name} filter ({len(queries)} requested):")
            matched = match_multiple(options, queries)
            if matched:
                subfilter_values[field_name] = [v for v, _ in matched]

        # ── Build base form (tracks full server-side form state) ───────────
        form: dict = {
            FORM_ID: FORM_ID,
            "yaxisVar_input": yaxis_val,
            "xaxisVar_input": xaxis_val,
            "selectedRto_input": "-1",  # All RTOs (overridden per-loop below)
            "selectedYearType_input": "C",  # Calendar Year
            "selectedYear_input": str(years[0]),
            "javax.faces.ViewState": vs,
        }
        if display_input_name:
            form[display_input_name] = "A"  # Actual values, not thousands/lakhs
        if state_input_name:
            form[state_input_name] = "-1"  # Default: All States aggregate
        for field_name, values in subfilter_values.items():
            form[field_name] = values

        # ── RTO mode: default to RTO-level whenever a state is in play ─────
        # (per your requirement — data at RTO level, not the state aggregate)
        if args.rto:
            rto_mode, rto_queries = "specific", args.rto
        elif args.aggregate_only:
            rto_mode, rto_queries = "aggregate", None
        elif args.state:
            rto_mode, rto_queries = "all", None
        else:
            rto_mode, rto_queries = "aggregate", None

        # ── ALL-states mode: loop every state automatically ─────────────────
        if args.state and args.state.strip().upper() == "ALL":
            print(
                f"Looping all {len(states_map)} states/UTs at RTO level "
                f"(concurrency={args.concurrency})…"
            )

            progress_file = os.path.join(out_dir, "_completed_states.txt")
            done = set()
            if os.path.exists(progress_file):
                done = {line.strip() for line in open(progress_file) if line.strip()}
                if done:
                    print(
                        f"  {len(done)} states already marked complete — will skip them."
                    )

            state_items = [(c, l) for c, l in states_map.items() if l not in done]

            if args.concurrency <= 1:
                # Sequential: reuse the client/form already set up above.
                for i, (code, label) in enumerate(state_items, 1):
                    print(
                        f"\n[{i}/{len(state_items)}] === STATE: {label} (code={code}) ==="
                    )
                    try:
                        ok = scrape_one_state(
                            client,
                            form,
                            out_path,
                            completed_combos,
                            expected_header,
                            years,
                            yaxis_val,
                            yaxis_label,
                            xaxis_val,
                            xaxis_label,
                            refresh_ids,
                            state_input_name,
                            code,
                            label,
                            rto_mode,
                            rto_queries,
                            debug_dir=debug_dir,
                            vhclass_loop=vhclass_loop,
                            subfilter_refresh_id=subfilter_refresh_id,
                        )
                        if ok:
                            with open(progress_file, "a") as f:
                                f.write(label + "\n")
                        else:
                            print(
                                f"  [WARN] State '{label}' did not complete cleanly — will retry on next run."
                            )
                    except Exception as e:
                        print(
                            f"  [ERROR] State '{label}' raised an exception: {e} — continuing to next state."
                        )
                        continue
            else:
                # Concurrent: each worker gets its own httpx.Client/session
                # and pulls states off a shared queue. Coordination is only
                # needed for the shared output file and progress checkpoint.
                state_queue: Queue = Queue()
                for item in state_items:
                    state_queue.put(item)

                write_lock = threading.Lock()
                progress_lock = threading.Lock()
                threads = []
                for worker_id in range(1, args.concurrency + 1):
                    t = threading.Thread(
                        target=run_worker,
                        args=(
                            worker_id,
                            state_queue,
                            out_path,
                            completed_combos,
                            expected_header,
                            write_lock,
                            progress_file,
                            progress_lock,
                            years,
                            yaxis_val,
                            yaxis_label,
                            xaxis_val,
                            xaxis_label,
                            rto_mode,
                            rto_queries,
                            debug_dir,
                            subfilter_values,
                            vhclass_loop,
                        ),
                        daemon=True,
                    )
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join()

            print(f"\nDone (all states). Output file: {out_path}")
            return

        # ── Single-state (or all-states-aggregate) mode ─────────────────────
        state_code, state_label = "", "all_states"
        if args.state:
            result = match_option(states_map, args.state)
            if not result:
                print(f"[ERROR] State '{args.state}' not found. Run --list-options.")
                sys.exit(1)
            state_code, state_label = result
            print(f"Selecting state: {state_label}  (code: {state_code})")

        scrape_one_state(
            client,
            form,
            out_path,
            completed_combos,
            expected_header,
            years,
            yaxis_val,
            yaxis_label,
            xaxis_val,
            xaxis_label,
            refresh_ids,
            state_input_name,
            state_code,
            state_label,
            rto_mode,
            rto_queries,
            debug_dir=debug_dir,
            vhclass_loop=vhclass_loop,
            subfilter_refresh_id=subfilter_refresh_id,
        )

    print(f"\nDone. Output file: {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="api.py",
        description="Vahan Dashboard API Scraper — faster alternative, no browser required",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output is a single CSV file (--out/--out-file, default vahan_data/vahan_output.csv).
Every row is tagged with State/RTO/Year columns, so the file is self-describing
even after filtering/pivoting. Re-running with the same output file resumes —
(State, RTO, Year) combos already present are skipped automatically.
RTO-level data is fetched by default whenever --state is given.

Examples:
  python3 scripts/api.py --list-options
  python3 scripts/api.py --yaxis "Vehicle Category" --xaxis "Fuel"
  python3 scripts/api.py --yaxis "Vehicle Category" --xaxis "Fuel" --state "Kerala" --year 2025
  python3 scripts/api.py --yaxis "Vehicle Category" --xaxis "Fuel" --state "Kerala" --start-year 2020
  python3 scripts/api.py --yaxis "Maker" --xaxis "Fuel" --state "Kerala" --rto "TRIVANDRUM" "KOLLAM"
  python3 scripts/api.py --yaxis "Fuel" --xaxis "Month Wise" --state ALL --start-year 2016 --end-year 2026
        """,
    )

    parser.add_argument(
        "--yaxis", default=None, help="Y-Axis variable (required unless --list-options)"
    )
    parser.add_argument(
        "--xaxis", default=None, help="X-Axis variable (required unless --list-options)"
    )

    year_grp = parser.add_argument_group("year selection")
    year_grp.add_argument(
        "--year", default=None, help="Single year (default: current year)"
    )
    year_grp.add_argument(
        "--start-year",
        dest="start_year",
        default=None,
        help="Start of year range (inclusive). Overrides --year.",
    )
    year_grp.add_argument(
        "--end-year",
        dest="end_year",
        default=None,
        help="End of year range (inclusive, default: current year)",
    )

    loc_grp = parser.add_argument_group("location filters")
    loc_grp.add_argument(
        "--state",
        default=None,
        help="State name, partial match (e.g. 'Kerala'). Pass 'ALL' to loop "
        "every state automatically (resumable — combos already in "
        "--out-file are skipped, plus a whole-state checkpoint).",
    )
    loc_grp.add_argument(
        "--all-rtos",
        action="store_true",
        help="(default behavior when --state is given — kept for backward "
        "compatibility, has no additional effect)",
    )
    loc_grp.add_argument(
        "--rto",
        nargs="+",
        default=None,
        help="One or more specific RTO names (requires --state). "
        "Overrides the RTO-level default to fetch only these RTOs.",
    )
    loc_grp.add_argument(
        "--aggregate-only",
        action="store_true",
        help="Fetch the state-level aggregate instead of per-RTO data "
        "(opt out of the RTO-level default).",
    )

    subfilter_grp = parser.add_argument_group(
        "vehicle sub-filters (left panel — separate from Y-Axis/X-Axis)"
    )
    subfilter_grp.add_argument(
        "--vehicle-category",
        nargs="+",
        default=None,
        help="Filter by Vehicle Category checkboxes, partial match "
        "(e.g. 'LIGHT MOTOR VEHICLE' 'HEAVY MOTOR VEHICLE'). "
        "Omit for no filter (all categories).",
    )
    subfilter_grp.add_argument(
        "--fuel",
        nargs="+",
        default=None,
        help="Filter by emission Fuel checkboxes, partial match "
        "(e.g. 'BHARAT STAGE VI' 'BHARAT STAGE IV').",
    )
    subfilter_grp.add_argument(
        "--vehicle-class",
        nargs="+",
        default=None,
        help="Filter by Vehicle Class checkboxes, partial match "
        "(e.g. 'BUS' 'SCHOOL BUS' 'OMNI BUS' for a truck/bus-style "
        "breakdown that Vehicle Category alone can't give you — "
        "run --list-vehicle-classes to see all ~76 options).",
    )
    subfilter_grp.add_argument(
        "--list-vehicle-classes",
        action="store_true",
        help="Print all Vehicle Category / Fuel / Vehicle Class "
        "checkbox options (with exact labels to match against) and exit.",
    )

    parser.add_argument(
        "--out", default="vahan_data", help="Output directory (default: vahan_data/)"
    )
    parser.add_argument(
        "--out-file",
        default="vahan_output.csv",
        help="Single output CSV filename within --out "
        "(default: vahan_output.csv). Every row is "
        "tagged with State/RTO/Year columns, and "
        "re-running with the same file resumes — "
        "combos already present are skipped.",
    )
    parser.add_argument(
        "--list-options",
        action="store_true",
        help="Print all available dropdown options and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="On any parse failure, save the raw AJAX response to "
        "--out/_debug_responses/ for inspection.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of states to process in parallel when "
        "--state ALL (default: 1 = sequential). Each "
        "worker opens its own independent session. "
        "Start conservative (3-5) — a government server "
        "has finite capacity, and pushing too hard risks "
        "throttling/blocking rather than going faster. "
        "Ignored for single-state runs.",
    )

    args = parser.parse_args()

    if not args.list_options and not args.list_vehicle_classes:
        if not args.yaxis or not args.xaxis:
            parser.error(
                "--yaxis and --xaxis are required (use --list-options to see valid values)"
            )
        if args.yaxis == args.xaxis:
            parser.error("--yaxis and --xaxis must be different")
        if args.rto and not args.state:
            parser.error("--rto requires --state")
        if args.aggregate_only and args.rto:
            parser.error("--aggregate-only and --rto are mutually exclusive")
        if args.year and (args.start_year or args.end_year):
            parser.error("--year cannot be combined with --start-year / --end-year")

    scrape(args)


if __name__ == "__main__":
    main()