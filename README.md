# ghst-summarizer

Daily AI-powered Telegram digests of Ghost CMS content changes, with optional
Markdown mirroring to a second Git repository for human-readable diff viewing.

Monitors all your self-hosted Ghost sites, computes diffs of published posts
against the previous day, asks DeepSeek V4-Flash for a terse summary, and
delivers one digest message per site to Telegram — only when there were actual
content changes.

Additionally: converts every post to Markdown with YAML frontmatter and pushes
to [my-ghost-sites](https://github.com/arkhivar/my-ghost-sites) so you can
browse content history and diffs in GitHub's native UI.

---

## Architecture

```
GitHub Actions (cron @ 09:00 UTC)
        |
        v
   ghst CLI -- fetches published posts (html + plaintext)
        |
        +---> ghost_digest.py
                |
                +-- state/<site>/posts.json (for tomorrow's diff)
                +-- MIRROR_DIR/<site>/<date>-<slug>.md (for GitHub viewing)
                +-- DeepSeek V4-Flash -- summarization
                +-- Telegram -- daily digest
```

**State management via Git:** Snapshots live in `state/` and are auto-committed
after each run. First run bootstraps silently — no notification spam.

**Markdown mirroring:** Posts are converted to Markdown with YAML frontmatter
(title, slug, dates, author, url) and pushed to `my-ghost-sites`. Browse
diffs at your leisure in GitHub's native diff viewer.

**No dedicated server needed.** Runs entirely on GitHub Actions free tier.

---

## Setup

### 1. Fork or clone this repo

You need your own copy to store state snapshots and run Actions.

### 2. Configure Ghost sites

Edit `config.yml`:

```yaml
sites:
  my-main-blog:
    alias: my-main-blog
    url: https://blog1.yoursite.com
    token: "staff_id:staff_secret"   # Ghost staff access token
    chat_id: -1003947505610
    thread_id: 42

  secondary-blog:
    alias: secondary-blog
    url: https://blog2.yoursite.com
    token: "staff_id:staff_secret"
    destinations:
      - chat_id: -1003947505610
        thread_id: 12
      - chat_id: -1009876543210
        channel: true
```

**Getting a Ghost staff token:** In Ghost Admin, go to your profile
(avatar → Your profile) → Scroll to "Staff Access Tokens" → Generate a new
token. The format is `{id}:{secret}` (e.g. `65a1b2c3d4e5f6:abcd1234...`).

**Alternative: keep tokens out of config.yml** by using the `GHST_SITES` secret
(see step 3).

### 3. Add repository secrets

Go to Settings → Secrets and variables → Actions → Repository secrets:

| Secret | Value | Required? |
|--------|-------|-----------|
| `DEEPSEEK_API_KEY` | From [platform.deepseek.com](https://platform.deepseek.com) | **Yes** |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) | **Yes** |
| `GHST_SITES` | JSON array overriding/extending config.yml tokens | No |
| `MIRROR_PAT` | Classic PAT with `repo` scope on `my-ghost-sites` | Only for mirroring |

The `GHST_SITES` secret format (keeps tokens out of the repo):

```json
[
  {
    "alias": "my-main-blog",
    "url": "https://blog1.yoursite.com",
    "token": "65a1b2c3d4e5f6:abcd1234...",
    "chat_id": -1003947505610,
    "thread_id": 42
  }
]
```

The `MIRROR_PAT` is only needed if you want the Markdown mirroring feature.
Create a [classic PAT](https://github.com/settings/tokens) with `repo` scope
that can write to your `my-ghost-sites` repository.

### 4. Move the workflow file to the correct location

**One manual step required.** The GitHub API prevents creating files under
`.github/workflows/` programmatically, so the workflow is at root:

```bash
mkdir -p .github/workflows
mv workflow-daily.yml .github/workflows/daily.yml
rm .github/placeholder
git add .github/workflows/daily.yml workflow-daily.yml .github/placeholder
git commit -m "ci: move workflow to correct location"
git push
```

### 5. Enable the workflow

Go to Actions → Daily Ghost Digest → Enable workflows. The cron will run
daily at 09:00 UTC. You can also trigger it manually via "Run workflow".

---

## Telegram Setup

**Groups with topics:** Enable "Topics" in your Telegram supergroup. Get
`chat_id` and `thread_id` by forwarding a message from the topic to
[@RawDataBot](https://t.me/RawDataBot).

**Channels:** Add your bot as an admin with post permission. Set
`channel: true` in the destination.

**Groups (no topics):** The bot must be a member. Use `chat_id` only.

---

## How It Works

### State bootstrap

The first time a site is processed, the script saves a snapshot of all
published posts to `state/<alias>/posts.json` and does **not** send a
notification. This avoids a flood of "all posts are new" messages on initial
setup. Markdown mirroring still happens — files are written to the mirror repo.

### Daily run

On subsequent runs, the script:

1. Fetches all published posts as **html + plaintext** via `ghst post list`
2. Compares each post's plaintext against yesterday's snapshot
3. Skips posts where only whitespace or timestamps changed
4. Sends a combined prompt (all diffs for the site) to DeepSeek V4-Flash
5. Posts a single Telegram message per site with the AI summary
6. Converts all posts to Markdown with YAML frontmatter
7. Writes Markdown files to `MIRROR_DIR/<alias>/`
8. Commits updated snapshots back to this repo
9. Commits updated mirrors to `my-ghost-sites`

### Telegram message format

```
my-main-blog · 2026-07-13

The GPU pricing guide was updated with new July data for the 5060 Ti — the ROI section now reflects current market prices. Alice also fixed a typo in the About page author bio.

[GPU Pricing Guide](https://blog1.yoursite.com/gpu-pricing-guide)
[About](https://blog1.yoursite.com/about)
```

### Markdown mirror format

Each post becomes a Markdown file with YAML frontmatter:

```markdown
---
title: "GPU Pricing Guide"
slug: gpu-pricing-guide
published_at: "2025-07-01T10:00:00.000Z"
updated_at: "2026-07-13T15:30:00.000Z"
author: "Alice"
url: "https://blog1.yoursite.com/gpu-pricing-guide/"
feature_image: "https://blog1.yoursite.com/content/images/2025/07/header.jpg"
---

# GPU Pricing Guide

Welcome to the definitive guide...
```

### What counts as a change

The script diffs the **plaintext body** of each post. This means:

- ✅ Text edits, paragraph additions/removals
- ✅ Code block changes
- ✅ Heading changes
- ✅ New or removed posts
- ❌ Feature image swaps (unless caption text changed)
- ❌ Tag-only changes (unless shown in post body)
- ❌ Publish date bumps with no text changes

Trivial diffs (< 10 chars or pure whitespace) are silently ignored.

---

## File Layout

```
ghst-summarizer/
├── .github/
│   ├── placeholder                # remove after setup (step 4)
│   └── workflows/
│       └── daily.yml              # move here from root (step 4)
├── scripts/
│   └── ghost_digest.py            # Core logic
├── config.yml                     # Site configuration
├── requirements.txt               # Python dependencies
├── state/                         # Snapshots (auto-committed)
│   ├── my-main-blog/
│   │   └── posts.json
│   └── secondary-blog/
│       └── posts.json
├── workflow-daily.yml             # at root until moved (step 4)
└── README.md                      # This file
```

---

## Dependencies

- **ghst** — Ghost's official CLI (`npm install -g @tryghost/ghst`)
- **Python 3.12+** — `requests`, `pyyaml`, `markdownify`
- **DeepSeek V4-Flash** — for summarization (cheap, non-thinking mode)
- **Telegram Bot** — for delivery

---

## License

MIT — same as tg-repo-watcher.
