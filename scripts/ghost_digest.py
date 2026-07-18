#!/usr/bin/env python3
"""
Daily Ghost digest — cron path.

Reads config.yml for Ghost sites, fetches all published posts as plaintext
AND html via the `ghst` CLI, compares plaintext against state/<site>/posts.json
(yesterday's snapshot), computes diffs for changed posts, asks DeepSeek V4-Flash
for a daily summary, and posts to the configured Telegram destination(s).

Additionally: converts posts to Markdown and writes them to MIRROR_DIR for
human-readable diff viewing in a separate Git repository.

State bootstrap: first run for a site silently saves the snapshot and does
NOT notify (avoids a burst of stale notifications on initial setup).

Reuses the Telegram and LLM stack from tg-repo-watcher.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests
import yaml

# Optional: markdownify for HTML->Markdown conversion
try:
    from markdownify import markdownify as md
    HAS_MARKDOWNIFY = True
except ImportError:
    HAS_MARKDOWNIFY = False

# ---------- config ----------
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yml"
STATE_DIR = REPO_ROOT / "state"
MIRROR_DIR = Path(os.environ.get("MIRROR_DIR", REPO_ROOT / "mirror"))

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Skip diffs that are pure whitespace or under this threshold
MIN_DIFF_CHARS = 10
# Max diff length per site (truncate to avoid huge prompts)
MAX_DIFF_PER_SITE = 8000
# Max posts to include in a single digest (safety cap)
MAX_CHANGED_POSTS = 20


# ---------- helpers ----------
def resolve_destinations(project: dict) -> list[tuple[int, int | None]]:
    """Return list of (chat_id, thread_id_or_None) tuples."""
    out: list[tuple[int, int | None]] = []

    def _add(chat_id, thread_id, is_channel=False):
        if chat_id in (None, "", 0):
            return
        if not is_channel and thread_id in (None, "", 0):
            return
        try:
            cid = int(chat_id)
            tid = int(thread_id) if thread_id not in (None, "", 0) else None
            out.append((cid, tid))
        except (TypeError, ValueError):
            print(f"[warn] bad destination {chat_id=} {thread_id=} -- skipped", file=sys.stderr)

    dests = project.get("destinations")
    if isinstance(dests, list) and dests:
        for d in dests:
            if isinstance(d, dict):
                _add(d.get("chat_id"), d.get("thread_id"), bool(d.get("channel")))
    else:
        _add(project.get("chat_id"), project.get("thread_id"), bool(project.get("channel")))
    return out


def load_state(site_alias: str) -> dict[str, dict]:
    """Return {slug: post_dict} from yesterday's snapshot, or {} if first run."""
    path = STATE_DIR / site_alias / "posts.json"
    if not path.exists():
        return {}
    try:
        posts = json.loads(path.read_text())
        return {p["slug"]: p for p in posts if p.get("slug")}
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"[warn] {site_alias}/posts.json corrupt -- starting fresh", file=sys.stderr)
        return {}


def save_state(site_alias: str, posts: list[dict]) -> None:
    dir_path = STATE_DIR / site_alias
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "posts.json").write_text(
        json.dumps(posts, indent=2, ensure_ascii=False) + "\n"
    )


def _parse_iso_date(date_str: str | None) -> str:
    """Extract YYYY-MM-DD from an ISO date string."""
    if not date_str:
        return datetime.utcnow().strftime("%Y-%m-%d")
    return date_str[:10]


def _normalize_author(author: dict | str | None) -> str:
    """Extract author name from various Ghost API shapes."""
    if isinstance(author, dict):
        return author.get("name") or author.get("slug", "unknown")
    if author:
        return str(author)
    return "unknown"


def _html_to_markdown(html: str) -> str:
    """Convert Ghost HTML to clean Markdown."""
    if not HAS_MARKDOWNIFY or not html:
        return ""
    try:
        md_text = md(html, heading_style="ATX", strip=["script", "style"])
        # Collapse excessive blank lines
        md_text = re.sub(r"\n{3,}", "\n\n", md_text)
        return md_text.strip()
    except Exception as e:
        print(f"[warn] markdownify failed: {e}", file=sys.stderr)
        return ""


def fetch_posts(site: dict) -> list[dict]:
    """Use ghst CLI to fetch all published posts with html + plaintext."""
    alias = site["alias"]
    url = site["url"]
    token = site["token"]

    # Authenticate ghst non-interactively
    auth_result = subprocess.run(
        ["ghst", "auth", "login", "--non-interactive",
         "--url", url, "--staff-token", token, "--json"],
        capture_output=True, text=True, timeout=60,
    )
    if auth_result.returncode != 0:
        print(f"[error] ghst auth failed for {alias}: {auth_result.stderr}",
              file=sys.stderr)
        return []

    # Fetch published posts with both html and plaintext in one call
    result = subprocess.run(
        ["ghst", "post", "list", "--limit", "all",
         "--filter", "status:published",
         "--formats", "html,plaintext",
         "--json",
         "--jq", (".posts[] | {slug, title, html, plaintext, updated_at, "
                  "published_at, url, primary_author, feature_image, excerpt}")],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"[error] ghst post list failed for {alias}: {result.stderr}",
              file=sys.stderr)
        return []

    posts = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            post = json.loads(line)
            post["primary_author"] = _normalize_author(post.get("primary_author"))
            posts.append(post)
        except json.JSONDecodeError:
            continue

    return posts


def is_trivial_diff(diff_text: str) -> bool:
    """Return True if the diff is just whitespace changes."""
    stripped = diff_text.strip()
    if len(stripped) < MIN_DIFF_CHARS:
        return True
    return len("".join(stripped.split())) < 5


def diff_posts(old_posts: dict[str, dict], new_posts: list[dict]) -> list[dict]:
    """Return list of changed posts with their diffs."""
    new_by_slug = {p["slug"]: p for p in new_posts if p.get("slug")}
    changes: list[dict] = []

    for slug, post in new_by_slug.items():
        if slug not in old_posts:
            changes.append({
                "slug": slug,
                "title": post.get("title", slug),
                "author": post.get("primary_author", "unknown"),
                "change_type": "new",
                "diff": f"[NEW POST]\n{post.get('plaintext', '')[:2000]}",
                "url": post.get("url", ""),
            })
            continue

        old_text = old_posts[slug].get("plaintext") or ""
        new_text = post.get("plaintext") or ""

        if old_text == new_text:
            continue

        import difflib
        diff = "\n".join(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile=f"{slug} (before)",
            tofile=f"{slug} (after)",
            lineterm="", n=3
        ))

        if is_trivial_diff(diff):
            continue

        changes.append({
            "slug": slug,
            "title": post.get("title", slug),
            "author": post.get("primary_author", "unknown"),
            "change_type": "updated",
            "diff": diff,
            "url": post.get("url", ""),
        })

    for slug, old_post in old_posts.items():
        if slug not in new_by_slug:
            changes.append({
                "slug": slug,
                "title": old_post.get("title", slug),
                "author": old_post.get("primary_author", "unknown"),
                "change_type": "deleted",
                "diff": "[POST REMOVED OR UNPUBLISHED]",
                "url": old_post.get("url", ""),
            })

    return changes[:MAX_CHANGED_POSTS]


# ---------- Markdown mirroring ----------
def mirror_posts(site_alias: str, posts: list[dict]) -> int:
    """Write posts as Markdown files to MIRROR_DIR for diff viewing.

    Returns number of files written.
    """
    site_mirror_dir = MIRROR_DIR / site_alias
    site_mirror_dir.mkdir(parents=True, exist_ok=True)

    # Build set of expected filenames to detect stale files
    expected_files: set[str] = set()
    written = 0

    for post in posts:
        slug = post.get("slug")
        if not slug:
            continue

        date_prefix = _parse_iso_date(post.get("published_at"))
        filename = f"{date_prefix}-{slug}.md"
        expected_files.add(filename)

        md_body = _html_to_markdown(post.get("html", ""))
        frontmatter_lines = [
            "---",
            f'title: "{post.get("title", slug)}"',
            f"slug: {slug}",
            f'published_at: "{post.get("published_at", "")}"',
            f'updated_at: "{post.get("updated_at", "")}"',
            f'author: "{post.get("primary_author", "unknown")}"',
        ]
        if post.get("url"):
            frontmatter_lines.append(f'url: "{post["url"]}"')
        if post.get("feature_image"):
            frontmatter_lines.append(f'feature_image: "{post["feature_image"]}"')
        frontmatter_lines.append("---")
        frontmatter_lines.append("")

        content = "\n".join(frontmatter_lines) + md_body + "\n"
        filepath = site_mirror_dir / filename
        filepath.write_text(content, encoding="utf-8")
        written += 1

    # Remove stale files (posts that were deleted or had slug changes)
    if site_mirror_dir.exists():
        for existing in site_mirror_dir.glob("*.md"):
            if existing.name not in expected_files:
                existing.unlink()
                print(f"  [mirror] removed stale file {site_alias}/{existing.name}")

    print(f"  [mirror] {written} Markdown file(s) in {site_mirror_dir}")
    return written


# ---------- LLM ----------
def build_site_prompt(alias: str, url: str, changes: list[dict]) -> str:
    """Build a single prompt for all changes on one site."""
    parts = [f"Site: {alias} ({url})", ""]
    parts.append(f"{len(changes)} post(s) changed since yesterday:")

    total_diff = 0
    for ch in changes:
        diff_text = ch["diff"]
        remaining = MAX_DIFF_PER_SITE - total_diff
        if remaining <= 0:
            parts.append("\n... [additional changes omitted] ...")
            break
        if len(diff_text) > remaining:
            diff_text = diff_text[:remaining] + "\n... [truncated] ..."

        parts.append(f"\n--- {ch['title']} ({ch['change_type']}) ---")
        parts.append(f"Author: {ch['author']}")
        parts.append(diff_text)
        total_diff += len(diff_text)

    return "\n".join(parts)


def call_deepseek(prompt: str, api_key: str) -> str:
    system_msg = (
        "You write terse daily digests of blog post changes for a site owner. "
        "Rules: 2-4 short sentences total for the entire digest, no emoji, no headers, "
        "no bullet lists, no marketing tone. Focus on WHAT changed and WHY it matters. "
        "Mention the author name(s) briefly. If a change is trivial, say so briefly. "
        "Group related changes together. End with a short sign-off."
    )
    r = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0.3,
            "max_tokens": 400,
        },
        timeout=90,
    )
    if r.status_code >= 400:
        print(f"[error] DeepSeek {r.status_code}: {r.text[:500]}", file=sys.stderr)
        return "(summary unavailable -- LLM error)"
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip() \
        or "(empty summary)"


# ---------- Telegram ----------
def tg_escape(s: str) -> str:
    if not s:
        return ""
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, f"\\{ch}")
    return s


def send_telegram(token: str, chat_id: int, thread_id: int | None,
                  text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    r = requests.post(TELEGRAM_API.format(token=token), json=payload, timeout=30)
    if r.status_code >= 400:
        print(f"[error] Telegram {r.status_code}: {r.text[:500]}", file=sys.stderr)
        payload.pop("parse_mode", None)
        payload["text"] = text.replace("\\", "")
        r2 = requests.post(TELEGRAM_API.format(token=token), json=payload, timeout=30)
        print(f"[fallback] plain-text retry -> {r2.status_code}")
    else:
        print(f"[ok] Telegram delivered ({r.status_code})")


# ---------- main ----------
def process_site(site: dict, tokens: dict) -> bool:
    """Fetch, diff, summarize, notify, mirror for one site.

    Return True if content changes were found and notified.
    """
    alias = site["alias"]
    url = site.get("url", alias)

    print(f"\n[site] {alias}")

    old_posts = load_state(alias)
    print(f"  [state] {len(old_posts)} post(s) in previous snapshot")

    new_posts = fetch_posts(site)
    if not new_posts:
        print(f"  [skip] no posts fetched for {alias}")
        return False
    print(f"  [fetch] {len(new_posts)} published post(s)")

    # Always mirror current posts to Markdown (independent of notification)
    mirror_posts(alias, new_posts)

    if not old_posts:
        # Bootstrap: save silently, no notification
        save_state(alias, new_posts)
        print(f"  [bootstrap] snapshot saved, no notification")
        return False

    changes = diff_posts(old_posts, new_posts)
    if not changes:
        print(f"  [info] no content changes")
        save_state(alias, new_posts)
        return False

    print(f"  [changes] {len(changes)} post(s) with content changes")

    prompt = build_site_prompt(alias, url, changes)
    summary = call_deepseek(prompt, tokens["ds"])
    print(f"  [llm] {summary[:200]}")

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    header = f"*{tg_escape(alias)}* · {date_str}"
    body = tg_escape(summary)
    links = ""
    for ch in changes:
        if ch.get("url"):
            links += f"\n\[{tg_escape(ch['title'][:40])}\]({ch['url']})"

    msg = f"{header}\n\n{body}" + (links if links else "")

    destinations = resolve_destinations(site)
    for chat_id, thread_id in destinations:
        send_telegram(tokens["tg"], chat_id, thread_id, msg)

    # Save new snapshot for tomorrow
    save_state(alias, new_posts)
    return True


def main() -> int:
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    if not ds_key or not tg_token:
        print("[fatal] DEEPSEEK_API_KEY and TELEGRAM_BOT_TOKEN required",
              file=sys.stderr)
        return 1

    if not CONFIG_PATH.exists():
        print(f"[fatal] config.yml not found at {CONFIG_PATH}", file=sys.stderr)
        return 1

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    sites = cfg.get("sites") or []
    if not sites:
        print("[info] no sites configured in config.yml")
        return 0

    # Sites can also come from GHST_SITES env (JSON array)
    env_sites = os.environ.get("GHST_SITES", "")
    if env_sites:
        try:
            env_sites_list = json.loads(env_sites)
            if isinstance(env_sites_list, list):
                config_aliases = {s["alias"]: i for i, s in enumerate(sites)}
                for es in env_sites_list:
                    alias = es.get("alias")
                    if alias and alias in config_aliases:
                        sites[config_aliases[alias]] = es
                    else:
                        sites.append(es)
        except json.JSONDecodeError:
            print("[warn] GHST_SITES env is not valid JSON, ignoring",
                  file=sys.stderr)

    if not HAS_MARKDOWNIFY and MIRROR_DIR != REPO_ROOT / "mirror":
        print("[warn] markdownify not installed but MIRROR_DIR is set; "
              "mirroring will be skipped. pip install markdownify",
              file=sys.stderr)

    tokens = {"ds": ds_key, "tg": tg_token}
    total_changed = 0

    for site in sites:
        if not site.get("alias") or not site.get("url") or not site.get("token"):
            alias = site.get("alias", "?")
            print(f"[skip] site '{alias}' missing url or token", file=sys.stderr)
            continue
        try:
            changed = process_site(site, tokens)
            if changed:
                total_changed += 1
        except Exception as e:
            print(f"[error] {site.get('alias', '?')}: {type(e).__name__}: {e}",
                  file=sys.stderr)

    print(f"\n[done] {total_changed} site(s) with changes notified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
