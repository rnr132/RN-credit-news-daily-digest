#!/usr/bin/env python3
"""
Watchlist Article Ideas Generator
Synthesizes weekly credit news into 3 article/commentary ideas
Scores each on media attention potential for credit markets
"""

import json
import os
from datetime import datetime, timedelta
import anthropic

# Initialize Anthropic client
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Media attention scoring rubric
ATTENTION_FACTORS = {
    "breaking_novelty": {
        "weight": 0.20,
        "description": "First-of-type development, breaking event, or unexpected announcement"
    },
    "sector_momentum": {
        "weight": 0.15,
        "description": "Sector actively in market focus, high deal/refinancing activity, recent volatility"
    },
    "investor_relevance": {
        "weight": 0.20,
        "description": "Direct relevance to credit investors, affects widely-held issuers, systemic risk angle"
    },
    "market_debate": {
        "weight": 0.15,
        "description": "Point of disagreement among analysts, market uncertainty, contrarian angle"
    },
    "regulatory_macro": {
        "weight": 0.15,
        "description": "Tied to pending policy, rate move, ESG pressure, macro headwind"
    },
    "structural_precedent": {
        "weight": 0.15,
        "description": "Unique deal structure, new creditor outcome, methodology-relevant event"
    }
}

def generate_article_ideas(watchlist_data: str) -> dict:
    """
    Use Claude to synthesize weekly watchlist into 3 article ideas.
    watchlist_data: string containing the week's worth of watchlist digest
    """
    
    prompt = f"""You are a credit markets research strategist helping a senior analyst generate high-impact article ideas.

Based on the following week of credit news from the Watchlist digest, generate exactly 3 article/commentary ideas suitable for either:
- In-depth sector notes (4+ pages)
- Short-form market commentaries (concise, 1-2 page analytical pieces)

For each idea, provide:
1. **Title** (clear, market-facing, 8-12 words)
2. **Format** (sector note OR short commentary)
3. **Core thesis** (2-3 sentences: what's the argument? why now?)
4. **Key angles** (3-4 bullet points of analytical depth)
5. **Target audience** (e.g., "credit investors in [sector]", "rating committee", "intermediaries with [issuer] exposure")
6. **Timeliness window** (when should this run: this week, next 2 weeks, upcoming earnings/event)

Prioritize:
- Actionable novelty (not retroactive commentary)
- Gaps in current market narrative (what's under-discussed?)
- Your sector expertise (Telecoms, Oil & Gas, Metals & Mining, Energy, Real Estate across EMEA)
- Investor-facing clarity (concise, judgment-driven, decisive)

Output in JSON format only, with structure:
{{
  "ideas": [
    {{
      "title": "...",
      "format": "...",
      "thesis": "...",
      "angles": [...],
      "audience": "...",
      "timing": "..."
    }},
    ...
  ]
}}

---
Weekly Watchlist Digest:
{watchlist_data}
"""
    
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    # Parse the response
    try:
        result_text = response.content[0].text
        # Extract JSON from response (handle potential markdown wrapping)
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]
        
        ideas = json.loads(result_text.strip())
        return ideas
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Error parsing Claude response: {e}")
        return {"ideas": [], "error": str(e)}


def score_media_attention(idea: dict) -> dict:
    """
    Score an article idea on media attention potential using Claude.
    Returns scores for each factor plus overall rating.
    """
    
    factors_json = json.dumps(ATTENTION_FACTORS, indent=2)
    
    prompt = f"""You are a credit market communications strategist assessing the media/market attention potential of a proposed research article.

Evaluate this article idea on the following factors (each 1-10 scale):

{factors_json}

Article idea:
Title: {idea.get('title', 'N/A')}
Thesis: {idea.get('thesis', 'N/A')}
Target audience: {idea.get('audience', 'N/A')}

For each factor, provide:
- Score (1-10)
- Rationale (1-2 sentences)

Then provide:
- Overall attention score (1-10, weighted average)
- Key strength (why this will get traction)
- Key risk (what could limit pickup)

Output in JSON format:
{{
  "scores": {{
    "breaking_novelty": {{"score": X, "rationale": "..."}},
    "sector_momentum": {{"score": X, "rationale": "..."}},
    ...
  }},
  "overall_score": X,
  "weighted_overall": X.X,
  "strength": "...",
  "risk": "..."
}}
"""
    
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    try:
        result_text = response.content[0].text
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]
        
        scores = json.loads(result_text.strip())
        return scores
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Error scoring idea: {e}")
        return {"error": str(e)}


def rank_ideas(scored_ideas: list) -> list:
    """
    Rank ideas by weighted attention score.
    """
    ranked = sorted(
        scored_ideas,
        key=lambda x: x.get("scores", {}).get("weighted_overall", 0),
        reverse=True
    )
    return ranked


def format_output(ranked_ideas: list, week_start: str) -> str:
    """
    Format the final output as markdown for GitHub Pages or direct viewing.
    """
    
    output = f"""# Watchlist Article Ideas — Week of {week_start}

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

---

"""
    
    for idx, idea in enumerate(ranked_ideas, 1):
        overall = idea.get("scores", {}).get("weighted_overall", 0)
        
        output += f"""## Idea {idx}: {idea['idea'].get('title', 'N/A')}

**Format:** {idea['idea'].get('format', 'N/A')}  
**Attention Score:** {overall:.1f}/10  
**Target:** {idea['idea'].get('audience', 'N/A')}  
**Timing:** {idea['idea'].get('timing', 'N/A')}

### Thesis
{idea['idea'].get('thesis', 'N/A')}

### Key Analytical Angles
"""
        for angle in idea['idea'].get('angles', []):
            output += f"- {angle}\n"
        
        output += f"""
### Media Attention Breakdown
"""
        for factor, data in idea.get('scores', {}).get('scores', {}).items():
            score = data.get('score', 'N/A')
            rationale = data.get('rationale', '')
            factor_display = factor.replace('_', ' ').title()
            output += f"- **{factor_display}**: {score}/10 — {rationale}\n"
        
        output += f"""
**Key Strength:** {idea.get('scores', {}).get('strength', 'N/A')}  
**Key Risk:** {idea.get('scores', {}).get('risk', 'N/A')}

---

"""
    
    return output


def load_week_from_archive(archive_dir: str, days: int = 7) -> str:
    """
    Read the last N days of daily digest files from the archive directory.
    Assumes daily digests are stored as dated files (e.g. YYYY-MM-DD.md or .json)
    in archive_dir. Adjust the glob pattern below to match your actual
    Watchlist archive naming convention.
    """
    import glob

    today = datetime.now()
    combined = []

    for i in range(days):
        day = today - timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        # Try common extensions/patterns used by static digest archives
        matches = glob.glob(os.path.join(archive_dir, f"{date_str}*"))
        for match in matches:
            try:
                with open(match, "r", encoding="utf-8") as f:
                    combined.append(f"### {date_str}\n{f.read()}")
            except (OSError, UnicodeDecodeError) as e:
                print(f"[!] Skipped {match}: {e}")

    return "\n\n".join(combined)


def main():
    """
    Main workflow: read watchlist data, generate ideas, score them, output.
    """

    # Preferred path: read directly from the Watchlist archive directory.
    # Fallback: env var, useful for local testing or manual override.
    archive_dir = os.environ.get("WATCHLIST_ARCHIVE_DIR", "")
    watchlist_data = ""

    if archive_dir and os.path.isdir(archive_dir):
        watchlist_data = load_week_from_archive(archive_dir)
    else:
        watchlist_data = os.environ.get("WATCHLIST_WEEK_DATA", "")

    if not watchlist_data:
        print("Error: No watchlist data found. Set WATCHLIST_ARCHIVE_DIR to your "
              "archive folder, or WATCHLIST_WEEK_DATA for a manual override.")
        return
    
    print("[*] Generating 3 article ideas from weekly watchlist...")
    ideas_result = generate_article_ideas(watchlist_data)
    
    if "error" in ideas_result:
        print(f"Error generating ideas: {ideas_result['error']}")
        return
    
    ideas = ideas_result.get("ideas", [])
    print(f"[+] Generated {len(ideas)} ideas")
    
    # Score each idea
    print("[*] Scoring media attention potential...")
    scored_ideas = []
    for idea in ideas:
        scores = score_media_attention(idea)
        scored_ideas.append({"idea": idea, "scores": scores})
        print(f"  - {idea.get('title', 'N/A')}: {scores.get('weighted_overall', 'N/A')}/10")
    
    # Rank by attention score
    ranked = rank_ideas(scored_ideas)
    
    # Format output
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    output = format_output(ranked, week_start)
    
    # Write to file (defaults to current dir; set OUTPUT_DIR to write
    # straight into your GitHub Pages source folder, e.g. "docs/ideas")
    output_dir = os.environ.get("OUTPUT_DIR", ".")
    os.makedirs(output_dir, exist_ok=True)

    output_file = os.path.join(output_dir, f"article_ideas_{week_start}.md")
    with open(output_file, "w") as f:
        f.write(output)

    print(f"\n[+] Output written to {output_file}")
    print("\n" + output)

    # Also return JSON for programmatic use
    output_json = os.path.join(output_dir, f"article_ideas_{week_start}.json")
    with open(output_json, "w") as f:
        json.dump({"week": week_start, "ideas": ranked}, f, indent=2)

    print(f"[+] JSON output written to {output_json}")


if __name__ == "__main__":
    main()
