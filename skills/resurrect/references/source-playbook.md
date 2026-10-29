# Source playbook

*Advice from past runs, not a procedure: use what helps with this target, in whatever order your own judgement says.*

Contents: 1. Source families · 2. By product type · 3. Websites from the Wayback Machine ·
4. Scarce mode · 5. Query craft and the second hop · 6. What not to collect · 7. Stopping

`$S/<script>` below means `python3 "<absolute path of this skill>/scripts/<script>"`, quoted (set
`S="${CLAUDE_SKILL_DIR}/scripts"`). Run from the project root; scripts find the workspace.

## 0. Two things every dig does

**Discourse, in one pass.** `community.py discuss "<name>" --aka "<other names>" --maker "<studio or channel>"
--type <type> --from <launch> --to <death>` gathers Hacker News and Reddit, and comments on the videos that
name it (namesakes from other makers drop out; `--videos ID,ID` picks them by hand), all dated and most
descriptive first, into `dossier/private/discuss-<name>.jsonl`, and prints a search plan for the forums,
newsgroups, blogs and reviews a script cannot query (a retro plan for targets before 1995). A discourse reader (subagent-briefs.md) turns it
into behaviour claims with dates and versions. People describe settings, gestures, limits and changes that
no screenshot shows; three independent places agreeing is strong evidence.

**Versions.** Things change. Give every descriptive item an `as_of` (when it looked or worked like that) and,
where known, a `version`. Build the timeline from the thing's own record: App Store / Play listings, release
notes, changelogs and help pages over time (`wayback.py changes <url>`: one row per distinct capture with
version strings and What's New), dated captures (`wayback.py timeline`), dated reviews, and video upload
dates (a video is as_of no later than its upload; read version strings and copyright years off its title
screens). `ledger.py versions propose` suggests iterations from the ledger; `versions add` defines them and
`versions target` names the one to rebuild (default: the last full version before it died, unless the user
prefers the era people miss). `check-spec` then flags Codex lines that cite another version without saying so.

**Lost-media search kit** (round 2, when the obvious searches are spent):
- **Filmot** (filmot.com) searches YouTube transcripts: find videos where people *say* the name or its UI
  strings, including videos whose titles never mention it. A deleted video's id can still be found in
  Wayback captures of its watch page and in Filmot's index.
- **Discmaster** (discmaster.textfiles.com) searches file names inside the CD and disk images on
  archive.org: installers, demos, manuals and assets of software that shipped on disc.
- **Open directories:** `"index of" "<file or folder name>"` finds old servers still listing files.
- **Guess URL patterns in Wayback:** once one asset URL is known (`/games/foo/foo.swf`), try its siblings
  with `wayback.py cdx <pattern> --match prefix`.
- **Other engines:** Yandex and Bing index pages Google dropped; try both for exact phrases.
- **Echoes are not sources:** ten posts repeating one wiki page are one source. Trace each claim to its
  first appearance; only independent witnesses corroborate.

## 1. Source families

| Family | What it gives you | Tools |
|---|---|---|
| `archive` | Archived pages, styles, help docs, changelogs, feeds, store listings, death notices | `$S/wayback.py`, `$S/archive_search.py` |
| `community` | What people did with it, loved, missed; tips, shortcuts, quirks, workflows | `$S/community.py hn/reddit`, WebSearch for forums, fan wikis, Discords' public mirrors |
| `media` | Moving pictures and the original's own art and sound inside them: trailers, let's plays, speedruns, reviews, tutorials, ads, keynotes, GIFs; sprite and sound rips | `$S/community.py yt` (transcripts, `--meta-only`), `$S/footage.py` (`scenes`, `sprite`, `sfx`, `loop`, `gif`), `$S/community.py frames` (stills and measuring bursts), `$S/community.py media` (fan-wiki files), WebSearch for images, GIFs, The Spriters Resource / The Sounds Resource, magazine scans on archive.org |
| `docs` | Manuals, help centres, FAQs, press kits, API docs, patents describing mechanics | Wayback captures of /help /faq /support, `$S/archive_search.py search ... --mediatype texts` |
| `code` | Surviving originals (SWF, JS bundles, ROMs, disk images): the restoration's raw material; disassemblies; things released openly; old SDK docs. An archive.org collection name (e.g. `open_source_software`) is not a licence: check the item's actual licence | `$S/wayback.py fetch --original` + `$S/inspect_swf.py` (parsed, never run; share-safe: dirty room only), WebSearch "github <name> open source"; record licences as `legal` evidence |
| `web` | News coverage, obituaries ("X is shutting down"), retrospectives, blog posts, the Wikipedia/fan-wiki article as it read while alive | WebSearch, WebFetch, `$S/community.py wiki "<title>" --as-of <date>` (`--site <x>.fandom.com` for fan wikis) |
| `user` | The person asking: their screenshots and recollections | Ask once, in the intake summary, if evidence is scarce |

## 2. By product type

**website**: Wayback is the spine (section 3). Add `web` for history and `community` for what
regulars remember about navigation and quirks. Questions skew to layout, templates, nav,
palette, typography, widgets, and which content was the site's own editorial voice.

**webapp**: Wayback for marketing pages, help centre, pricing, changelog and blog (they
describe features precisely and are usually crawled even when the app behind login was not).
`media` for tutorials that show real logged-in screens. `community` for workflows and the
"I still miss..." threads. `wayback.py api <host>` sometimes finds archived public feeds or
JSON from the era; read them as examples of the data shape.

**app** (desktop or mobile): `community.py store "<name>"` first (is it still listed? its
screenshots and release notes are evidence either way), then store listings via Wayback (Play Store and App Store pages carry
screenshots, feature bullets and changelogs; try `wayback.py urls play.google.com --prefix
/store/apps/details?id=<package>` or WebSearch for the store URL), the developer's site and
help pages, magazine reviews on archive.org (`--mediatype texts`), video reviews and tutorials.
Manuals are gold: they list every menu item and setting. **Disc images on archive.org carry the
original manual** (a PDF guide, readmes, help files): `archive_search.py inside <id> <file.iso>`
lists them without downloading the image, and `archive_search.py download <id> '<file.iso>/<path>'`
fetches just that document. The disc image itself is the original program: run it in an in-browser
emulator (`archive_search.py download <id> <file.iso> --original`) rather than reconstructing it. Many
mobile apps also had a **web client or viewer**. Look for its JS (`wayback.py find <domain> "\.js" --to
<death-date>`): it is original code to restore and holds endpoints, labels and feature names.

**Portals that served games under hashed names** (`<cdn>/mediaFiles/<hash>.swf`): the
archived game-list page names the file; take the URL from there and run `wayback.py cdx <url> --all`
(every status) to see whether the program itself was ever captured.

**Old magazines and books are searchable by their text**: `archive_search.py fulltext '"<name>"'` searches
the scanned text of every book and magazine on archive.org (reviews, type-in listings, maps, hint
columns, adverts with prices and publishers' addresses); `archive_search.py grep <item> "<name>"` gives
the pages inside one item, ready for `pages`.

**text and command-line games** (mainframe, PLATO, BBS door, MUD, early adventures): the program may
survive as a printed listing (magazine type-ins, books of BASIC games), a tape image (bitsavers), a
Usenet post (comp.sources.games, rec.games.*), a BBS or shareware CD file (Discmaster), or on IFDB and the
IF Archive. Without it, transcripts are the primary evidence: Usenet posts (Google Groups search),
walkthroughs, magazine reviews with screen dumps, hint books, maps, and players' memories with exact
messages ("it said 'YOU ARE IN A MAZE...'"). Record every verbatim game message as `seen` copy.

**game** (including Flash, browser and mobile games): video is the primary evidence, and it holds
the game's own art and sound. Search widely: `community.py yt "<name> gameplay" --meta-only --n 10`,
then "<name> trailer", "<name> walkthrough", "<name> all levels", "<name> soundtrack/music/OST",
the developer's or publisher's channel, and the name in other languages. Prefer long, continuous,
period footage with original audio. Recover assets with `footage.py` (rebuild guide, "Recovering
assets from footage"), and search for rips, which are the original's own files: KHInsider game
soundtracks and "gamerip" albums (`downloads.khinsider.com`, music and often UI sounds, per track),
The Sounds Resource and The Spriters Resource (WebSearch `site:sounds-resource.com <name>`), The
Models Resource, fan wikis (`community.py media`), archive.org sound and asset dumps (`archive_search.py
search "<name> sound"`), and VGMdb for track names and order. Rips from an archived APK/IPA are fair to
use as files (art, sounds, data tables) without running the app.

**Mobile games and apps with an archived APK/IPA** (archive.org, APK mirrors): the package is a zip.
List it (`unzip -l app.apk`), then extract only data into `dossier/originals/`: `assets/` and `res/`
(images, sounds, fonts, `.plist`/`.json`/`.csv` data tables, localisation strings with every UI line),
and read the engine and file names (cocos2d, Unity `.assets` need a tool; plain folders do not). Internal
names map sounds and screens to events (`se_drop_bumper.ogg`). Many server-era games downloaded their
art and sound after install, so the package may hold only the shell: say so and look for rips. Never
run the package.
**Press kits and developer press pages** often survive in the Wayback Machine when the game does not:
official logo files, full-size screenshots of every screen, fact sheets with the rules. Look for
`/press`, `/presskit`, `/media`, `press_kit.zip` and "screenshots" pages on the developer's and
publisher's sites (`wayback.py urls <domain> --group`), and fetch zips with `--original`.

**Voiced games** (kids' and learning games, adventure games): narration carries the rules, the
prompts and the feedback. `community.py yt <id> --timed dossier/media/captions` keeps caption
timings, so every line can be tied to its frame, and `footage.py lines` cuts each line as a clip.

**Results screens are calibration data.** Score, rank and rating screens are what people upload most
("my high score", "new record", "I got GENIUS"). Collect the final seconds of many such videos
(`community.py yt --meta-only` to list them, `frames --start <duration-60>`), read every result (score,
class name, size, rank), and fit the scoring and thresholds to all of them: one result is a guess,
twenty pin the formula and the class boundaries. Record each reading as `seen` evidence with its locator. Let's plays and
speedruns show rules, timings, levels and sounds; tutorial and walkthrough videos and text
walkthroughs (GameFAQs-style) give exact levels, items, scores and secrets. Fan wikis give
numbers. Portal pages (via Wayback) give the title screen, controls blurb and description.
Note timings from video by timestamp (`locator: "youtube <id> @ 03:12"`), and measure them:
a `community.py frames --fps 30` burst over a jump or a fall, then `shots.py track <frames> --find
"#hex" --fps 30`, gives positions per unique frame and fits gravity and launch speed. Check the
footage is the original, not a clone: clones are common in "gameplay" uploads.
**Flash games: check whether the SWF survives** (`wayback.py find <portal-domain> <word from the
title> --mime flash`, or the `<embed>`/`<object>` src on the archived game page; mirrors on
partner domains are common). If it
does, that file **is** the game: fetch it with `--original` and run it with a self-hosted Ruffle
(rebuild guide), or port it with its own constants, scripts and assets (`inspect_swf.py`). That is the
biggest fidelity win available. The Wayback Machine now plays many archived SWFs in the page through
an emulator, so `shots.py wayback` can screenshot the real title screen and play field. If
the game is preserved in a playable archive, tell the user where to play it, and get its files from
there: check Flashpoint's catalogue (flashpointarchive.org; its database is searchable online), Flash
Museum (flashmuseum.net, which plays SWFs in Ruffle), and the Wayback replay. These copies often hold the
files the Wayback Machine never captured. Observing it as a player is fine, and measurable: `shots.py play
https://web.archive.org/web/<ts>/<url-of-the.swf> --steps steps.json` clicks, holds and records
timed bursts of the real game, and `shots.py probe --find "#hex"` turns them into positions per
frame. (The bare SWF address emulates more reliably than the page that embedded it.)

**Minigames inside a bigger client (MMO minigames, portal games that needed a login):** the
SWF often survives (`wayback.py find <media-subdomain> <name> --prefix /<games-path>/`), but it
renders blank on its own because it expected its host. Port it from its own scripts, constants and
assets (`inspect_swf.py`), stubbing the host calls, and take timing from video. Wiki numbers about time are often in game units, so check them against
a video burst.

**Facebook and portal games:** the developer's own site usually had a page per game
(`playfish.com/?page=game_<name>`, `zynga.com/games/<name>`), with the rules, tips and screenshots, and
it is often the best rules source. Big domains time out in `wayback.py find`: list hosts and paths with
`wayback.py urls <domain> --group --to <death>` and then `find --prefix`.

**Server-side games and apps (Facebook games, MMOs, social apps):** archived pages are login
shells, so `wayback.py timeline` mistakes them for life. Take the death date from shutdown
notices and news instead. The numbers live in **era fan wikis**: `community.py wiki "<Page>"
--site <name>.fandom.com --as-of <peak-year>-06-01` (data templates come back as fields), or the
Wayback copy of `<name>.wikia.com/wiki/<Page>` from the peak years (`wayback.py text`). Item and job
tables often live in templates: `wiki ... --expand` renders them. **Era drift**: games and apps were
rebalanced, so the "peak year" revision may describe a different game than the one asked for. Read the
earliest and the peak revision of one key page, decide which era you are rebuilding, record it as a
`[DESIGN]` call, and take every number from that era. Era
strategy guides, "complete job list" blog posts and spreadsheets come next. Screenshots in those
guides show the UI with real players' names: crop or redact before the museum.

**recipe, ruleset, api, place, media**: see `references/beyond-software.md` for their sources.

**other**: start with `web` to learn what it was, then pick families by what its questions need.

## 3. Websites from the Wayback Machine

Goal: restore the site's own pages (`$S/wayback.py mirror`) and understand its **design system and
templates**, so pages the archive missed can be rebuilt in the same shape.

1. `$S/wayback.py timeline <home-url>`: first and last capture, months alive, suggested
   snapshots spread across **design eras** (content-hash changes), and the first capture after
   death (often a shutdown notice, which is evidence of when and why). Try `www.` and bare
   domain variants.
2. Propose the era to resurrect: the last full version before decline by default, or the era the
   community misses most if that is clearly different. The user chooses at the checkpoint
   (`ledger.py versions target`).
3. `$S/wayback.py urls <domain> --group --mime text/html`: site map by section. This tells you the
   templates (home, listing, article, profile, search, help...).
4. Fetch two to five representative pages per template **from the chosen era**:
   `$S/wayback.py fetch <url> <ts> --out dossier/raw/<template>` (it notes when Wayback serves a
   nearby capture instead; cite the capture date it reports). Fetch their stylesheets too
   (find them in `extract.py page` output under `stylesheets`; fetch with the same timestamp).
5. `$S/extract.py templates dossier/raw` and `$S/extract.py page <file> --css <css>`: skeleton,
   nav labels, forms, design tokens (colours, fonts, widths), widgets, and the privacy scan.
6. Screenshot each template: `$S/shots.py wayback <url> <ts> dossier/screens/<template>-<year>.png`.
   Open them with Read and log `ui` evidence. Flash elements often render now (Wayback
   emulates them); if one does not, fetch the SWF with `--original` and capture every state with
   `shots.py swf` (it can type into the movie), or look for video or community screenshots of that part.
   Pages built by JavaScript after load (2012+ web apps) are often captured as empty shells:
   the data came from XHR calls. Look for those responses with `$S/wayback.py api <host>`,
   restore the archived JS bundle (it is the original client) for routes and labels, and find logged-in screens
   in launch blog posts and tutorial videos.
7. Help, FAQ, about, press and changelog pages explain intent and behaviour in the owner's own
   words: fetch and quote them.

Keep fetches lean: dozens of pages, not thousands. The CDX budget is about 24 requests a minute.

## 4. Scarce mode

When intake forecasts scarce evidence, change tactics instead of lowering the bar:

- **Pictures first.** Save each useful image with `$S/shots.py fetch-image <url> dossier/screens/<name>.png --page <page>`. WebSearch for "<name> screenshot", "<name> interface", "<name>
  <year>" in image-heavy places (fan wikis, Flickr, old blog posts, Imgur links in forum threads,
  magazine scans). Each image you look at yields several `ui` items.
- **Video frames as screenshots.** Video pages often expose thumbnails and chapter titles;
  transcripts narrate what is on screen ("click the green plus in the corner").
- **Store and portal listings.** Captured listing pages carry official screenshots and feature
  bullets, even when the product itself was never crawled.
- **Memory threads.** Search "remember <name>", "<name> nostalgia", "anyone else miss <name>",
  "<name> alternative" (people describe exactly what they lost when asking for replacements).
- **The user.** Ask once, concretely: "What screens do you remember? Any screenshots in old
  folders?" Log answers with `--family user --locator "user recollection"`, confidence medium.
- **Triangulate.** Two independent weak sources agreeing make a `seen` + `inferred` pair you can
  build on. One source alone stays medium confidence.
- **Accept honest gaps.** A scarce resurrection with clear `[DESIGN]` calls and a high soul score
  beats a padded one.

## 5. Query craft and the second hop

Hacker News search on a domain (`hn "goo.gl"`) matches every comment that merely *contains* a
link to it. Search stories, or quote a phrase people would write ("goo.gl shutting down").

Start short and broad, then narrow: `"<name>"`, `"<name>" <maker>`, `"<name>" review`,
`"<name>" shutting down`, `"<name>" screenshot`, `"<name>" how to`, `"<name>" tips`,
`"<name>" keyboard shortcuts`, `"<name>" alternative`, `"<name>" "I miss"`.

Second-hop queries that find the numbers: `"<name>" disassembly`, `"<name>" reverse engineered`,
`"<name>" ROM`, `"<name>" source code`, `"<name>" decompiled`, and `community.py github "<name>"`.
Check each result against the original before trusting it (section 5b).

Each find suggests the next query. Turn these into leads with `ledger.py lead add`:
- exact feature names users used ("river of news", "power-ups", "the shelf")
- version numbers and era names ("v2", "the 2009 redesign")
- people: developers' blogs, designers' portfolios (portfolios often show polished screenshots)
- fan sites, wikis, mods, competitor comparisons, "X vs Y" posts
- image hosts and video channels that keep turning up

## 5b. Fan remakes, ports and clones

Fan work is common for beloved dead things, and it is evidence of very different strength:
- **Imitations** (a CSS clone of a famous menu, a "remake" built from memory or video): their numbers
  are someone else's guesses. Log them as a lead at most, and measure the original yourself.
- **Disassemblies** (the original program's own instructions and tables, annotated) count as the
  original for provenance: `seen`, high. Comments in them are the annotator's
  reading; where comment and code disagree, the code wins.
- **Ports or decompilations built from the original files** (a C port of a Flash minigame that reads
  its constants, a fan wiki that published values from the game files): the numbers probably come
  from the original, but second-hand. Record values as `inferred` with medium confidence and the port
  as source, and confirm the soul numbers against the original (the surviving file, or measured
  video). Original assets bundled in a port (sprites, sounds) are the original's material: prefer the
  copy from the archive, and use the port's copy only after checking it matches (same size and look as
  the archived file or frames). Their own code is a rewrite, not the original.
- **Official open-source releases** (the owner released code: `community.py github` marks them "official
  release") are `legal` evidence and may be reused under their licence. Before reusing anything:
  1. Read LICENSE and NOTICE and record the licence as a `legal` item.
  2. Check what the release actually covers (often the engine or protocol, not the beloved UI), and record
     it as `seen`.
  3. Keep notices and attribution in app/README.md.
  4. The code licence covers the code, not the trademarks (relevant for share-safe mode).
  5. Add a "Licence and reuse" section to the Codex saying what was reused, what was not, and why.
  Search under the release's
  own name too (Google Wave → Apache Wave). Releases also ship **specs, protocol docs and whitepapers**:
  even when the beloved UI is missing, they are the most exact source for data models and behaviour
  (Wave's operation format and OT), so read them as `docs` evidence.

## 6. What not to collect

- Pages full of other people (profiles, guestbooks, forums): fetch them with `wayback.py fetch
  --private` (dossier/private/, git-ignored), take only structure, classes and colours, and screenshot
  them with `--mask-images` plus `--mask` on comment regions.
- User-generated content and personal data: comments, guestbook entries, forum posts' bodies,
  profiles, usernames, emails, photos of people. Note that such regions existed and their
  structure (`ui`/`content` evidence), never their contents. Short quotes of public opinions
  about the product are fine as sentiment evidence; cite the URL.
- Mirrors beyond the chosen era and pages you need: restore what the resurrection uses, not
  the whole domain. Program files come in with `--original` and run only in a sandbox (browser,
  emulator). Keep `dossier/originals/` and `app/original/` out of public repos unless the user decides
  to publish.
- Anything behind a login, paywall or robots exclusion; anything a site asks not to be fetched.

## 7. Stopping

`ledger.py round end` decides saturation. Also stop a family early when three different queries
return only things already in the ledger. Before leaving the dig, make sure every must-have
question is `answered`, `partial` with a clear inference, or `confirmed-unknown` with an
`unknown` evidence item saying what was searched.
