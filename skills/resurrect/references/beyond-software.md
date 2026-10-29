# Beyond software

> Where this guide mentions journeys, codex files, parity or the scoreboard, those are optional scored
> checks (`verify.py`). What is required is the pack, the build from it, and the fresh-eyes review
> (SKILL.md).

The method does not care what the lost thing was made of: recover the hidden recipe from its
traces, tag every claim, rebuild, and measure the rebuild against the traces. Only the sources,
the rebuild artifact and the yardstick change. Read the section for your `--type`.

Contents: 1. recipe · 2. ruleset · 3. api · 4. place · 5. media (exhibit only) · 6. Never

`$S/<script>` = `python3 "<absolute path of this skill>/scripts/<script>"` (quoted: the path can hold spaces).

## 1. recipe: discontinued foods, drinks, menu items

The label is a leaked spec. US/EU labels list ingredients in descending order by weight, and
the nutrition panel pins fat, carbs, sugar, protein and sodium per 100 g. Together they narrow
the formula far more than taste reviews can.

**Dishes made in restaurants** (never packaged, so no label exists): the formula lives with the people
who made it. Former employees describe the dough, sauce, cheese, pans and bake in forum threads and in the
comments under retrospectives; training videos show the build. `pantry.py hunt "<dish>" --brand "<chain>"
--served` lists those searches. When such a page answers 403 or a challenge, read its archived copy
(`wayback.py fetch <url>`, each page of the thread). Where they describe the formula, it beats a fit to
nutrition numbers: fit the quantities around what they describe. (In a past run a recipe built without the employees'
accounts lost to one built on them, although it had better ads and nutrition data.)

**Where labels survive** (look wherever your instinct says; these are the places runs have found them)
- **Photos of the pack, for anything sold before about 2010.** Databases started too late for old
  products; their labels survive as photos taken by collectors and sellers: packaging collectors on
  Flickr (some shoot every side of a box, panels included), Dinosaur Dracula, Mr. Breakfast (cereal),
  collector Instagram and Facebook accounts, auction listings (eBay, Worthpoint). Articles about the
  product reuse these photos with a credit line naming the collector: `$S/pantry.py hunt "<product>"
  --brand "<brand>" --credits <article URLs>` lists every spelling of the name, the searches to run and
  the collectors those articles credit. Read every photo that shows the side or back. A legible
  ingredient list or nutrition panel on a pack photo is the product's own label (authority: artifact).
  Other editions of the line (a later flavour pack, a co-branded or seasonal edition) usually share
  the base recipe, so hunt their boxes too (`--edition "<name>"`) and say which edition a panel came
  from. Period calorie-counter books (`archive_search.py fulltext`) often quote the nutrition panel.
  (In past runs a collector's photo of an old box held the product's whole label while every database
  held nothing; a run that skipped the photos fell back to a category average and lost.)
- `$S/pantry.py label "<product>"` (Open Food Facts keeps discontinued products): ingredients
  text, per-100 g nutrition, photos of the label. Try brand + flavour + size variants and
  `--barcode` if a UPC turns up anywhere. Look at `ingredients_image` with Read: it beats OCR'd text.
- **The brand's own site, from the product's years**: `wayback.py find <brand-domain> nutrition`,
  then `ingredient`, `product`, `label`, `.pdf` and `.txt` (`--from`/`--to` the sale years). Old
  brand sites often kept a nutrition data file or a product page per item; this is the maker's own
  label, so it outranks every database. Read hits with `wayback.py text`.
- Restaurant items: the chain's **ingredient statement and nutrition PDFs** via Wayback
  (`wayback.py urls <chain-domain> --prefix /nutrition` or search `"<item>" ingredients pdf`).
  They list every sub-component (sauce, bun, seasoning) with its own ingredient list.
- **Fan and collector pages** often transcribe the label from a kept bottle or box (ingredients in order,
  the panel, the can's fine print), under page names no keyword finds (`jostaw.html`). Run
  `wayback.py inventory <fan-domain>` (small sites list every page), read candidates with `wayback.py text`.
- **Retailer listings** keep discontinued items long after the brand page dies (Instacart, Walmart,
  Target, Kroger, Amazon, grocery-delivery sites, and their Wayback captures). The maker's ingredient
  statement and panel often sit only in the page's embedded JSON: `$S/pantry.py retail <url>` pulls them out.
- Brand product pages via Wayback, review blogs that photograph the back of the pack, unboxing
  and review videos (timestamps), commercials (what the product looked like), patents for
  the process (Google Patents), copycat attempts (r/TopSecretRecipes, copycat books) as
  `inferred` hints only, and "bring back X" threads/petitions for what people miss.
- Rank label sources: a photo of the pack you looked at (Read) beats a retailer listing, which
  beats a search snippet (low confidence; never the only source for the ingredient order).
  USDA lookups are reference data: do not link them to the label question.
- **When no photo of the label exists anywhere you searched**, a USDA category entry ("fruit leather,
  pieces") can stand in as the target: set `target.kind: "category"` and `target.searched` (every
  place you looked), and call the result a match against a category average, never the product's
  label. Ingredients still follow what the evidence shows (a starch chew is not a gelatin gummy).
- Flavour and texture words from reviews become `sentiment` items with features such as
  `flavour:tangy-finish` or `texture:shatter-crunch`. These are the soul.

**Method**
1. Ledger the exact ingredient list and the per-100 g panel as `seen` (`kind: data`).
2. For each ingredient, `$S/pantry.py food "<ingredient>"` (USDA FoodData Central; `--offline` uses a
   cached SR Legacy table with no key or rate limit, and it is used automatically when the API is
   rate-limited). Pick the closest entry and cite `USDA FDC <id>` as `seen`. Industrial ingredients get a home
   stand-in (`[DESIGN]`: "modified corn starch → cornflour").
3. Write `app/recipe.json` (schema in `pantry.py --help`), with ingredients in label order and
   `target.serving_g` set so label rounding is allowed for, and `target.source` naming where the
   numbers came from (maker's label > retailer > calorie database). Filled or layered products
   get a `part` per ingredient (dough, filling); with no surviving ingredient list set
   `"order": "unknown"`. Give ingredients `per100g.water` so cook loss stays physically possible. Mark salt and other amounts you know
   as `fixed`, and give flavour ingredients that reviews say you could taste a `min_grams`. Then run
   `$S/pantry.py solve app/recipe.json`. The fitted grams are `inferred` evidence
   (`derived_from` the label and the USDA items). Anything reported `at_minimum` is
   unconstrained by the numbers. Size it from flavour evidence and mark it `[DESIGN]`.
4. `$S/pantry.py match app/recipe.json` writes `codex/label-match.json`; `verify.py score`
   reports it as **Label match**. Iterate on stand-ins and cook loss (frying and baking drive off
   water) until it stops improving.
5. Turn the grams into a home recipe: `app/RECIPE.md`, titled with the original's name ("Homemade
   Magic Middles", "Arch Deluxe at home"), opening with a two-line summary (what it makes, yield, time)
   and then: ingredients with grams first and cups/spoons second, the method step by step (from
   process evidence or `[DESIGN]`), an allergen line, "what to tweak if it tastes off" mapped to the soul
   features, and a short "what this rests on" list (the label, the panel, the sources). This file is what
   the user cooks from and what the museum page shows, so write it for a home cook, not for the ledger:
   keep evidence IDs in a final column or footnote rather than mid-sentence.

**Restaurant items** (per-sandwich numbers, no weights): set `"basis": "serving"` in recipe.json.
The chain's own component nutrition (bun, patty, cheese) goes in as `per_serving` fixed parts,
and the unknown part (the sauce) is fitted to the difference. Look for the chain's nutrition menus,
component breakdowns (often PDFs) and "build" pages on its archived site
(`wayback.py find <chain-domain> nutrition --from <launch> --to <death>`, then `cdx` and `text`).
Report `fitted_share` with the match: a 99% match where the fit decided 15% of the item says
that the remaining 15% is consistent, nothing more.

Calories: labels compute them with general factors (9 kcal/g fat, 4 carbs, 4 protein); `match` does the
same by default, so a recipe whose fat, carbs and protein match is not marked down for USDA's
food-specific calorie factors (`"kcal_method": "usda"` in the target switches back).

**Verify**: Label match is automatic. Add `{"id": "J1", "feature": "F1", "kind": "label", "min": 0.9}`
to journeys.json and `verify.py score` counts it as a passing check. Taste, look and texture are manual journeys
(`"manual": true` in `codex/journeys.json`; see `references/codex-template.md`) that the user records after
cooking with `verify.py manual`. Never claim a taste match nobody tasted.

**Rules**: flavour house formulas ("natural flavour") stay `[UNKNOWN]` and become a disclosed design
choice. Name the recipe after the original ("Homemade <Product>"); just don't pass a batch off as the
maker's product.

## 2. ruleset: board, card, party and playground games; game-show formats

Restore the game as published: its name, rules text, board, cards and art, recovered from scans
and photos (law 2), for personal use. Only share-safe mode rewrites the rules in your own words and
redraws the components (`references/share-safe.md`).

**Sources**: rulebook scans on archive.org (`archive_search.py search "<name>" --mediatype
texts`), BoardGameGeek pages and forums (rules questions reveal edge cases), how-to-play
and play-through videos (timestamps), the publisher's site via Wayback, episode guides and
fan wikis for game shows, and memory threads for house rules (record them as variants).

**Rebuild**: `app/RULES.md` (setup, turn structure, scoring, end, edge cases, each line traced
in the Codex), printable components restored from scans of the originals (board, cards, rulebook)
where they survive and redrawn to match where they don't, and, when the game
is small, a playable web version that can run seeded simulations.

**Electronic games** (a talking tower, a handheld, a game with a chip): the printed rules give
setup and flow, but the odds, prices and event chances live in the device's program. The ROM dump
(MAME and preservation sites) and its disassemblies are the original logic: port the tables and
routines from them (playbook 5b), or at least measure them from long play-through videos
(count events across many turns) before marking them `[DESIGN]`. Scanned rulebooks without a text
layer: `archive_search.py pages <id>` gives page images to read.

**Verify**: if there is a web version, journeys assert rule outcomes through
`window.__RESURRECT__` (for example "a 7 robs the leader"). A playthrough of the printed
version is a manual journey.

## 3. api: dead public APIs, feeds, widgets, embeddable services

Programs that called it are the evidence. The goal is a local server that old clients could
talk to.

**Sources**: `wayback.py api <host>` (archived real responses), docs via
`wayback.py urls <docs-host> --prefix /<docs-path>`, client libraries and their tests on
GitHub (request/response shapes; respect licences, read, don't vendor), Stack Overflow
questions (real requests, error messages, rate-limit behaviour), and blog tutorials.

**Rebuild**: a small server (stdlib Python or Node) with the same paths, parameters, status
codes, error bodies and content types. Fill it with synthetic data in the archived shape,
never data replayed from the archive about real people. Do not proxy to a successor
service.

**Payload strings.** Interface identifiers that clients parse (namespace URIs, element and key
names, content types, error codes) are facts about compatibility: keep them exactly, because
old clients break otherwise. Human-readable branding inside the payload (titles, "Yahoo!
Weather", copyright lines, logo URLs) stays as the original had it (share-safe mode replaces it with a
neutral string).

**Find real responses**: `wayback.py api <host> --prefix /<endpoint> --errors` (error and
shutdown responses are evidence too; `fetch` keeps archived 4xx bodies), docs pages, tutorials and
forum posts that pasted a full response, and client libraries' test fixtures.

**Verify**: `request` journeys built from archived examples: status, content type, the JSON
fields (`json`) or XML paths (`xml`, with counts) that clients relied on. Then `verify.py shape`:
list archived responses in parity.json `shape` and the scoreboard reports **Shape match**, the API
counterpart of the visual score. In the museum, use `text_pairs` to show an archived response
next to the rebuild's.

## 4. place: defunct rides, shops, venues, exhibitions, physical spaces

**Sources**: ride-through and walk-through videos (timestamps give the sequence and timing),
park maps and brochures on archive.org, fan sites, trip reports, and photo sets (describe them
as `ui` evidence).

**Rebuild**: an interactive walkthrough page made of a map or floor plan and the scenes in sequence
with timing. Use the original's own material where it survives (official photos, the ride's narration
and music from source recordings, park maps); where it doesn't, reconstruct from the clearest frames. Keep the
original's scale and timing (for example "4 min 30 s, 7 scenes").

**Senses a page cannot reproduce** (the orange-grove scent, a cold room, a seat that moved):
keep them as evidence and in the story, render them as an explicit cue ("you smell oranges") at
the right moment, and verify them with a manual journey. Do not rank them as the soul just because
people mention them most; the soul is what the rebuild can actually deliver.

**Verify**: journeys walk the sequence (scene count, order, timing hooks). Visual pairs compare
your scenes with the evidence frames.

## 4b. Device interfaces (handhelds, music players, phones, appliances): use `--type app`

**Sources**: the user guide (archive.org manuals and the maker's support PDFs, via Wayback),
hands-on and review videos (measure transitions with `frames --fps`, sounds with `community.py
audio`), teardown and press photos for proportions, and open firmware projects (Rockbox, custom
ROM wikis). Those are fan reverse-engineering: facts about wheel resolution, timings and screens go in
as `inferred` unless they quote the original firmware.
**Look and sound**: restore the device's own look: its shell, logo, interface font and system sounds,
taken from press images, recordings and firmware resources where they survive, and reconstructed to
match where they don't (share-safe mode swaps these for neutral ones).
**Verify**: `arc` steps for wheels and dials, `upload` for the user's own media, manual checks for feel.

## 5. media: lost TV episodes, ads, songs, web videos, radio (exhibit only)

Recreating a copyrighted work from memory still creates a copy or derivative of that work. So
for media, **the exhibit is the resurrection**: a museum page that reassembles everything
known (timeline, credits, descriptions, surviving fragments linked where they are publicly
hosted, what each source says, and a "hunt list" of the unknowns for lost-media searchers).
Skip Rebuild and Verify: Codex → Unveil, with `parity.json` listing *knowledge* items instead
of features. The exception is material the ledger records as public domain, openly licensed,
or the user's own.

**Sources**: period press first (newspaper and magazine coverage from the time quotes the
work directly: search `"<title>" <year>` plus newspaper names, and archive.org texts), then Lost
Media Wiki (and its Wayback copies when it blocks fetches), fandom wikis read
with `community.py wiki --raw` (quotes from period newspapers sit in templates), newspaper and
magazine archives (archive.org texts, Google News Archive), museum and festival event pages
(screenings), official anniversary books and documentaries, and news clips. **Fragments** go in
the exhibit as `fragments`: link only official or owner-sanctioned copies, describe unofficial
uploads (where and what) without linking them, and never record an uploader's name or email.
The **hunt list** (`hunt`: what is missing, where it was searched, the best lead) is the most
useful thing the exhibit can give the people still searching.

**Letting people experience it, honestly**: `known_scenes` lists the documented moments in order,
each with a short verbatim quote from the press (a line or two, cited, as reporting does) and a
note on how it is known. Gaps stay gaps. Do not write the missing dialogue, songs or scenes, do not
voice or draw the characters, and do not stage moments that rest on a single second-hand report.
That is exactly what turns an exhibit into a fake "found" episode.

## 6. Never

- **People.** No personas, chatbots, voice or face recreations of real people, living or dead,
  however much material survives. A defunct *product* with a mascot is fine; a person is not
  a product.
- **Private traces.** Deleted posts, private accounts, leaked data, or someone else's private
  details from their personal site (its other content comes back; ask whether the site is the
  person's own, law 3).
- **Still available.** The product is sold, the API is live, the ride runs elsewhere, or the brand
  relaunched it (common for foods): stop and point to it (law 4).
- **Passing off.** Never present or sell a restoration as an official release or a recipe as the
  maker's product. (Publishing at all is the user's call; share-safe mode exists for it.)
