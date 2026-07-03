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

def build_prompt(config: dict, email_items: list) -> str:
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
4. Deduplicate — do not report the same underlying event twice.

INBOX CONTENT
{email_block}

OUTPUT FORMAT
Respond with ONLY a JSON array (no markdown fences, no preamble, no commentary) of
objects, each with exactly these fields:
  "category": "industry" or "company"
  "topic": the sector name (for industry items) or company name (for company items) —
           must match one of the coverage names above as closely as possible
  "title": a short headline, in your own words, under 100 characters
  "summary": 2-3 sentences in your own words explaining the news and why it matters
             from a credit perspective. Never quote source text verbatim.
  "credit_flag": one of "positive", "negative", "watch", "neutral" — your assessment of
                 the likely direction of credit impact
  "source": either the publication name (e.g. "Reuters") or, for inbox items, the sender
  "url": the source URL if from the web, or "" if from an inbox email

Return between 6 and 16 items total, covering as much of the sector/company list as
genuine news allows. If there is truly nothing credit-relevant for a sector or company,
simply omit it rather than inventing something."""


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
        "model": config.get("model", "claude-sonnet-4-6"),
        "max_tokens": 4000,
        "messages": messages,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }

    collected_text = []

    # Loop to let the model use web_search across multiple turns if it wants to,
    # stopping once it returns a normal (non tool-use) end turn.
    for _ in range(6):
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


def parse_json_items(text: str) -> list:
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
    return valid_items


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


def render_archive(history: list) -> str:
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

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    out = template.replace("{{ITEMS}}", "".join(cards))
    out = out.replace("{{TOPIC_CHIPS}}", topic_buttons)
    out = out.replace("{{FLAG_CHIPS}}", flag_buttons)
    out = out.replace("{{GENERATED_AT}}", html.escape(generated_at))
    out = out.replace("{{EDITION_COUNT}}", str(len(history)))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    email_items = fetch_email_items(config)
    prompt = build_prompt(config, email_items)

    try:
        items = call_claude(config, prompt)
    except Exception as exc:
        print(f"Error generating digest: {exc}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    edition = {
        "date": now.strftime("%Y-%m-%d"),
        "date_display": now.strftime("%A, %d %B %Y"),
        "items": items,
    }

    history = load_history()
    history.insert(0, edition)
    save_history(history, config.get("history_editions_to_keep", 30))

    OUTPUT_PATH.write_text(render_html(history), encoding="utf-8")
    ARCHIVE_PATH.write_text(render_archive(history), encoding="utf-8")
    print(f"Digest generated: {len(items)} items. Wrote {OUTPUT_PATH} and {ARCHIVE_PATH}")


if __name__ == "__main__":
    main()
