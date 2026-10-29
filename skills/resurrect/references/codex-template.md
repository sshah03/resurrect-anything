# Codex template

The Codex is the cited spec the rebuild follows. Every content line (bullets, table rows and
prose; code fences are skipped) must contain an evidence ID (`E0012`, several allowed), or `[DESIGN]` (a decision we made where evidence is
silent), or `[UNKNOWN]`. Sections whose heading contains "Unknown", "Open question",
"Provenance", "Appendix" or "How to read" are exempt. Check with
`ledger.py check-spec codex/CODEX.md`.

Prefer `seen` evidence for soul features. When a line rests only on `inferred` items, say so
in the line ("inferred from E0031, E0044").

## codex/CODEX.md

```markdown
# Codex: <Original>, restored

## How to read
IDs like E0012 point to evidence.jsonl. [DESIGN] marks our decisions; [UNKNOWN] marks gaps.

## Identity
- Core job: <one sentence> (E0001, E0007)
- Target version: <label from `ledger.py versions`, e.g. "2.0 (May 2014 - Sep 2016)"> (E0015) and why: the
  last full version before it died (default), or the era people miss, as the user chose at the checkpoint
- Version timeline: <one line per iteration with its dates and what changed> (E0016, E0017); evidence from
  another version is cited with "(from <version>)" in the line
- Born <date> (E0002); died <date>, <cause> (E0003)
- Restoration of <Original>; original material belongs to its owners (share-safe mode: a codename instead)

## Restoration plan
| Part | Source | Files | Evidence |
|---|---|---|---|
| Bird (3 flap poses) | recovered: cut from footage, 43 takes merged | app/original/sprites/bird-*.png | E0012 |
| Wing / point / hit sounds | recovered: cut from gameplay audio | app/original/sfx/*.wav | E0014 |
| Medal panel art | recovered: fan-wiki rip, checked against frames | app/original/ui/medal-*.png | E0018 |
| Unfilmed settings menu | reconstructed from two reviews | (nothing survived) | E0031 |
| Game (whole), if the SWF survives | original SWF in self-hosted Ruffle | app/original/game.swf | E0040 |

Sources, most original first: the original's own files; its pieces recovered from traces (footage,
GIFs, rips); reconstruction of what no trace holds (SKILL.md law 2). Every original or recovered
file is listed in app/original/MANIFEST.json with its origin and locator.

## Screen inventory and flow
| # | Screen | Reached from / by | Leaves to / by | Transition | Evidence |
|---|---|---|---|---|---|
| S1 | Title | launch | S2 by tapping Start | fade to black ~0.5 s | E0020 (scenes 1-3) |
| S2 | Get Ready | S1 | S3 by first tap | Get Ready fades as play starts | E0021 |

`codex/screens.json` holds the same inventory as data, so every screen gets compared with its frame:

```json
[{"id": "S1", "name": "Title", "then": "dossier/screens/title.png", "shot": "title", "evidence": ["E0020"]},
 {"id": "S2", "name": "Get Ready", "then_frames": "dossier/screens/ready-*.png", "shot": "ready",
  "evidence": ["E0021"]}]
```
`then` is the clearest reference frame (straightened with `blueprint.py rectify` if it was photographed),
`shot` the name of the journey `screenshot` step that shows the rebuild's version of the same screen.

## Behaviour spec (one block per feature, the reverse-engineering way)
### F1 <feature>
- Observed: <input> -> <response>, step by step, with timings and numbers (E0045 @ 01:12.4)
- Rules: <what scores, what kills, what unlocks> (E0046)
- Numbers: <measured values with units and method> (E0047, shots.py track)
- Fitted: <the rule fit.py found, its reading count and worst error, and the reading that would settle
  it> (inferred from E0045-E0052, fit.py rule)
- Inferred: <what the observations imply> (inferred from E0045, E0047)
- Unknown: <what no trace shows> [UNKNOWN]
- Test: journey J3 replays this sequence

### Apps: what each behaviour block must cover
Apps are judged by feel, so spec every one of these that the evidence touches:
- Gestures: each swipe, drag, long press and pull, with its commit threshold, what happens just short
  of it (the spring back), velocity rules, and the edge zones (sidebar grab width, back swipe).
- Animations: duration, curve and overshoot of every transition (open, close, delete, reorder, sheet).
- Screens and states: empty, first launch (tutorial content), loading, error, offline.
- Settings and defaults: every option on the settings screens, its default, and what it changes.
- Data: what is stored per item (fields, ordering rules, dates), what survives a restart.
- Text: every label, button title, alert and empty-state line, verbatim.
- Dead services (sync, sign-in, sharing): the screens as they were, with a local stand-in behind them.
- Viewport: the device it was used on (`ledger.py init --viewport 320x568`).

## Asset list
| Asset | Kind | Source | Native size | Evidence |
|---|---|---|---|---|
| bird flap 1-3 | sprite | recovered (footage.py sprite, video X @ 0:10-0:13) | 17x12 | E0012 |
| wing | sound | recovered (footage.py sfx, 12 takes) | 90 ms | E0014 |

## Soul (build and verify these first)
| # | Feature | Why it matters | Evidence |
|---|---|---|---|
| F1 | <name> | missed by N independent sources | E0040, E0052, E0061 |

## Screens / templates
### <Screen name>
- Layout: <regions, proportions, fixed width or fluid> (E0020)
- Elements: <nav items in order, buttons, panels> (E0021)
- States: empty / loading / error (E0033) or [DESIGN]

## Flows
### <Flow name>
1. <step> (E0034)
2. <step> [DESIGN]

## Behaviour, rules and numbers
- <keyboard shortcut / timing / rule / formula> (E0045)

## Design system
- Palette: <hex values with roles> (E0060 from extract.py tokens)
- Type: <families and sizes; the original font files if recovered, else the closest match> (E0061)
- Spacing, widths, corner radius, iconography style (E0062)
- Sound and motion: <descriptions> (E0070)

## Content model and placeholder policy
- <entities users created> (E0080)
- Regions that held user-generated or personal content: <list> -> fictional placeholders [DESIGN]
- Site's own editorial content: restored as it was from the archive (E####); only pages no capture holds get
  marked stand-ins [DESIGN]

## Copy deck
| Location | Text | Evidence |
|---|---|---|
| Top nav | "Home · Browse · Upload" | E0021 |

## Copy and settings from discussion
Users describe settings, gestures and wording the screenshots never show. Each line: the behaviour, how
many independent places describe it (threads, videos, reviews), the dates, and the version (E0040, E0052).

## Modernisation choices
- Stack: <choice> [DESIGN]
- Responsive behaviour, accessibility, storage, performance [DESIGN]

## Unknowns
- <what could not be established, what was searched, and the design call taken>
```

## codex/parity.json

```json
{
  "features": [
    {"id": "F1", "name": "River of news", "priority": "soul", "weight": 3, "tags": ["river-of-news"],
     "evidence": ["E0040", "E0052"], "journeys": ["J1", "J2"], "status": "missing"},
    {"id": "F2", "name": "Folders in sidebar", "priority": "core", "evidence": ["E0021"],
     "journeys": ["J3"], "status": "missing"}
  ],
  "visual": [
    {"name": "home", "then": "dossier/screens/home-2011.png", "now": "museum/shots/home.png",
     "then_clip": [120, 40, 320, 568]}
  ]
}
```

Priorities: `soul` (weight 3), `core` (2), `extra` (1). `weight` overrides. Statuses:
`missing`, `built` (set by you), `verified` and `broken` (set only by `verify.py`). `tags` are the
ledger's feature tags this feature covers. Evidence-backed tags that no feature covers show up
on the scoreboard as "left out". Deliberate omissions are listed in `codex/LEFT-OUT.md` (one line
each: what, the evidence IDs, why); a feature you could not get to pass stays `built` with its
reason there.

`visual` holds only extra pairs. Every screen in `codex/screens.json` becomes a pair on its own
(named `<screen id>-<name slug>`, e.g. `S1-title`), and its LOOK.md section uses that name:
`## S1-title (code 7F3A)`, then one `- band N: ...` verdict per band of its look sheet: `matches`,
`other moment: <what>` (an animation or scroll phase, a cursor), `differs, no trace: <what is missing,
where you searched>`, `matches (stand-in content)` or `matches (photo: <why>)`. A plain `differs: <what>` is
an open to-do: the scoreboard lists it, and the screen is not settled until it is fixed and looked at again.

## Journey cases
Each journey may carry `"case": "normal" | "wrong" | "edge"`. A soul feature whose evidence shows no
wrong input or edge carries `"failure_case": "none in the evidence"` in parity.json instead. Soul features need at least one `wrong` or
`edge` journey wherever the evidence shows one (a wrong answer, game over, an error message, a limit, an
empty state): a rebuild that only passes the happy path is not yet the thing.

## codex/journeys.json

One journey per observable behaviour, derived from the cited flows. Journeys check
behaviour, not pixels. For canvas games, expose test hooks on `window.__RESURRECT__` (score,
lives, level, state) and assert on them with `eval` (`equals`, `gt`/`lt`, `approx` + `tol`).
Use `hold`, `mouse_down`/`mouse_up` and `key_down`/`key_up` for held input, `drag` (with `dx`/`dy`/`steps`) for
swipes, so that real pointer events exercise the gesture code, and `wait_until` instead of
fixed waits. APIs use `request` steps. The full step list is in `verify.py --help`. Checks a browser cannot
do (taste, a printed playtest) are **manual** journeys that the user records. A journey that expects
harmless console noise (a blocked script in a sanitiser test, a missing favicon) gets
`"allow_console": "<regex>"`. A journey that sets
state through test hooks instead of real input gets `"via": "hooks"`; the scoreboard reports how many
there are, so prefer real input (`click` with `x`/`y`, `drag`, `hold`) for soul features.

```json
[
  {"id": "J1", "feature": "F1", "name": "j/k moves through items",
   "steps": [
     {"do": "goto", "url": "/"},
     {"do": "expect_count", "selector": ".item", "min": 3},
     {"do": "press", "key": "j"},
     {"do": "expect_visible", "selector": ".item.is-current"},
     {"do": "screenshot", "name": "home"}
   ]},
  {"id": "J7", "feature": "F4", "name": "eating a pellet scores 10",
   "steps": [
     {"do": "goto", "url": "/?seed=1"},
     {"do": "press", "key": "ArrowRight"},
     {"do": "wait", "ms": 400},
     {"do": "eval", "js": "window.__RESURRECT__.score", "equals": 10}
   ]},
  {"id": "J8", "feature": "F5", "name": "holding the button climbs",
   "steps": [
     {"do": "goto", "url": "/?seed=1&test=1"},
     {"do": "eval", "js": "window.__RESURRECT__.y", "gt": 0},
     {"do": "hold", "selector": "canvas", "ms": 600},
     {"do": "eval", "js": "window.__RESURRECT__.vy", "lt": 0}
   ]},
  {"id": "J9", "feature": "F2", "name": "GET /v1/url returns longUrl",
   "steps": [{"do": "request", "url": "/v1/url?shortUrl=x", "status": 200,
              "content_type": "json", "json": {"longUrl": "*", "kind": "urlshortener#url"}}]},
  {"id": "J10", "feature": "F3", "name": "shatter crunch", "manual": true,
   "check": "Bite a chip from the test batch: it snaps cleanly rather than bending (E0031)"}
]
```

Name screenshot steps after the screen's `shot` in screens.json (or the extra visual pair they feed:
`home` above writes `museum/shots/home.png`). Match the "then" framing: same viewport (`verify.py run --width/--height`),
or `"selector": "#stage"` / `"clip": [x, y, w, h]` on the screenshot step. When the "then" is a
promo composite, a video frame or a page shot, set `then_clip` (and `now_clip`) in the visual
pair so both sides compare the same region. A pair against a photo of a physical object (a board, a
device, a set) gets `"reference": true`: it appears in the museum but stays out of the Visual score.
Moving game screens score as noise: give the rebuild a frozen canonical scene (`?test=1&scene=title`)
and compare that against a matching still, or mask the moving objects.
