"""
Factiva exporter — saved search OR custom query → single RTF file.

Examples:
    # Saved search → one file
    python factiva_agent.py --search "AI" --out ai_news.rtf

    # Custom query, today's WSJ
    python factiva_agent.py --query "artificial intelligence" --date today --source "The Wall Street Journal" --out wsj_today.rtf

    # Custom query, last 7 days, any source
    python factiva_agent.py --query "OpenAI" --date week --out openai_week.rtf

    # Custom date range
    python factiva_agent.py --query "NVIDIA" --date 2026-06-01:2026-06-29 --out nvidia_june.rtf
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, Frame

load_dotenv()

SAVED_SEARCHES_URL = "https://global.factiva.com/mss/default.aspx?NAPC=G&frS=results"
SEARCH_BUILDER_URL = "https://global.factiva.com/sb/default.aspx?NAPC=S"


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Export Factiva articles to a single RTF file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--search", help="Name of a saved search (exact)")
    mode.add_argument("--query",  default="", help="Free-text search query (can be empty if --source is set)")

    p.add_argument("--date",
        default="week",
        help=(
            "Date filter: today | week | month | year | "
            "YYYY-MM-DD (single day) | YYYY-MM-DD:YYYY-MM-DD (range). "
            "Default: week"
        ),
    )
    p.add_argument("--source",  default="", help="Source name filter (e.g. 'The Wall Street Journal')")
    p.add_argument("--out",     default="factiva_export.rtf", help="Output RTF file")
    p.add_argument("--max-pages", type=int, default=0, help="Max pages to export (0 = all)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--dedup", action="store_true", help="Exclude duplicate articles (Duplicates=Off in Factiva)")
    return p.parse_args()


# ── auth ──────────────────────────────────────────────────────────────────────

def login(page: Page, username: str, password: str):
    print("[*] Logging in...", flush=True)
    page.goto("https://global.factiva.com/", wait_until="networkidle", timeout=60_000)
    try:
        page.click("button:has-text('Accept')", timeout=4_000)
    except PWTimeout:
        pass
    page.wait_for_selector("#email", timeout=20_000)
    page.fill("#email", username)
    page.click("button[type='submit']")
    page.wait_for_selector("#password-form-item", timeout=12_000)
    page.fill("#password-form-item", password)
    page.click("button[type='submit']")
    page.wait_for_function(
        "() => !window.location.hostname.includes('accounts.dowjones.com')",
        timeout=45_000,
    )
    page.wait_for_load_state("networkidle", timeout=30_000)
    print(f"[+] Logged in", flush=True)


# ── search modes ──────────────────────────────────────────────────────────────

def open_saved_search(page: Page, search_name: str):
    print(f"[*] Opening saved search: {search_name!r}", flush=True)
    page.goto(SAVED_SEARCHES_URL, wait_until="networkidle", timeout=30_000)
    time.sleep(2)

    clicked = page.evaluate(f"""() => {{
        const links = Array.from(document.querySelectorAll("a.act-run"));
        const target = links.find(l => l.textContent.trim() === {repr(search_name)})
            || links.find(l => l.textContent.trim().toLowerCase().includes({repr(search_name.lower())}));
        if (target) {{ target.click(); return target.textContent.trim(); }}
        return null;
    }}""")

    if not clicked:
        available = page.evaluate(
            "() => Array.from(document.querySelectorAll('a.act-run')).map(l => l.textContent.trim())"
        )
        raise RuntimeError(f"Saved search {search_name!r} not found. Available: {available}")

    print(f"[+] Clicked: {clicked!r}", flush=True)
    time.sleep(6)
    page.wait_for_load_state("networkidle", timeout=20_000)
    time.sleep(2)
    _wait_for_results_frame(page)
    print("[+] Results ready", flush=True)


def run_custom_search(page: Page, query: str, date_arg: str, source: str, dedup: bool = False):
    print(f"[*] Custom search: query={query!r}  date={date_arg!r}  source={source or 'all'!r}  dedup={dedup}", flush=True)
    # Pass dedup=on via URL parameter if supported
    url = SEARCH_BUILDER_URL + ("&dup=off" if dedup else "")
    page.goto(url, wait_until="networkidle", timeout=30_000)
    time.sleep(2)

    # Use only the user query (no source embedded) — source filter applied after search
    actual_query = query.strip() if query.strip() not in ("", " ") else "a"
    print(f"[*] Query: {actual_query!r}  source: {source!r}", flush=True)

    # Set query via JS to avoid auto-quote-completion
    set_ok = page.evaluate(f"""() => {{
        const cmEl = document.querySelector('.CodeMirror');
        if (cmEl && cmEl.CodeMirror) {{
            cmEl.CodeMirror.setValue({repr(actual_query)});
            return 'cm5';
        }}
        if (window.ace) {{
            const ed = ace.edit(document.querySelector('.ace_editor'));
            ed.setValue({repr(actual_query)}, 1);
            return 'ace';
        }}
        return 'none';
    }}""")
    print(f"[*] Query input: {set_ok}", flush=True)

    # Set date
    _set_date(page, date_arg)

    # Exclude duplicates
    if dedup:
        _set_dedup(page)

    # Click Search
    page.click("input[value='Search'], #btnSearchBottom, button:has-text('Search')",
                timeout=8_000)
    try:
        page.wait_for_url(lambda u: "sb/default.aspx" not in u, timeout=30_000)
    except Exception:
        pass
    page.wait_for_load_state("networkidle", timeout=30_000)
    time.sleep(4)

    print(f"[*] Page after search: {page.url}", flush=True)

    if "sb/default.aspx" in page.url:
        raise RuntimeError("Search did not navigate — query may be empty.")

    no_results = page.evaluate("""() => {
        const body = document.body.innerText.toLowerCase();
        return body.includes('no results') || body.includes('0 results');
    }""")
    if no_results:
        raise RuntimeError("No results found. Check query spelling.")

    # Apply source filter from results page FILTERS panel
    if source:
        _apply_source_filter(page, source)

    _wait_for_results_frame(page, timeout=90_000)
    print("[+] Results ready", flush=True)


def _set_date(page: Page, date_arg: str):
    """Set the date filter. Maps friendly names to Factiva select option values."""
    today = date.today()

    # Factiva select#dr option values
def _apply_source_filter(page: Page, source: str):
    """Apply source filter from the results page FILTERS panel."""
    try:
        print(f"[*] Applying source filter: {source!r}", flush=True)

        # The results page has a FILTERS panel on the left (blue tab)
        # Click to expand it
        for sel in ["#filterPanel", ".filtersPanel", "a:has-text('FILTERS')",
                    "[class*='filter']", ".filterTab"]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1_500):
                    el.click()
                    time.sleep(1)
                    break
            except Exception:
                pass

        # Find source filter section and search input
        # Factiva results page has source filter as a list or search box
        src_repr = repr(source)
        result = page.evaluate(f"""() => {{
            const inputs = document.querySelectorAll('input[type="text"]');
            for (const inp of inputs) {{
                const placeholder = (inp.placeholder || '').toLowerCase();
                const parent = inp.closest('[class*="filter"], [class*="source"], section, div');
                if (placeholder.includes('source') || placeholder.includes('publication') ||
                    (parent && parent.textContent.toLowerCase().includes('source'))) {{
                    inp.focus();
                    inp.value = {src_repr};
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('keyup', {{bubbles: true}}));
                    return 'filter-input:' + inp.id;
                }}
            }}
            return null;
        }}""")
        if result:
            print(f"[*] Source filter input: {result}", flush=True)
            time.sleep(2)
            # Click first suggestion
            for sug in [".ac_results li", ".acResults li", "[class*='suggest'] li"]:
                try:
                    el = page.locator(sug).first
                    if el.is_visible(timeout=2_000):
                        el.click()
                        print(f"[+] Source filter applied", flush=True)
                        time.sleep(3)
                        page.wait_for_load_state("networkidle", timeout=15_000)
                        return
                except Exception:
                    pass
        else:
            print(f"    [!] Source filter panel not found, continuing without", flush=True)
    except Exception as e:
        print(f"    [!] Source filter skipped: {e}", flush=True)


    PRESET_MAP = {
        "today": "LastDay",
        "day":   "LastDay",
        "week":  "LastWeek",
        "month": "LastMonth",
        "3months": "Last3Months",
        "6months": "Last6Months",
        "year":  "LastYear",
        "2years": "Last2Years",
        "5years": "Last5Years",
        "all":   "_Unspecified",
    }

    if date_arg in PRESET_MAP:
        page.select_option("#dr", PRESET_MAP[date_arg])
        return

    # Custom date range: YYYY-MM-DD or YYYY-MM-DD:YYYY-MM-DD
    if ":" in date_arg:
        date_from, date_to = date_arg.split(":", 1)
    else:
        date_from = date_to = date_arg

    page.select_option("#dr", "Custom")
    time.sleep(0.5)

    # Factiva uses MM/DD/YYYY format in the date inputs
    def to_factiva_date(iso: str) -> str:
        y, m, d_ = iso.split("-")
        return f"{m}/{d_}/{y}"

    try:
        page.fill("input[name='datefrom'], #datefrom", to_factiva_date(date_from))
    except Exception:
        pass
    try:
        page.fill("input[name='dateto'], #dateto", to_factiva_date(date_to))
    except Exception:
        pass


def _set_source(page: Page, source: str):
    """Set source filter by clicking the ► expand and using Playwright fill() for autocomplete."""
    try:
        print(f"[*] Setting source: {source!r}", flush=True)

        # Click the ► expand link in the Source row (class fesTabLinkFix)
        page.evaluate("""() => {
            for (const row of document.querySelectorAll('tr')) {
                const first = row.querySelector('td');
                if (first && first.textContent.trim() === 'Source') {
                    const a = row.querySelector('a');
                    if (a) { a.click(); return; }
                }
            }
        }""")
        time.sleep(1.5)

        # Use Playwright fill() on #scTxt — this triggers AJAX autocomplete
        inp = page.locator("#scTxt")
        inp.wait_for(state="visible", timeout=5_000)
        inp.fill(source)
        print(f"[*] Source typed, waiting for autocomplete...", flush=True)
        time.sleep(3)

        # Log what appeared in DOM after typing
        ac_debug = page.evaluate("""() => {
            const lists = document.querySelectorAll('ul, ol, div[class*="ac"], div[class*="auto"]');
            for (const l of lists) {
                if (l.querySelectorAll('li').length > 0 && l.offsetParent !== null) {
                    return l.className + ' -> ' + Array.from(l.querySelectorAll('li'))
                        .slice(0,3).map(li => li.textContent.trim()).join(' | ');
                }
            }
            return 'no visible list found';
        }""")
        print(f"[*] Autocomplete DOM: {ac_debug}", flush=True)

        # Click first visible list item
        clicked_sug = page.evaluate("""() => {
            const lists = document.querySelectorAll('ul, div[class*="ac"], div[class*="auto"]');
            for (const l of lists) {
                if (l.offsetParent === null) continue;
                const li = l.querySelector('li');
                if (li) { li.click(); return li.textContent.trim().slice(0,80); }
            }
            return null;
        }""")

        if clicked_sug:
            print(f"[+] Source selected: {clicked_sug}", flush=True)
            time.sleep(0.5)
            return

        # No autocomplete — press Enter
        inp.press("Enter")
        time.sleep(0.5)
        print(f"    [!] No autocomplete found, pressed Enter", flush=True)

    except Exception as e:
        print(f"    [!] Source filter skipped: {e}", flush=True)


def _set_dedup(page: Page):
    """Set Duplicates filter to Off — auto-detect the select element via JS."""
    try:
        # Expand any Options/Display tab first
        for selector in ["a:has-text('Options')", "a:has-text('Display')",
                         "#optTab a", "#displayTab a", ".fesTabLinkFix"]:
            try:
                page.click(selector, timeout=1_500)
                time.sleep(0.8)
                break
            except Exception:
                pass

        # We know the select id is "isrd" from diagnostics — set it directly
        result = page.evaluate("""() => {
            const selects = document.getElementById('isrd')
                ? [document.getElementById('isrd')]
                : Array.from(document.querySelectorAll('select'));
            for (const sel of selects) {
                const opts = Array.from(sel.options).map(o => o.text.toLowerCase());
                // Match select that has duplicate/similar/off-related options
                if (sel.id === 'isrd' || opts.some(t =>
                        t.includes('duplicate') || t.includes('similar') ||
                        t.includes('off') || t.includes('no dup'))) {
                    // Pick the option that means "no duplicates":
                    // look for "off", "no dup", "similar" (Factiva uses "Similar" for dedup)
                    // The last option is usually the most restrictive
                    const offIdx = Array.from(sel.options).findIndex(o => {
                        const t = o.text.trim().toLowerCase();
                        return t === 'off' || t.includes('no dup') || t.includes('off');
                    });
                    // If no "off" found, pick index 1 (first non-default option)
                    const idx = offIdx >= 0 ? offIdx : (sel.options.length > 1 ? 1 : -1);
                    if (idx >= 0) {
                        sel.selectedIndex = idx;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        const opt = sel.options[idx];
                        return `id=${sel.id || sel.name} idx=${idx} label=${opt ? opt.text : '?'}`;
                    }
                }
            }
            return 'NOT_FOUND:' + selects.map(s =>
                (s.id||s.name) + '=[' + Array.from(s.options).map(o=>o.value+':'+o.text).join('|') + ']'
            ).join(' ;; ');
        }""")

        if result and not result.startswith("NOT_FOUND"):
            print(f"[*] Dedup -> Off ({result})", flush=True)
        else:
            # Log available selects so we can fix the selector next time
            info = (result or "").replace("NOT_FOUND:", "")
            print(f"    [!] Dedup: select not found. Available: {info[:300]}", flush=True)
    except Exception as e:
        print(f"    [!] Dedup skipped: {e}", flush=True)


# ── frame helpers ─────────────────────────────────────────────────────────────

def _results_frame(page: Page) -> Frame | None:
    # Check all frames INCLUDING the main frame
    frames_to_check = list(page.frames)
    # Also ensure main frame is included (it may not be in page.frames on some versions)
    if page.main_frame not in frames_to_check:
        frames_to_check.insert(0, page.main_frame)

    for f in frames_to_check:
        try:
            if f.evaluate("() => document.querySelectorAll('input[name=hdl]').length") > 0:
                return f
        except Exception:
            pass
    return None


def _wait_for_results_frame(page: Page, timeout: int = 60_000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if _results_frame(page):
            return
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for article checkboxes")


# ── page export ───────────────────────────────────────────────────────────────

def export_page_rtf(page: Page, page_num: int, tmp_dir: Path) -> Path | None:
    """Select all on current results page and download RTF. Returns saved path or None."""
    frame = _results_frame(page)
    if not frame:
        return None

    count = frame.evaluate("() => document.querySelectorAll('input[name=hdl]').length")
    if count == 0:
        return None

    print(f"[*] Page {page_num}: {count} articles...", flush=True)

    frame.evaluate("""() => {
        const sa = document.querySelector('input[title="Select All"]');
        if (sa && !sa.checked) sa.click();
    }""")
    time.sleep(0.5)

    # Verify selection
    checked = frame.evaluate("() => document.querySelectorAll('input[name=hdl]:checked').length")
    if checked == 0:
        frame.evaluate("() => document.querySelectorAll('input[name=hdl]').forEach(cb => { if(!cb.checked) cb.click(); })")
        time.sleep(0.5)

    out_path = tmp_dir / f"page_{page_num:03d}.rtf"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, 4):  # up to 3 attempts
        try:
            with page.expect_download(timeout=120_000) as dl_info:
                frame.evaluate("() => viewProcessing('../pps/default.aspx?pp=RTF&ppstype=Article', true)")
            dl_info.value.save_as(out_path)
            print(f"    saved {out_path.name} ({out_path.stat().st_size:,} bytes)", flush=True)
            return out_path
        except Exception as e:
            print(f"    [!] Download attempt {attempt} failed: {e}", flush=True)
            if attempt < 3:
                time.sleep(5)
                # Re-select all before retry
                frame.evaluate("""() => {
                    const sa = document.querySelector('input[title="Select All"]');
                    if (sa) { sa.checked = false; sa.click(); }
                }""")
                time.sleep(1)
            else:
                print(f"    [!] Skipping page {page_num} after 3 failed attempts", flush=True)
                return None


def go_next_page(page: Page) -> bool:
    frame = _results_frame(page)
    if not frame:
        return False
    has_next = frame.evaluate("""() => {
        const btn = document.querySelector('a.nextItem');
        return btn ? btn.offsetParent !== null : false;
    }""")
    if not has_next:
        return False
    frame.evaluate("() => document.querySelector('a.nextItem').click()")
    time.sleep(2)
    try:
        _wait_for_results_frame(page, timeout=20_000)
    except RuntimeError:
        return False
    return True


# ── RTF merge ─────────────────────────────────────────────────────────────────

def merge_rtf_files(rtf_files: list[Path], out_path: Path):
    """Merge multiple RTF files into one, inserting page breaks between them."""
    if not rtf_files:
        raise ValueError("No RTF files to merge")

    print(f"[*] Merging {len(rtf_files)} RTF files -> {out_path.name}", flush=True)

    # RTF files from Factiva all share the same header (font/color tables).
    # Strategy: keep header from file 1, extract body from each, join with \page.
    PAGE_BREAK = b"\n\\page\n"

    first_data = rtf_files[0].read_bytes()
    header_end = first_data.find(b"\\pard", 3000)
    if header_end == -1:
        header_end = 3089  # fallback

    header = first_data[:header_end]

    # Measure how many closing braces the header leaves unclosed
    h_balance = header.count(b"{") - header.count(b"}")

    def extract_body(data: bytes) -> bytes:
        """Strip exactly h_balance closing braces from the body tail.

        Each file's body closes the header's h unclosed groups, so we strip those
        tail braces and let the merged document close them once at the very end.
        """
        body = data[header_end:]
        stripped = bytearray(body.rstrip())
        for _ in range(h_balance):
            pos = stripped.rfind(b"}")
            if pos != -1:
                del stripped[pos]
        return bytes(stripped)

    bodies = [extract_body(first_data)]
    for rtf_file in rtf_files[1:]:
        bodies.append(extract_body(rtf_file.read_bytes()))

    # The closing braces that go at the very end (once for the whole document)
    closing = b"}" * h_balance

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header)
        for i, body in enumerate(bodies):
            if i > 0:
                f.write(PAGE_BREAK)
            f.write(body)
        f.write(b"\n")
        f.write(closing)

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[+] Output: {out_path.resolve()}  ({size_mb:.1f} MB)", flush=True)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    username = os.getenv("FACTIVA_USER")
    password = os.getenv("FACTIVA_PASS")
    if not username or not password:
        sys.exit("ERROR: Set FACTIVA_USER and FACTIVA_PASS in .env")

    out_path   = Path(args.out)
    tmp_dir    = out_path.parent / f".tmp_{out_path.stem}"
    state_file = Path(__file__).parent / ".session_state.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)

        # Load saved session if available
        ctx_kwargs = dict(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
            print("[*] Loading saved session...", flush=True)

        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        try:
            # Check if session is still valid by opening Factiva
            page.goto("https://global.factiva.com/", wait_until="networkidle", timeout=60_000)
            try:
                page.click("button:has-text('Accept')", timeout=3_000)
            except PWTimeout:
                pass
            page.wait_for_load_state("networkidle", timeout=15_000)

            still_logged_in = "accounts.dowjones.com" not in page.url and \
                              "login" not in page.url.lower()

            if still_logged_in:
                print("[+] Session restored — skipping login", flush=True)
            else:
                print("[*] Session expired, logging in...", flush=True)
                login(page, username, password)
                # Save session for next run
                context.storage_state(path=str(state_file))
                print(f"[+] Session saved to {state_file.name}", flush=True)

            if args.search:
                open_saved_search(page, args.search)
            else:
                run_custom_search(page, args.query, args.date, args.source, dedup=args.dedup)

            # Try to read total result count from the page
            try:
                frame0 = _results_frame(page)
                if frame0:
                    total_txt = frame0.evaluate("""() => {
                        const el = document.querySelector('.hd-count, .hdCount, #hitCount, .resultcount, [class*="count"]');
                        return el ? el.textContent.trim() : null;
                    }""")
                    if total_txt:
                        print(f"[+] Total articles found: {total_txt}", flush=True)
            except Exception:
                pass

            rtf_files = []
            page_num  = 1
            total_articles = 0
            while True:
                path = export_page_rtf(page, page_num, tmp_dir)
                if path is None:
                    print("[*] No more articles", flush=True)
                    break
                rtf_files.append(path)
                total_articles += 20  # Factiva shows 20 per page

                if args.max_pages and page_num >= args.max_pages:
                    print(f"[*] Reached max-pages limit ({args.max_pages})", flush=True)
                    break

                if not go_next_page(page):
                    print(f"[*] All pages done", flush=True)
                    break

                page_num += 1
                print(f"[*] Moving to page {page_num}, collected ~{total_articles} articles...", flush=True)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            page.screenshot(path="error_screenshot.png")
            raise
        finally:
            context.close()
            browser.close()

    if rtf_files:
        merge_rtf_files(rtf_files, out_path)
        # Clean up temp files
        for f in rtf_files:
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()
        total = len(rtf_files) * 20
        print(f"[+] Done — ~{total} articles exported to {out_path.name}", flush=True)
    else:
        print("[!] No articles collected", flush=True)


if __name__ == "__main__":
    main()
