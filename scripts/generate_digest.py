#!/usr/bin/env python3
"""
The Watchlist — daily credit-relevant news digest generator.

What this script does, in order:
  1. Loads config.json (sectors, companies, geography, preferred sources, email settings).
  2. Optionally pulls recent messages from a dedicated inbox over IMAP (behind
     config["email"]["enabled"]).
  3. Calls the Anthropic API (Claude + the web_search tool) to find and categorise
     credit-relevant news for the configured coverage, folding in anything pulled
     from email.
  4. Parses the model's structured JSON answer into a list of news items.
  5. Prepends today's edition to a rolling history file (digest_history.json).
  6. Renders index.html from scripts/template.html, most recent edition first.

Environment variables expected (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY   - required
  EMAIL_APP_PASSWORD  - required only if config["email"]["enabled"] is true
"""

import os
import re
import sys
import json
import html
import difflib
import imaplib
import email as email_lib
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "digest_history.json"
TEMPLATE_PATH = ROOT / "scripts" / "template.html"
OUTPUT_PATH = ROOT / "index.html"
ARCHIVE_PATH = ROOT / "archive.html"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
VALID_FLAGS = {"positive", "negative", "watch", "neutral"}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Email ingestion
# ---------------------------------------------------------------------------

def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="replace"
        )
    except Exception:
        return ""


def fetch_email_items(config: dict) -> list:
    """Pull recent messages from the dedicated inbox. Returns a list of
    {sender, subject, snippet, date} dicts. Never touches sensitive data
    beyond subject + a short plain-text snippet passed to the model."""
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled"):
        return []

    password = os.environ.get("EMAIL_APP_PASSWORD")
    username = email_cfg.get("username")
    if not password or not username:
        print("Email enabled but EMAIL_APP_PASSWORD or username missing; skipping email step.")
        return []

    host = email_cfg.get("imap_host", "imap.gmail.com")
    port = email_cfg.get("imap_port", 993)
    folder = email_cfg.get("folder", "INBOX")
    lookback_hours = email_cfg.get("lookback_hours", 26)
    mark_as_read = email_cfg.get("mark_as_read", False)

    items = []
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(username, password)
        conn.select(folder)

        since_date = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            conn.logout()
            return []

        msg_ids = data[0].split()
        fetch_flag = "(RFC822)" if mark_as_read else "(BODY.PEEK[])"

        for msg_id in msg_ids:
            status, msg_data = conn.fetch(msg_id, fetch_flag)
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            sender = _decode(msg.get("From", ""))
            subject = _decode(msg.get("Subject", ""))
            body = _extract_body(msg)
            snippet = " ".join(body.split())[:1200]

            items.append({
                "sender": sender,
                "subject": subject,
                "snippet": snippet,
                "date": msg.get("Date", ""),
            })

        conn.logout()
    except Exception as exc:
        print(f"IMAP fetch failed, continuing without email content: {exc}")
        return []

    return items


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def summarize_recent_coverage(history: list, days: int = 6, max_items: int = 60) -> str:
    """Build a compact 'already reported' block from recent editions, so the
    model can avoid re-reporting the same underlying story. Only titles +
    topics are included (not full summaries) to keep this cheap in tokens.
    Caps at max_items total to bound prompt size even with a busy week."""
    if not history:
        return "No prior editions yet — this is the first one."

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    lines = []
    for edition in history:
        try:
            edition_date = datetime.strptime(edition["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if edition_date < cutoff:
            break  # history is newest-first, so we can stop early
        for item in edition.get("items", []):
            lines.append(f"- [{item.get('topic', '?')}] {item.get('title', '')}")
            if len(lines) >= max_items:
                break
        if len(lines) >= max_items:
            break

    if not lines:
        return "No prior editions in the last few days."
    return "\n".join(lines)


def build_prompt(config: dict, email_items: list, recent_coverage: str) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")

    sources_line = (
        ", ".join(config["preferred_sources"])
        if config.get("preferred_sources")
        else "no fixed source list yet — use your judgement and prioritise reputable financial/trade press"
    )

    email_block = "No newsletter/inbox content was supplied for this edition."
    if email_items:
        lines = []
        for it in email_items:
            lines.append(
                f"- From: {it['sender']} | Subject: {it['subject']} | Date: {it['date']}\n"
                f"  Content: {it['snippet']}"
            )
        email_block = (
            "The following newsletter/alert emails were received in the dedicated inbox "
            "since the last edition. Fold anything credit-relevant from these into the "
            "digest, tagged by sender rather than a web source:\n" + "\n".join(lines)
        )

    return f"""You are producing today's edition ({today}) of "The Watchlist", a daily
credit-analyst-style news digest.

COVERAGE
Sectors: {", ".join(config["sectors"])}
Named companies: {", ".join(config["companies"])}
Geography: {config["geography"]}
Preferred sources: {sources_line}

TASK
1. Use the web_search tool to find genuinely new, credit-relevant news from the last
   24-48 hours across the sectors and companies above. Prioritise items that would
   matter to a credit analyst: ratings actions, debt issuance/refinancing, covenant or
   liquidity news, M&A, regulatory/tariff changes, earnings surprises, guidance changes,
   management changes, litigation, commodity/price moves relevant to the sector, and
   material operational events (outages, strikes, supply disruption).
2. Also incorporate the inbox content below where relevant.
3. Skip generic/non-credit-relevant stories (marketing news, minor product launches) unless
   they have a plausible credit angle.
4. CRITICAL — avoid re-reporting old news. The ALREADY REPORTED section below lists
   headlines from the last several editions. Before including an item, check it against
   that list:
   - If it's the same underlying story/event as something already reported (even if
     phrased differently, or from a different outlet), DO NOT include it, UNLESS there is
     a genuinely new, material development (e.g. a deal that was "progressing" has now
     closed; a "guidance range" has been replaced by actual results; a rumour is now
     confirmed). If including a follow-up, make the headline and summary clearly reflect
     what specifically changed since the last report, not a restatement of the same facts.
   - A running situation (e.g. an ongoing M&A process, an ongoing geopolitical situation
     affecting commodity prices) should only reappear when there's a concrete new
     development, not as a general recap of the situation's current state.
   - When in doubt, leave it out — it is much better to return fewer items than to pad
     the digest with repeats.

ALREADY REPORTED (last several editions — do not repeat these unless there's a
genuinely new development, per the rule above)
{recent_coverage}

INBOX CONTENT
{email_block}

OUTPUT FORMAT
Respond with ONLY a JSON array (no markdown fences, no preamble, no commentary, no
citation markup such as <cite> tags — plain text only) of objects, each with exactly
these fields:
  "category": "industry" or "company"
  "topic": the sector name (for industry items) or company name (for company items) —
           must match one of the coverage names above as closely as possible
  "title": a short headline, in your own words, under 100 characters
  "summary": 2-3 sentences (aim for under 300 characters) in your own words explaining
             the news and why it matters from a credit perspective. Never quote
             source text verbatim, and never include citation tags or brackets.
  "credit_flag": one of "positive", "negative", "watch", "neutral" — your assessment of
                 the likely direction of credit impact
  "source": either the publication name (e.g. "Reuters") or, for inbox items, the sender
  "url": the source URL if from the web, or "" if from an inbox email

Return up to 16 items, covering as much of the sector/company list as genuine *new*
news allows. There is no minimum — on a quiet news day, or a day where most ongoing
stories haven't materially advanced, it is completely acceptable to return only 2-3
items, or even zero for a sector/company. If there is truly nothing credit-relevant
and new for a sector or company, simply omit it rather than inventing something or
re-reporting old news to pad the count."""


def call_claude(config: dict, prompt: str) -> list:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    messages = [{"role": "user", "content": prompt}]
    body = {
        "model": config.get("model", "claude-haiku-4-5-20251001"),
        "max_tokens": 2800,
        "messages": messages,
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": config.get("max_web_searches", 8),
        }],
    }

    collected_text = []

    # Loop to let the model use web_search across multiple turns if it wants to,
    # stopping once it returns a normal (non tool-use) end turn. Capped at 3 turns
    # to bound cost — each turn re-sends the growing conversation, so more turns
    # multiplies token spend fast.
    for _ in range(3):
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        collected_text.extend(text_blocks)

        if data.get("stop_reason") != "tool_use":
            break

        # Web search tool_use blocks are executed server-side by Anthropic and the
        # results come back as server_tool_use / web_search_tool_result content
        # blocks in the same response, so we just need to continue the conversation
        # to let the model keep reasoning over them.
        messages.append({"role": "assistant", "content": data["content"]})
        messages.append({
            "role": "user",
            "content": "Continue. Remember: final answer must be ONLY the JSON array described above.",
        })
        body["messages"] = messages

    final_text = "\n".join(collected_text).strip()
    return parse_json_items(final_text)


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/numbers noise for similarity comparison."""
    t = title.lower()
    t = re.sub(r"[^a-z\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _is_similar(a: str, b: str, threshold: float = 0.72) -> bool:
    return difflib.SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio() >= threshold


_STOPWORDS = {
    "the", "a", "an", "to", "for", "on", "in", "of", "and", "or", "as", "at",
    "amid", "with", "from", "over", "into", "its", "is", "are", "up", "down",
}


def _keywords(title: str) -> set:
    """Extract distinctive words from a title (proper nouns, numbers, key
    terms) for a cruder but more reliable overlap check than pure string
    similarity, since this project's model tends to reword headlines
    substantially even when reporting the identical underlying story."""
    words = _normalize_title(title).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _is_same_story(a: str, b: str, sim_threshold: float = 0.72, keyword_overlap_threshold: float = 0.55) -> bool:
    """Two signals, either sufficient: (1) high string similarity, or (2) high
    overlap in distinctive keywords (catches heavily-reworded repeats of the
    same story that string similarity misses)."""
    if _is_similar(a, b, threshold=sim_threshold):
        return True
    kw_a, kw_b = _keywords(a), _keywords(b)
    if not kw_a or not kw_b:
        return False
    overlap = len(kw_a & kw_b) / min(len(kw_a), len(kw_b))
    return overlap >= keyword_overlap_threshold


def dedupe_items(items: list) -> list:
    """Drop items that are near-duplicates of an earlier item in the same list
    (same topic + similar title = almost certainly the same underlying story
    reported twice by the model in one response)."""
    kept = []
    for it in items:
        is_dupe = False
        for prior in kept:
            if it["topic"] == prior["topic"] and _is_same_story(it["title"], prior["title"]):
                is_dupe = True
                break
        if not is_dupe:
            kept.append(it)
    dropped = len(items) - len(kept)
    if dropped:
        print(f"[dedupe] Dropped {dropped} same-day duplicate item(s).")
    return kept


def filter_against_recent_history(items: list, history: list, days: int = 4, threshold: float = 0.78) -> list:
    """Safety net: drop items that are near-duplicates of something already
    published in the last N editions. This runs AFTER the prompt-level
    instruction, catching anything that slips through despite it. Slightly
    higher similarity threshold than same-day dedup, since day-to-day
    phrasing naturally varies more and we only want to catch clear repeats,
    not legitimately-related follow-ups."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    recent_titles = []  # (topic, title)
    for edition in history:
        try:
            edition_date = datetime.strptime(edition["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if edition_date < cutoff:
            break
        for it in edition.get("items", []):
            recent_titles.append((it.get("topic", ""), it.get("title", "")))

    kept = []
    for it in items:
        is_repeat = any(
            it["topic"] == topic and _is_same_story(it["title"], title, sim_threshold=threshold, keyword_overlap_threshold=0.65)
            for topic, title in recent_titles
        )
        if is_repeat:
            print(f"[dedupe] Dropped likely repeat of recent story: {it['title'][:70]}")
            continue
        kept.append(it)
    return kept


def parse_json_items(text: str) -> list:
    # Defensive: strip any citation markup the model may have inserted despite
    # instructions not to (Anthropic auto-inserts these when web_search is used).
    # Keep the inner text, drop the tag itself, e.g. <cite index="1-2">X</cite> -> X
    text = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'</?cite[^>]*>', '', text)

    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        print("Failed to parse model output as JSON:")
        print(text[:2000])
        raise exc

    valid_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        flag = it.get("credit_flag", "neutral")
        if flag not in VALID_FLAGS:
            flag = "neutral"
        valid_items.append({
            "category": it.get("category", "industry"),
            "topic": it.get("topic", ""),
            "title": it.get("title", "(untitled)"),
            "summary": it.get("summary", ""),
            "credit_flag": flag,
            "source": it.get("source", ""),
            "url": it.get("url", ""),
        })
    return dedupe_items(valid_items)


# ---------------------------------------------------------------------------
# History + rendering
# ---------------------------------------------------------------------------

def load_history() -> list:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list, keep: int) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history[:keep], f, indent=2, ensure_ascii=False)


FLAG_LABEL = {
    "positive": "Positive",
    "negative": "Negative",
    "watch": "Watch",
    "neutral": "Neutral",
}


def render_item(item: dict) -> str:
    flag = item["credit_flag"]
    label = FLAG_LABEL.get(flag, "Neutral")
    title = html.escape(item["title"])
    summary = html.escape(item["summary"])
    topic = html.escape(item["topic"])
    source = html.escape(item["source"])
    url = html.escape(item["url"])

    source_html = f'<a href="{url}" target="_blank" rel="noopener">{source}</a>' if url else source

    return f"""
        <article class="item">
          <span class="flag flag-{flag}" title="Credit signal: {label}">{label}</span>
          <div class="item-body">
            <div class="item-tag">{topic}</div>
            <h3 class="item-title">{title}</h3>
            <p class="item-summary">{summary}</p>
            <div class="item-source">{source_html}</div>
          </div>
        </article>"""


def render_edition(edition: dict) -> str:
    industry_items = [i for i in edition["items"] if i["category"] == "industry"]
    company_items = [i for i in edition["items"] if i["category"] == "company"]

    sections = []
    if industry_items:
        sections.append(
            '<section class="category">\n<h2>Industry News</h2>\n'
            + "".join(render_item(i) for i in industry_items)
            + "\n</section>"
        )
    if company_items:
        sections.append(
            '<section class="category">\n<h2>Company News</h2>\n'
            + "".join(render_item(i) for i in company_items)
            + "\n</section>"
        )

    date_str = html.escape(edition["date_display"])
    body = (
        '<div class="columns">\n' + "".join(sections) + "\n</div>"
        if sections
        else '<p class="empty">No credit-relevant items found for this edition.</p>'
    )
    return f"""
      <section class="edition">
        <div class="edition-date">{date_str}</div>
        {body}
      </section>"""


def render_html(history: list) -> str:
    """Render the main index page. Shows only the latest edition, plus a link to
    the searchable archive of everything else."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    latest_html = render_edition(history[0]) if history else '<p class="empty">No editions yet.</p>'
    has_archive = len(history) > 1

    out = template.replace("{{EDITIONS}}", latest_html)
    out = out.replace("{{GENERATED_AT}}", html.escape(generated_at))
    out = out.replace("{{ARCHIVE_LINK_DISPLAY}}", "inline-block" if has_archive else "none")
    return out


def render_archive_card(item: dict, date_display: str) -> str:
    flag = item["credit_flag"]
    label = FLAG_LABEL.get(flag, "Neutral")
    title = html.escape(item["title"])
    summary = html.escape(item["summary"])
    topic = html.escape(item["topic"])
    source = html.escape(item["source"])
    url = html.escape(item["url"])
    category = html.escape(item["category"])
    date_esc = html.escape(date_display)

    source_html = f'<a href="{url}" target="_blank" rel="noopener">{source}</a>' if url else source

    # data-search holds a lowercase blob of everything text-searchable about the item
    search_blob = html.escape(
        f"{item['title']} {item['summary']} {item['topic']} {item['source']}".lower()
    )

    return f"""
        <article class="item" data-topic="{topic}" data-flag="{flag}" data-category="{category}" data-search="{search_blob}">
          <span class="flag flag-{flag}" title="Credit signal: {label}">{label}</span>
          <div class="item-body">
            <div class="item-tag">{topic} &middot; {date_esc}</div>
            <h3 class="item-title">{title}</h3>
            <p class="item-summary">{summary}</p>
            <div class="item-source">{source_html}</div>
          </div>
        </article>"""


def compute_coverage_status(history: list, config: dict) -> list:
    """For every configured sector/company, find the most recent edition date
    it was mentioned in. history is ordered newest-first, so the first
    edition containing a topic is its most recent mention. Topics with no
    mentions at all get never=True so they can be flagged distinctly from
    merely-stale ones."""
    coverage_names = list(config.get("sectors", [])) + list(config.get("companies", []))
    last_seen = {}  # topic -> (date_str, date_display)

    for edition in history:
        for item in edition["items"]:
            topic = item.get("topic", "")
            if topic and topic not in last_seen:
                last_seen[topic] = (edition["date"], edition["date_display"])

    today = datetime.now(timezone.utc).date()
    status = []
    for name in coverage_names:
        if name in last_seen:
            date_str, date_display = last_seen[name]
            try:
                last_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                days_ago = (today - last_date).days
            except ValueError:
                days_ago = None
            status.append({
                "name": name,
                "date_display": date_display,
                "days_ago": days_ago,
                "never": False,
            })
        else:
            status.append({
                "name": name,
                "date_display": None,
                "days_ago": None,
                "never": True,
            })

    # Stalest / never-mentioned first, so gaps surface at the top.
    status.sort(key=lambda s: (not s["never"], -(s["days_ago"] if s["days_ago"] is not None else 0)))
    return status


def render_coverage_status(status: list, stale_threshold_days: int = 5) -> str:
    """Render the coverage-status panel: one row per configured sector/company
    showing last-mention recency, clickable to filter the archive by that topic."""
    rows = []
    for s in status:
        name_esc = html.escape(s["name"])
        if s["never"]:
            state_class = "coverage-never"
            detail = "no mentions yet"
        elif s["days_ago"] is not None and s["days_ago"] >= stale_threshold_days:
            state_class = "coverage-stale"
            days = s["days_ago"]
            detail = f"{days}d ago"
        else:
            state_class = "coverage-fresh"
            days = s["days_ago"]
            detail = "today" if days == 0 else f"{days}d ago"

        rows.append(
            f'<button class="coverage-item {state_class}" data-filter-topic="{name_esc}">'
            f'<span class="coverage-name">{name_esc}</span>'
            f'<span class="coverage-detail">{html.escape(detail)}</span>'
            f'</button>'
        )
    return "".join(rows)


def render_archive(history: list, config: dict) -> str:
    """Render a standalone, searchable archive page covering every edition in
    history, with client-side text search and tag/topic/flag filter chips.
    Filtering runs entirely in the browser (no server), so it works on a
    static GitHub Pages site."""
    template_path = ROOT / "scripts" / "archive_template.html"
    template = template_path.read_text(encoding="utf-8")

    all_topics = set()
    cards = []
    for edition in history:
        for item in edition["items"]:
            all_topics.add(item["topic"])
            cards.append(render_archive_card(item, edition["date_display"]))

    topic_buttons = "".join(
        f'<button class="chip" data-filter-topic="{html.escape(t)}">{html.escape(t)}</button>'
        for t in sorted(all_topics)
    )
    flag_buttons = "".join(
        f'<button class="chip chip-{flag}" data-filter-flag="{flag}">{label}</button>'
        for flag, label in FLAG_LABEL.items()
    )

    coverage_status = compute_coverage_status(history, config)
    coverage_html = render_coverage_status(coverage_status)

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    out = template.replace("{{ITEMS}}", "".join(cards))
    out = out.replace("{{TOPIC_CHIPS}}", topic_buttons)
    out = out.replace("{{FLAG_CHIPS}}", flag_buttons)
    out = out.replace("{{COVERAGE_STATUS}}", coverage_html)
    out = out.replace("{{GENERATED_AT}}", html.escape(generated_at))
    out = out.replace("{{EDITION_COUNT}}", str(len(history)))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    history = load_history()

    email_items = fetch_email_items(config)
    recent_coverage = summarize_recent_coverage(history)
    prompt = build_prompt(config, email_items, recent_coverage)

    try:
        items = call_claude(config, prompt)
    except Exception as exc:
        print(f"Error generating digest: {exc}", file=sys.stderr)
        sys.exit(1)

    items = filter_against_recent_history(items, history)

    now = datetime.now(timezone.utc)
    edition = {
        "date": now.strftime("%Y-%m-%d"),
        "date_display": now.strftime("%A, %d %B %Y"),
        "items": items,
    }

    history.insert(0, edition)
    save_history(history, config.get("history_editions_to_keep", 30))

    OUTPUT_PATH.write_text(render_html(history), encoding="utf-8")
    ARCHIVE_PATH.write_text(render_archive(history, config), encoding="utf-8")
    print(f"Digest generated: {len(items)} items. Wrote {OUTPUT_PATH} and {ARCHIVE_PATH}")


if __name__ == "__main__":
    main()
