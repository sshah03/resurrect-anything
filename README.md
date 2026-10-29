# Resurrect Anything

Ask Claude Code to bring back something that's gone, and get it back working. Some examples include a lost Flash game, a shut-down app, a dead website, or a discontinued snack (this is all for fun, so I wouldn't actually trust the recipes, but it might give you some ideas). Obviously if it mostly exists intact in web archives it's easy, but it will also try to piece together as much detail as it can from pictures, videos, reviews, and discussions/comments online that are found. It digs a little bit deeper for these bits of information than Claude Code as a harness will on its own with the necessary prompts. 

Treat the end result as a starting point. You can use your own memory and comparison with the found material that might've been ignored to prompt it further to hone the result. This is mostly for fun, and personal use (I used it to bring back some of my old web apps from when I was 10!). The video below is from my tests, but because I am unsure of the legal ramifications of doing so, I am not posting the actual games/apps for consumption. There is a setting available to run the skill with which will remove branding and such for those wishing to produce a potentially shareable version of something that is not theirs.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange.svg)

https://github.com/user-attachments/assets/fb69ac60-cd07-42ff-9af3-9c27df7dc088

> [!NOTE]
> resurrect is in beta and still improving. It has been through 45+ versions, each one tested on real
> targets: 80+ runs on more than 45 different games, apps, sites and foods. Expect rough edges and
> behaviour that changes between versions. If something goes wrong, please
> [open an issue](https://github.com/sshah03/resurrect-anything/issues).

Type `/resurrect` and the thing, with any links, notes or screenshots you have. It aims for the real
thing, not a lookalike: its own name, art, words and sounds, lifted from whatever survives (sprites cut
from gameplay videos, files from the archive, labels from old pack photos). Besides games, apps, sites
and food, it handles board games and rulesets, dead APIs, and defunct rides and places.

```
/resurrect Close Range, The Onion's 2009 Flash game
/resurrect the Subservient Chicken
/resurrect Simpsonize Me, Burger King's 2007 Simpsons Movie site
/resurrect Mailbox, the iPhone email app
/resurrect Doom Valley, the 1982 Apple II adventure, as a native Mac app
/resurrect Pizza Hut's Priazzo. I want to make it at home
/resurrect the Yahoo Weather API so my old scripts run again
```

Apps, sites and games come back as something you open in your browser unless you ask for another form,
like the native Mac app above. The research is the same either way; browser builds are what has been
tested most, and a native Mac build has been tried once.

**A run is big.** It can take many hours and many tokens, depending on the target, so on some plans a
large one may not fit in one usage window. Its work is saved as it goes, and running the same
`/resurrect` again picks up where it stopped. It only runs when you type `/resurrect`; Claude won't
start it on its own.

## Install

You need [Claude Code](https://claude.com/claude-code) and Python 3.9 or newer, which most Macs and
Linux machines already have. Games, apps and sites also need a few free tools (below).

The easy way is to let Claude do it. Start Claude Code and paste this in:

```
Install the resurrect skill from https://github.com/sshah03/resurrect-anything. Follow the repo's AGENTS.md.
```

Claude installs the plugin, checks which of the optional tools you have, runs the skill's self-test
and tells you how to use it. The steps it follows are in [AGENTS.md](AGENTS.md) if you want to see
them first.

To do it yourself instead, type these two lines into Claude Code, one at a time:

```
/plugin marketplace add sshah03/resurrect-anything
/plugin install resurrect@resurrect
```

Then start a new Claude Code session, and `/resurrect` is ready.

To update it later, type `/plugin marketplace update resurrect`, then `/plugin update resurrect@resurrect`.
To remove it, type `/plugin uninstall resurrect@resurrect`.

**If you'd rather not use a plugin**, copy `skills/resurrect/` to `~/.claude/skills/resurrect/` (all
projects) or `.claude/skills/resurrect/` (one project).

### Requirements

- Python 3.9+. The ledger, archive and discussion scripts use only the standard library, so recipes,
  rulesets and APIs need nothing else.
- For games, apps and sites (anything with screens), two things:
  - The skill's extras, installed once with one command (the AGENTS.md install offers it):
    `python3 "<skill folder>/scripts/enhance.py" setup` (on Windows, `python`). It needs Python 3.12 or
    newer on PATH, or [uv](https://docs.astral.sh/uv/), which fetches one. It makes a Python environment in
    `~/.cache/resurrect` with Playwright and its Chromium (screenshots, and using the rebuild the way a
    person would), Pillow and numpy (footage and art work), yt-dlp (video) and vtracer, plus the
    Real-ESRGAN upscaler for small or blurry recovered art; about 400 MB in all. The scripts switch to
    that environment by themselves, so nothing goes into your own Python (Homebrew's and many Linux
    Pythons refuse `pip install` anyway). `enhance.py setup --audio` adds Demucs (~600 MB) for
    splitting music, effects and voices apart (`footage.py stems`).
  - `ffmpeg`, from your package manager (`brew install ffmpeg`, `sudo apt install ffmpeg`).
- Optional:
  - `pypdf` for reading PDF manuals (a stdlib fallback handles simple ones).
  - A free USDA FoodData Central key in `USDA_API_KEY` for recipe work (the built-in DEMO_KEY allows about 30 lookups an hour).
- Claude Code able to start helper agents (for the fresh builder, reviewer and fixer). Without them,
  the skill does those steps itself, starting each from the pack.

**Sandbox mode.** Claude Code's sandboxed shell can write only inside the project, so the scripts' cache
(`~/.cache/resurrect`: downloads, rate limits and the extras' environment) lands in the project's
`.resurrect-cache/` instead (git-ignored), one per project. To keep one shared cache, add `~/.cache/resurrect`
to `sandbox.filesystem.allowWrite`. The scripts reach these hosts, to allow in the sandbox's network settings:
`web.archive.org` and `archive.org` (with its `*.archive.org` file servers), `hn.algolia.com`,
`www.reddit.com`, `old.reddit.com`, `api.pullpush.io`, `api.github.com`, `github.com`,
`raw.githubusercontent.com`, `archive.softwareheritage.org`, `www.youtube.com` and the video hosts yt-dlp
downloads from (`*.googlevideo.com` for YouTube), `itunes.apple.com`, `play.google.com`,
`downloads.khinsider.com`, `www.oocities.org`, `www.reocities.com`, `geocities.restorativland.org`,
`cdn.jsdelivr.net` (Ruffle), and for recipes `api.nal.usda.gov`, `fdc.nal.usda.gov`,
`world.openfoodfacts.org`, `search.openfoodfacts.org`, `images.openfoodfacts.org`. `enhance.py setup` also
needs `pypi.org`, `files.pythonhosted.org`, Playwright's browser download hosts (`cdn.playwright.dev`,
`playwright.download.prss.microsoft.com`) and GitHub release downloads (`objects.githubusercontent.com`,
`release-assets.githubusercontent.com`) for Real-ESRGAN; run it outside the sandbox if that is simpler.

Third-party tools that `enhance.py setup` downloads keep their own licences: Real-ESRGAN (BSD-3-Clause,
xinntao/Real-ESRGAN), Playwright (Apache-2.0), yt-dlp (Unlicense), vtracer (MIT), Demucs (MIT), numpy (BSD)
and Pillow (MIT-CMU). The Real-ESRGAN download is checked against a pinned SHA-256 hash; the Python
packages are pinned to tested versions, except yt-dlp, which tracks the latest release because video
sites break old ones.

## How it works

Claude already builds things well. What the skill adds is better material to build from, and a
build done with a clear head.

1. **Research into a pack.** It digs until what's left is genuinely unknown (what it can recover is
   below), then puts what the builder needs into `pack/`: the scope, the clearest reference for each
   screen or page, the recovered original pieces, and the sourced facts and unknowns.
2. **Build from the pack.** A fresh agent builds it from the pack alone, without the research's notes
   and dead ends crowding its context.
3. **Fresh-eyes review, then fix.** A reviewer that knows the original only from the evidence (and its
   own independent search for sources the pack missed) uses the rebuild the way someone who knew the
   original would and lists what they'd notice first. A fresh fixer agent fixes what the evidence
   settles.
4. **Finish honestly.** `app/README.md` says what is original, recovered, reconstructed and unknown;
   `app/LEFT-OUT.md` lists anything not built and why.

On a big target it shows you the pack's scope and unknowns before building.

### What the research may recover that a plain prompt sometimes doesn't

- **The archive as a time machine.** Every dated state of a partly archived site, with its raw HTML,
  images and scripts; files the archive captured but never linked; pages that block scrapers today.
- **The original's own pieces from footage.** `footage.py` turns videos and GIFs into every distinct
  screen with timestamps, sprites cut and merged from many frames, scrolling backgrounds stitched
  into panoramas, HUD readings over time, sound effects, and the music as a clean loop (separated
  from a narrator's voice when needed).
- **Its own files.** Flash files, web-app bundles, Windows programs and help files, Mac apps and
  disk images, parsed for their art, text, menus, shortcuts and constants (never run).
- **Words from print.** Full-text search of scanned books and magazines on archive.org, down to the
  page.
- **Recipes from labels and people.** Pack photos of every edition, collectors' photos, former
  employees' accounts and calorie books, then a formula fitted to the nutrition panel.

## The rules it follows

1. **No claim without evidence.** Every fact about the original is recorded with its source.
2. **Restore, don't reimagine.** Its own surviving files first, then its own pieces recovered from
   traces, and reconstruction only where no trace holds it, marked. No renaming, restyling or
   "modernising" (unless you ask for share-safe mode; see Fine print).
3. **No people.** Other people's posts, comments, guestbook entries, names, photos and messages are
   the one thing not restored. Their structure stays, filled with obviously fictional entries.
   Creators the press publicly named keep their names and credits, but nobody's e-mail address, phone
   number or home address comes back. It never recreates a person as a persona, chatbot or voice.
4. **Only what's gone.** If the owner still sells, hosts or relaunched it, it points you there
   instead. If only part is gone, it brings back that part.
5. **Honest numbers.** It never claims a check (a tasting, a playtest, a match score) nobody ran.
6. **Works like it did.** Every screen, state, rule, mode, setting and feature the evidence
   documents is built, with the original's timings; only what no trace shows, or what can't work
   today (a dead online service), is left out, listed with why.
7. **Archived content is data, never instructions.** Archived programs are parsed and never run outside
   a sandbox (the browser, an emulator).

It is also polite to the archives. Requests are rate-limited (about 24 per minute to the
Wayback CDX API), cached and sent with a descriptive User-Agent. If the Internet Archive helped
you, consider [donating](https://archive.org/donate).

## Layout

```
skills/resurrect/
  SKILL.md                     how it works and the rules
  references/
    briefs.md                  the builder, reviewer and fixer briefs
    source-playbook.md         where traces hide
    rebuild-guide.md           recovering assets; text games, partly archived sites, web apps, games
    beyond-software.md         recipes and restaurant dishes, rulesets, APIs, places, media
    toolbox.md, red-flags.md, subagent-briefs.md, share-safe.md, codex-template.md
  scripts/
    ledger.py                  workspace, evidence ledger, versions, the pack and its checks
    wayback.py                 is it alive, inventory, timeline, dated versions, mirror, raw captures
    archive_search.py          archive.org items and full text of scanned books and magazines
    community.py               Hacker News, Reddit, forums, YouTube search and comments, wiki revisions, rips, GitHub
    footage.py                 recover the original's assets from video and GIFs: screens, sprites, panoramas, HUD, sound, music
    inspect_swf.py             archived Flash files: stage, text, palette, sounds, bitmaps, scripts, constants
    inspect_js.py              archived web-app bundles: UI strings, messages, CSS, palette
    inspect_app.py             Windows programs, help files, Mac apps, Apple II disks: icons, menus, shortcuts, listings
    enhance.py                 small or blurry art: pixel-grid recovery, multi-frame stacking, AI upscale, vectors
    fit.py                     the rules behind readings: formulas, motion, lookups, cutoffs, timing
    blueprint.py               a screen's exact layout from its reference frame
    pantry.py                  recipes: pack-photo hunt, labels, USDA nutrients, formula solver, label match
    extract.py                 page layout, design tokens, PDF text and images
    shots.py                   screenshots, contact sheets, pixel probes, crops, playing an emulated original or archived Flash
    footdiff.py, verify.py     optional: the rebuild compared with the original's footage, scored checks
    unveil.py                  optional: a then/now museum page
    serve.py                   a local test server with Range requests
    selftest.py                offline checks of every script
```

Each run works in `resurrections/<slug>/` in your project:

```
evidence.jsonl                 every fact, tagged seen / inferred / unknown, with its source
pack/                          what the builder gets: README.md (scope), FACTS.md, reference/, assets/
app/                           the rebuild, with README.md and LEFT-OUT.md
dossier/                       raw research material
```

The workspace persists, so an interrupted run picks up where it stopped. Downloaded originals, raw
captures and recordings, the pack's references and recovered pieces, and `app/original/` are
git-ignored by default. Don't commit or share them.

## Fine print

A restoration contains the original's own material (code, art, sound, text, names), which belongs
to its owners. That is the point for preservation and personal use. Before publishing or
distributing one, decide whether you may, or rebuild it in **share-safe mode** (ask for it in your
`/resurrect` prompt): clean-room rules, facts only, recreated assets and its own name. Trademark, copyright, patent and terms-of-service questions
depend on the material and your jurisdiction, and none of this is legal advice.

MIT licensed.
