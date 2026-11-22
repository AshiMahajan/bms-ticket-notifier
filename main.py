"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks configured dates, and emails when availability changes.
State is persisted in bms_state.json between GitHub Actions runs.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from playwright.sync_api import sync_playwright

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────

CONFIG = {
    # Set this in GitHub Actions → Settings → Secrets and variables → Variables.
    # No movie URL is hardcoded here.
    "url": os.getenv("BMS_URL", "").strip(),
    "dates": os.getenv("BMS_DATES", "").strip(),
    "theatre": os.getenv("BMS_THEATRE", "").strip(),
    "time_period": os.getenv("BMS_TIME", "").strip(),
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
STATE_FILE = "bms_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────

AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default": "AVAILABLE",
}

TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}

REGION_MAP = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
}

# ──────────────────────────────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────────────────────────────

@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)


@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────

def parse_bms_url(url):
    path_parts = urlparse(url).path.strip("/").split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}

    for part in path_parts:
        if re.fullmatch(r"ET\d{8,}", part):
            result["event_code"] = part
        elif re.fullmatch(r"\d{8}", part):
            result["date_code"] = part

    if "movies" in path_parts:
        index = path_parts.index("movies")
        if index + 1 < len(path_parts):
            result["region_slug"] = path_parts[index + 1]

    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    return REGION_MAP.get(key, (key.upper()[:6], key, "0", "0", ""))


# ──────────────────────────────────────────────────────────────────────
# BOOKMYSHOW — PLAYWRIGHT BROWSER FETCH
# ──────────────────────────────────────────────────────────────────────

def build_booking_url(event_code, date_code):
    """Build the date-specific booking URL from the configured BMS_URL."""
    parsed = urlparse(CONFIG["url"])
    path_parts = parsed.path.rstrip("/").split("/")

    try:
        event_index = path_parts.index(event_code)
    except ValueError:
        print("  ❌ Event code was not found in BMS_URL path.")
        return None

    if date_code:
        if event_index + 1 < len(path_parts) and re.fullmatch(
            r"\d{8}", path_parts[event_index + 1]
        ):
            path_parts[event_index + 1] = date_code
        else:
            path_parts.insert(event_index + 1, date_code)

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query_items = [
        (key, event_code if key == "refEventCode" else value)
        for key, value in query_items
    ]
    query = urlencode(query_items, doseq=True, safe="*")

    return urlunparse(
        parsed._replace(path="/".join(path_parts), query=query)
    )


def fetch_bms(
    event_code,
    date_code,
    region_code,
    region_slug,
    lat,
    lon,
    geohash,
):
    """Open the real BMS booking page and capture its showtime JSON response.

    The region arguments remain in the signature so main() and the existing
    configuration flow do not need to change. Chromium supplies the browser
    request headers, including the correct page Referer, automatically.
    """
    booking_url = build_booking_url(event_code, date_code)
    if not booking_url:
        return None

    print(f"  Opening booking page for {date_code or '(default date)'}...")
    captured_responses = []

    def capture_response(response):
        response_url = response.url.lower()
        if "showtimes-by-event" in response_url:
            captured_responses.append(response)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            try:
                context = browser.new_context(
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                )
                page = context.new_page()
                page.on("response", capture_response)

                page_response = page.goto(
                    booking_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                if page_response:
                    print(
                        "  Booking page HTTP status: "
                        f"{page_response.status}"
                    )

                # Allow the booking page's JavaScript and API requests to run.
                page.wait_for_timeout(15000)
                print(f"  Page title: {page.title()}")

                for response in reversed(captured_responses):
                    if response.status != 200:
                        print(
                            "  BMS showtime API HTTP status: "
                            f"{response.status}"
                        )
                        continue

                    try:
                        payload = response.json()
                    except Exception:
                        continue

                    if not isinstance(payload, dict):
                        continue

                    data_section = payload.get("data", {})
                    if isinstance(data_section, dict) and any(
                        key in data_section
                        for key in (
                            "showtimeWidgets",
                            "topStickyWidgets",
                            "bottomSheetData",
                        )
                    ):
                        print("  ✅ Captured BMS showtime JSON response.")
                        return payload

                    print(
                        "  BMS response was JSON, but did not contain "
                        "the expected showtime data structure."
                    )

                if not captured_responses:
                    print("  No showtime API response was captured.")
                else:
                    print("  No usable showtime JSON response was captured.")

                title = page.title().lower()
                if "just a moment" in title or "attention required" in title:
                    print("  ⚠️ The page appears to show a browser security challenge.")

                return None
            finally:
                browser.close()

    except Exception as error:
        print(f"  Playwright failed: {error}")
        return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────

def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}

    for widget in data.get("data", {}).get("topStickyWidgets", []):
        if widget.get("type") == "horizontal-text-list":
            for item in widget.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for component in row.get("components", []):
                        text_value = component.get("text", "")
                        if "•" in text_value:
                            info["language"] = text_value.strip()

    bottom_sheet = data.get("data", {}).get("bottomSheetData", {})
    for widget in bottom_sheet.get("format-selector", {}).get("widgets", []):
        if widget.get("type") == "vertical-text-list":
            for item in widget.get("data", []):
                if item.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = item.get("text", info["name"])

    return info


def parse_dates(data):
    dates = []
    for widget in data.get("data", {}).get("topStickyWidgets", []):
        if widget.get("type") != "horizontal-block-list":
            continue

        for item in widget.get("data", []):
            if len(item.get("data", [])) >= 3:
                style = item.get("styleId", "")
                dates.append(
                    DateInfo(
                        date_code=item.get("id", ""),
                        status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                    )
                )

    return dates


def parse_shows(data):
    shows = []
    for widget in data.get("data", {}).get("showtimeWidgets", []):
        if widget.get("type") != "groupList":
            continue

        for group in widget.get("data", []):
            if group.get("type") != "venueGroup":
                continue

            for card in group.get("data", []):
                if card.get("type") != "venue-card":
                    continue

                additional = card.get("additionalData", {})
                venue_name = additional.get("venueName", "Unknown")
                venue_code = additional.get("venueCode", "")

                for showtime in card.get("showtimes", []):
                    show_data = showtime.get("additionalData", {})
                    date_code = str(
                        show_data.get("showDateCode", "")
                        or show_data.get("dateCode", "")
                    ).strip()
                    cutoff = show_data.get("cutOffDateTime", "")
                    if not date_code and re.match(r"^\d{8}", cutoff):
                        date_code = cutoff[:8]

                    show = ShowInfo(
                        venue_code=venue_code,
                        venue_name=venue_name,
                        session_id=show_data.get("sessionId", ""),
                        date_code=date_code,
                        time=showtime.get("title", ""),
                        time_code=show_data.get("showTimeCode", ""),
                        screen_attr=(
                            showtime.get("screenAttr", "")
                            or show_data.get("attributes", "")
                        ),
                    )

                    for category in show_data.get("categories", []):
                        show.categories.append(
                            CatInfo(
                                name=category.get("priceDesc", ""),
                                price=str(category.get("curPrice", "0")),
                                status=str(category.get("availStatus", "")),
                            )
                        )

                    shows.append(show)

    return shows


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────

def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    keywords = (
        [item.strip().lower() for item in theatre_filter.split(",") if item.strip()]
        if theatre_filter else []
    )
    periods = (
        [item.strip().lower() for item in time_periods.split(",") if item.strip()]
        if time_periods else []
    )
    dates_set = (
        {item.strip() for item in date_codes.split(",") if item.strip()}
        if date_codes else set()
    )

    for show in shows:
        if keywords and not any(
            keyword in show.venue_name.lower() for keyword in keywords
        ):
            continue

        if dates_set and show.date_code and show.date_code not in dates_set:
            continue

        if periods:
            try:
                time_code = int(show.time_code)
            except (ValueError, TypeError):
                time_code = 0

            if not any(
                period in TIME_PERIODS
                and TIME_PERIODS[period][0] <= time_code < TIME_PERIODS[period][1]
                for period in periods
            ):
                continue

        result.append(show)

    return result


# ──────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT — CHANGE DETECTION BETWEEN RUNS
# ──────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def build_state(shows, dates):
    show_state = {}
    for show in shows:
        for category in show.categories:
            key = (
                f"{show.venue_code}|{show.session_id}|"
                f"{show.date_code}|{category.name}"
            )
            show_state[key] = {
                "venue": show.venue_name,
                "time": show.time,
                "date": show.date_code,
                "cat": category.name,
                "price": category.price,
                "status": category.status,
            }

    date_state = {date.date_code: date.status for date in dates}
    return {"shows": show_state, "dates": date_state}


def detect_changes(old_state, new_state):
    changes = []
    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})

    for date_code, status in new_dates.items():
        old_status = old_dates.get(date_code)
        if old_status == "NOT_OPEN" and status in ("BOOKABLE", "AVAILABLE"):
            changes.append(f"📅 NEW DATE OPENED: {date_code}")

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    for key in set(new_shows) - set(old_shows):
        show = new_shows[key]
        changes.append(
            f"🆕 NEW: {show['venue']} {show['time']} [{show['date']}] "
            f"— {show['cat']} ₹{show['price']}"
        )

    for key, new_show in new_shows.items():
        old_show = old_shows.get(key)
        if old_show and old_show["status"] == "0" and new_show["status"] != "0":
            label, icon = AVAIL_STATUS_MAP.get(
                new_show["status"], ("UNKNOWN", "⚪")
            )
            changes.append(
                f"{icon} BACK: {new_show['venue']} {new_show['time']} "
                f"[{new_show['date']}] — {new_show['cat']} → {label}"
            )

    return changes


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION — RESEND
# ──────────────────────────────────────────────────────────────────────

def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


def send_email(subject, changes, shows, movie_info):
    api_key = RESEND_API_KEY.strip()
    recipient = RESEND_TO_EMAIL.strip()
    sender = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not recipient:
        print("  ⚠️ Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        sys.exit(1)

    now_string = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")

    changes_html = ""
    if changes:
        change_rows = "".join(
            f'<li style="padding:3px 0;font-size:14px;">{escape(change)}</li>'
            for change in changes
        )
        changes_html = f"""
        <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
            Changes Detected
        </h3>
        <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
            {change_rows}
        </ul>"""

    venue_groups = {}
    for show in shows:
        venue_groups.setdefault(show.venue_name, []).append(show)

    shows_html = ""
    for venue_name, venue_shows in venue_groups.items():
        show_rows = ""
        for show in venue_shows:
            categories = " | ".join(
                f"{escape(category.name)} Rs.{escape(str(category.price))} "
                f"({_cat_status_label(category.status)})"
                for category in show.categories
            )
            screen_format = (
                f" [{escape(show.screen_attr)}]" if show.screen_attr else ""
            )
            show_rows += (
                "<tr>"
                '<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                'font-size:13px;vertical-align:top;">'
                f"{escape(show.time)}{screen_format}</td>"
                '<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">{categories}</td>'
                "</tr>"
            )

        shows_html += f"""
        <p style="margin:14px 0 4px 0;font-size:14px;font-weight:bold;color:#333;">
            {escape(venue_name)}
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#f5f5f5;">
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;">Time</th>
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;">Categories</th>
            </tr>
            {show_rows}
        </table>"""

    html_body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;
             font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:18px;color:#111;">
        BMS Alert: {escape(movie_name)}
    </h2>
    <p style="margin:0 0 20px 0;font-size:13px;color:#666;">{escape(now_string)}</p>
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 20px 0;">
    {changes_html}
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
        Current Showtimes
    </h3>
    {shows_html}
    <p style="margin:24px 0 0 0;font-size:12px;color:#999;">
        This is an automated alert from BMS Ticket Notifier.
    </p>
</body>
</html>"""

    plain_lines = [subject, "", f"Checked at: {now_string}", ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {change}" for change in changes)
        plain_lines.append("")

    plain_lines.append("Current Showtimes:")
    for venue_name, venue_shows in venue_groups.items():
        plain_lines.append(f"\n{venue_name}")
        for show in venue_shows:
            categories = " | ".join(
                f"{category.name} Rs.{category.price} "
                f"({_cat_status_label(category.status)})"
                for category in show.categories
            )
            screen_format = f" [{show.screen_attr}]" if show.screen_attr else ""
            plain_lines.append(f"  {show.time}{screen_format} - {categories}")

    plain_lines.extend(["", "This is an automated alert from BMS Ticket Notifier."])

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": "\n".join(plain_lines),
                "html": html_body,
            },
            timeout=15,
        )

        if response.status_code in (200, 201):
            print(f"  ✅ Email sent to {recipient}")
        else:
            print(f"  ❌ Resend {response.status_code}: {response.text}")
            sys.exit(1)

    except requests.RequestException as error:
        print(f"  ❌ Email failed: {error}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def main():
    now_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_string}] BMS Ticket Checker — CI mode")

    # Temporary Resend email test mode.
    if os.getenv("SEND_TEST_EMAIL", "").lower() == "true":
        print("  📧 Running Resend email test...")
        send_email(
            "BMS Ticket Notifier - Test Email",
            [
                "This is a test email. "
                "Resend sender and recipient configuration is working."
            ],
            [],
            {"name": "Resend Configuration Test"},
        )
        print("  ✅ Resend test completed.")
        return

    if not CONFIG["url"]:
        print("  ❌ BMS_URL is empty. Set it in GitHub repository variables.")
        sys.exit(1)

    parsed = parse_bms_url(CONFIG["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("  ❌ Invalid BMS_URL. Could not extract event code or region.")
        sys.exit(1)

    region_code, region_slug_resolved, lat, lon, geohash = resolve_region(
        region_slug
    )

    raw_dates = CONFIG["dates"].strip()
    if raw_dates:
        date_list = [item.strip() for item in raw_dates.split(",") if item.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print(
        f"  Event: {event_code}  Region: {region_code}  Dates: {date_list}"
    )

    all_shows = []
    all_dates = []
    received_data = False
    movie_info = {"name": "Unknown", "language": ""}

    for date_code in date_list:
        data = fetch_bms(
            event_code,
            date_code,
            region_code,
            region_slug_resolved,
            lat,
            lon,
            geohash,
        )

        if not data:
            print(f"  ⚠️ No data for date {date_code or '(default)'}")
            continue

        received_data = True
        if movie_info["name"] == "Unknown":
            movie_info = parse_movie_info(data)

        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))

    # Do not mistake a blocked/failed fetch for a genuine zero-showtime result.
    if not received_data:
        print("  ❌ Could not retrieve BMS data for any configured date.")
        sys.exit(1)

    if not all_shows:
        print("  ⚠️ No showtimes in the captured data; checking date availability.")

    print(f"  🎬 {movie_info['name']} {movie_info['language']}")

    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
    )
    print(f"  📊 {len(filtered)} showtime(s) after filters")

    new_state = build_state(filtered, all_dates)
    old_state = load_state()
    changes = detect_changes(old_state, new_state) if old_state else []
    save_state(new_state)

    if changes:
        print(f"\n  ⚡ {len(changes)} change(s) detected:")
        for change in changes:
            print(f"     {change}")

        send_email(
            f"BMS Alert: {movie_info['name']} - {len(changes)} change(s)",
            changes,
            filtered,
            movie_info,
        )
    else:
        print("  ✅ No changes since last check.")

    print(f"\n  Current status ({len(filtered)} shows):")
    for show in filtered:
        categories = ", ".join(
            f"{category.name}=₹{category.price}"
            f"({AVAIL_STATUS_MAP.get(category.status, ('?', ''))[0]})"
            for category in show.categories
        )
        screen_format = f"|{show.screen_attr}" if show.screen_attr else ""
        print(
            f"    {show.venue_name} — {show.time}{screen_format} "
            f"[{show.date_code}] — {categories}"
        )

    print("\n  Done.")


if __name__ == "__main__":
    main()
