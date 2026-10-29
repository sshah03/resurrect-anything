# Restoration guide

> Where this guide mentions journeys, codex files, parity or the scoreboard, those are optional scored
> checks (`verify.py`). What is required is the pack, the build from it, and the fresh-eyes review
> (SKILL.md).

*Advice from past runs, not a procedure: use what helps with this target, in whatever order your own judgement says.*

Principle: **the thing that existed, as exactly and fully as possible** (SKILL.md laws 2 and 6).
For every part use the most original source there is: its own files where they survive, its own
pieces recovered from traces (footage, GIFs, rips), and reconstruction only where no trace holds
it. Modern pieces go only where the original cannot run today, and they stay invisible: a player,
a polyfill, a stand-in for a dead server.

Keep `app/original/` for original and recovered files. `MANIFEST.json` records where each came from.
`wayback.py mirror` writes it for mirrored pages; add the rest by hand in the same shape (paths are
relative to the manifest's folder; any other shape is not counted):

```json
{"files": {
  "copter.swf": {"origin": "original", "url": "https://...", "how": "wayback.py fetch --original"},
  "art/logo.png": {"origin": "recovered", "from": "https://youtu.be/X @ 00:07.75",
                   "how": "footage.py cut, 3 takes merged, fringe cleaned by hand"}
 },
 "missing": []}
```
Everything else in `app/` is code or reconstruction. `verify.py score` reports the original share
and the recovered count.

What counts as which:
- `original`: a file exactly as the original shipped or published it (a mirrored page, an extracted
  sound, a press-kit logo PNG), byte for byte.
- `recovered`: the original's own pixels or samples, taken out of something else and cleaned up
  (cut from a video frame, a crop of an official screenshot with text erased, a sound cut from a
  recording, a fan rip checked against footage).
- Anything drawn, synthesised or written by you is reconstruction, even when traced from a frame.

## Recovering assets from footage (the usual case)

When the files are gone, the original's art and sound are still in its videos. Recover them; do
not redraw them.

1. **Pick the best recordings.** Early (the version people remember), unedited, original audio, no
   facecam over the play field, highest resolution. A trailer from the developer is often the cleanest
   footage. Check every video's version (title screens, UI details) before mixing their evidence.
2. **Screens:** `footage.py scenes <video>` lists every screen with timestamps and a contact sheet.
   Each distinct screen becomes a row in the Codex screen inventory; the order gives the flow.
   Crop a clean frame of each (`shots.py crop`) as the layout reference.
3. **Sprites:** take a burst where the object moves (`community.py frames --fps 24 --start --end`, or
   ffmpeg on the `footage.py get` copy), then `footage.py sprite <frames> --region <play field>
   --follow x,y --native-width <logical width>`. Every take of a pose is merged (the median cancels
   compression noise) and reduced to native pixels. Open `poses/sheet.png`, keep the real animation
   frames, and fix stray pixels by hand at native size against the clearest frame, with colours from
   `shots.py probe`. Smooth Flash, vector or 3D art is not pixel art: use `sprite --vector`, which keeps
   full resolution.
   Static art (logos, characters in a pose, buttons, panels, icons) comes from the cleanest frames with
   `footage.py cut <frames> --box x,y,w,h --trim`: it merges the takes and clears the background inward
   from the box edges, so outlines protect colours that match the background. Use the highest-resolution
   video and keep the cut at its size: Flash art was vectors and looked sharp at any window size, so
   ship the large cut and draw it at the logical size on a canvas scaled by `devicePixelRatio`. Shrinking
   it to the stage size (`--width`) and letting the browser stretch it back is what makes rebuilt art
   look blurry. Flat Flash colours that also fill holes in the art: add `--key #hex`.
   Backgrounds and pictures that are always partly covered (play fields behind sprites, a jigsaw picture
   behind its pieces, a menu behind a popup): `footage.py merge <frames> --out plate.png` takes, per
   pixel, what most frames show, so whatever moves vanishes; it writes a mask of what no frame showed.
   Gather frames from several videos first with `footage.py stage <frames> --size <stage>` so they line
   up.
   **Art that came out small or blurry** (`enhance.py`, never a plain stretch). Pick by what the art was:
   - *Pixel art* (consoles, handhelds, phone games, DOS, retro-styled Flash): `enhance.py native <crop>
     --out sprite.png` finds the original pixel grid, also at fractional scales like 2.83x, reads one
     colour per original pixel and snaps it to the palette. That is the original sprite at its true size.
     Pass `--native-width` when the logical width is known (NES 256, SNES 256, Game Boy 160, GBA 240,
     DS 256, Flappy Bird 144). The game zoomed it with nearest-neighbour, so ship it that way
     (`image-rendering: pixelated`). Rotated, scaled or moving-blurred sprites are off the grid: take them
     from a still screen.
   - *Several takes of the same thing* (a static screen in many frames, an object that pans across):
     `enhance.py stack <takes> --out f.png --scale 2` lines them up to a fraction of a pixel and solves
     for the finer picture. It is real detail, but only when the takes sit at different sub-pixel
     positions, and it says whether they did.
   - *Smooth art* (Flash, painted, 3D renders, photos): `enhance.py upscale <cut> --out dir` runs
     Real-ESRGAN (install it once with `enhance.py setup`). Outlines and flat fills come back sharp, but
     the model invents fine detail. Open `*_review.png`, which shows both whole pictures and the tiles the
     model changed most, and reject what it made up. Small text always garbles: re-set text in the
     period font instead. Faces and logos drift; patch them from the source.
   - *Flat logos and shapes*: `enhance.py vector` traces SVG. Gradients turn into bands, so use it only
     where the original was flat.

   In the MANIFEST, an enhanced file stays `recovered`, with `"enhanced": "<command, model>"`, and the
   un-enhanced cut is kept beside it.
4. **Fonts and text:** copy every string exactly as shown (capitalisation, punctuation, typos). Bitmap
   fonts are recovered as glyph sprites from frames that show many characters (score counters,
   menus); otherwise find the font by name (fan wikis, font-identification threads).
5. **Sounds:** `footage.py sfx <video> --start --end` over plain gameplay. Repeated sounds are the
   effects; match each to its event by time: find the moments in the video (a wrong answer at 31.2 s,
   a coin at 15.0 s) and pass them as `--events 31.2:wrong,15.0:coin` to name the groups. Use the cleanest take;
   if all sit under music or commentary, split the recording first with `footage.py stems <video>
   --start --end` (Demucs; `enhance.py setup --audio` installs it). A narrator goes to `vocals`, and
   `--two-stems vocals` leaves the game's own sound in `no_vocals`. Hits and clicks usually land in
   `drums`, the tune in `other` and `bass`. Run `sfx` on the stem, not the mix: on a narrated video, the
   mix's "effects" are mostly syllables. Otherwise, look for a quieter stretch, another video, or a rip
   (The Sounds Resource, fan wikis: `community.py media --search`). Music: `footage.py loop` over a menu or
   pause screen; `loop --stack` over a long stretch medians every cycle (music restarts included), so a
   narrator or effects on top drop out. Then `sfx --minus-loop <loop.wav>` subtracts that loop from the
   game audio and finds the effects it was hiding.
6. **Rips:** fan wikis and rip sites often hold sprite sheets and sound files taken from the game
   itself (`community.py media <page> --site <wiki> --download`). Check each against footage
   (size, palette, timing) before treating it as original; record it as `recovered`.
7. **Numbers:** speeds, gravity, spawn rates and timings from bursts (`shots.py track`); scores, time
   bonuses, counters and floaters over time from `footage.py hud --region <the HUD>` (one sheet, each
   change once, with timestamps). Every reading gets a ledger item and a row in a readings table (CSV,
   with an `evidence` column). Then let `fit.py` find the rule instead of guessing it:
   - `fit.py rule readings.csv --y points --x length,streak` tries constant, linear, two-variable,
     product, quadratic, every-N steps, a cap or a change of slope, power and exponential. It snaps the
     constants to the round numbers designers use while every reading still fits, and names the
     reading that breaks each rule that fails. When two rules fit, it names the one reading that would
     tell them apart: go and find it.
   - `fit.py motion track.json --fps <game fps> --scale <video px per game px>` gives gravity shared
     across arcs, when the taps were, whether a tap sets the speed or adds to it, a terminal speed, and
     the per-frame constants with the half-step correction (the round one is the original).
   - `fit.py table` (enemy -> points, with disagreements), `cutoffs` (rank boundaries and the gap each
     lies in), `timing` (fixed, ramp or random spawns; whether types come in a fixed cycle).
   Record each rule as `inferred` with `derived_from` the readings (`--batch` writes the ledger line).
   State how many readings it rests on.
8. **Privacy in footage:** trailers and let's plays of social games show real people's photos, names and
   friend lists, and avatar games show players' own avatars (Miis, Xbox avatars, Club Penguin
   penguins dressed by players). Crop or redact them in anything shipped or shown in the museum, and
   use fictional avatars in their place (law 3). The game's own art and its default characters are
   fine.
9. **Effects under music** rarely come out clean: `sfx` finds the timing and pitch, but the cut carries
   the music. Look for a cleaner source first: a menu with music off, a video with game-only audio, the
   app package, or a rip. Synthesise only what none of them hold, matched to the measured length and
   pitch, and mark it reconstructed.

(Share-safe mode replaces this with clean-room rules: `references/share-safe.md`.)

## By type

### Websites

1. Pick the era (the timeline's design eras, the era people miss), then restore it:
   `wayback.py mirror <url> <ts> --depth 1 --max-pages 40`. Pages, stylesheets, images, scripts,
   Flash and backgrounds come back raw from the captures nearest that date, with links rewritten.
2. Open the entry page and compare it with an archived screenshot of the same date. Fix only what
   the archive broke: missing files (listed under `missing` in the manifest), absolute links the
   rewrite missed, and scripts that call dead services.
3. Reconstruct each missing file from the best evidence (another capture date, a screenshot, a
   video frame), matched in size and style, and mark it as reconstructed.
4. Flash inside pages: keep the original SWF and load it with a self-hosted Ruffle (see Games).
5. Forms and server features (search, guestbook submit, login): replace the server with a small
   local stand-in that answers the way the original did. The page stays the original.
6. Other people's content (guestbook entries, comments, member pages): keep the structure and
   styling and swap in fictional entries (law 3). The site's own copy stays as it was; for its
   author's private details, law 3 says to ask whether the site is the person's own.

**When the archive caught only part of a site** (the usual case for 1990s personal and fan sites: a
homepage and a few images, the subpages, animations and MIDI music missing):
1. Map what existed before restoring anything: every link on every captured page is a page that
   existed. `wayback.py urls <host>/<path> --group` and `cdx` on each linked URL (all dates, all
   statuses: a 404 capture still proves the name), plus old host variants (`geocities.com/Area51/...`
   and `geocities.com/<user>`, `members.aol.com`, `angelfire.com/<state>/<name>`, `www.` or not).
2. Look outside the Wayback Machine. GeoCities: Archive Team's 2009 dump on archive.org
   (`archive_search.py search "geocities archive team"`), and the mirrors that serve it by path
   (oocities.org, reocities.com, geocities.restorativland.org, geocities.ws). Angelfire, Tripod and Xoom
   sites were often re-hosted or copied wholesale by fans; search for an exact sentence from the page in
   quotes. Webrings, link directories (Yahoo!, Open Directory) and "cool site of the day" pages give the
   titles and descriptions of missing pages.
3. Recover missing files by name: 1990s sites shared animated GIFs, backgrounds, counters and MIDI files.
   Search the exact file name (`"dancing_baby.gif"`, `"xfiles.mid"`) on the web and in other archived
   sites (`wayback.py find <other-host> <name>`); a byte-identical copy elsewhere is the original file.
4. Text of missing pages often survives quoted: in Usenet posts, forum threads, articles about the site,
   and in other sites that copied a page. Each quoted passage is `seen` evidence for that page.
5. Lost index and list pages (a ring's member list, a link page, an A-Z): rebuild them from lists of the same
   period elsewhere (fan lists of the same ring, the Yahoo! or Open Directory category, webring hub pages,
   "new pages" tables on the site itself), each entry cited to where it was listed and when.
6. Rebuild each missing page only from what survives about it: its title and link text from the
   pages that linked to it, quoted passages, screenshots, descriptions. Use the site's own surviving
   pages as the template (same HTML style, same fonts and backgrounds), mark every reconstructed page,
   and list pages nothing survives of as "known to have existed" (catalogue tier) rather than inventing
   them.
7. Dead services embedded in the page (hit counters, webring navigation, guestbook buttons, "best viewed
   with" badges served by other hosts): point the tag at a static local image of what it showed (a counter
   frozen at the last archived value, the badge from its own host's capture), not a script that runs after
   the browser has already failed to load it. Original pages changed only for privacy or link rewriting are
   `"origin": "original-edited"` in MANIFEST.json (they still count as original).
8. **Every state the archive holds.** A partly archived site is restored in every version the captures
   show, not one per page: each dated state of a page (an early version and its later rewrite), its closure
   notice, its "we moved" stub and its redirect are all part of the story, and visitors can step between
   them (a dated list on the landing page). Pick a default state per page; keep the rest one click away.
9. Several versions: pages changed roles between captures, and a site's last captures are often stubs
   (a closure notice, a few hundred bytes). `wayback.py known --died <year>` flags stubs and post-death
   captures; pick each page's capture from the era you target. Two sites from two eras get two targets
   (`ledger.py versions target "early page,later index"`).
10. Period fidelity: the original's HTML as written (tables, `<font>`, frames, `<marquee>`, `<bgsound>`,
   `<embed src=*.mid>`), viewed at the screen size of the time (640x480 or 800x600). Never modernise the
   markup; add a tiny shim only where today's browsers dropped a feature (MIDI playback, `<blink>`).

### Web apps

1. Restore the shell: the archived pages plus the original JS and CSS bundles (`mirror`, then
   `wayback.py inventory <domain>`: client bundles, including Closure/j2cl module URLs like `/_/js/` and
   `/_/scs/`, are listed as program files). Read them with `inspect_js.py`: every UI string, message,
   CSS rule and colour is the original's own, even when the bundle is too partial to run. Bundles from
   several capture dates (`--diff`) show which strings came and went between versions.
2. **Run the original client against a resurrected backend:** (`inspect_js.py` lists its strings; search the
   bundle for its endpoint paths, e.g. `grep -o '"/ajax/[^"]*"'`). Stub the server in the browser or with a
   small local server, and answer exactly the shapes the client expects. Pitfalls: dead third-party calls
   (analytics, ads, beacons) inside the original's success handlers throw and undo the UI: stub them to
   no-ops before the client loads; keep the original files untouched and put the shim in its own file.
   Take the response shapes from archived responses (`wayback.py api`, docs) and serve those paths
   locally with fictional data (`verify.py shape` checks them). This is usually the most exact
   result possible for a dead web app.
3. Where the client did not survive, rebuild a working client from screenshots, video and docs,
   keeping the original's labels, layout, keyboard shortcuts and timings, with a real local backend
   so every feature works (create, edit, search, delete, settings), not a clickable picture.
4. Replace dead integrations (sign-in, sharing to other services) with local stand-ins that look the
   same and are labelled as stand-ins in the README.

### Games (Flash, browser and mobile games)

1. **Usually no game file survives → rebuild it from the traces, fully.**
   - Art and sound: recovered from footage, GIFs and rips (above), at the original resolution, scaled
     up crisply (`image-rendering: pixelated` for pixel art).
   - Stage: the original size and aspect (measure it from footage; Flash games were often 550x400 or
     640x480; mobile games had the device's logical resolution).
   - Behaviour: implement every rule in the Codex behaviour spec with the measured numbers. Unknown
     constants get a `[DESIGN]` value tuned until the rebuild's motion matches the footage (track the
     same object in both and compare).
   - The whole game (law 6): title, menus, instructions, every mode and level the evidence shows,
     scoring, lives, game over, high scores (saved in localStorage), pause, sound toggle, and the
     original's controls. Words and dictionary games need the real word list or the closest period
     list; card and board games need the full rules.
2. **The SWF survives → run it.** Ship the original SWF with a self-hosted Ruffle
   (`npm pack @ruffle-rs/ruffle`, or a pinned release copied into `app/vendor/ruffle/`) and a page
   sized to the original stage. Check it in a browser: some AS3 features are still missing in Ruffle.
   `$S/shots.py swf <file or folder> --steps ...` runs it in a pinned Ruffle, muted, and drives it
   (click, type, key, wait, shot), listing any files the movie asked for that the folder lacks.
3. **Ruffle can't run it → port it.**
   - `inspect_swf.py <swf> --section actions` gives the logic; `digest` gives the constants.
   - `--extract app/original/assets` saves the original sounds and bitmaps.
   - Vector art: export with JPEXS FFDec (`ffdec -export shape,sprite,image,sound,font app/original/ffdec
     file.swf`) if installed, or capture each sprite from Ruffle frames.
   - Reimplement the scripts line for line with the same constants, frame rate and stage size.
4. **A sibling survives** (the single-player version of a lost multiplayer game, a sequel, a port to
   another platform): use its files for what the two share, checked against footage of the lost one,
   and rebuild what differs from traces.
5. Expose hooks for verification behind `?test=1`: `window.__RESURRECT__ = { score, state, pause(),
   step(n) }`. A live loop keeps running between steps, so measure inside one `eval`. For a Ruffle-hosted
   original, verify by playing it (`shots.py play` steps) rather than through hooks.
6. Keep the original controls; add touch only as an extra.

### Word, trivia and puzzle content

- Dictionaries: the original's list if any trace holds it (a rip, an app package, a fan dump); else a
  period-appropriate public list (ENABLE, SOWPODS/Collins only if licensed, the system list), plus every
  word the footage shows accepted, and minus every word it shows rejected. Record which list and why.
- Puzzle sets, questions and levels: transcribe every one that footage or guides show, verbatim; fill the
  rest in the same style and mark them `[DESIGN]`.

### Friends, leaderboards and other players

Social games showed real friends, ranks and names. Keep the panels, their layout and their behaviour
(weekly/monthly/all-time tabs, challenge buttons, invites) and fill them with fictional players and
plausible scores; the local player's own results go in for real. Never reuse a real name, photo or
avatar from footage (law 3).

### Text, command-line and terminal games (mainframe, PLATO, BBS doors, MUDs, early adventures)

1. **Look for the program first**: magazine and book type-in listings (BASIC games were published as
   source: `archive_search.py` for the magazine, `pages` to read the listing, then transcribe it exactly;
   that is the original program), bitsavers and university tape archives, Usenet source groups
   (comp.sources.games), PLATO archives, BBS file lists and CD-ROM shareware collections (Discmaster).
   A surviving disk image of it, or of a sibling from the same publisher, reads with `inspect_app.py a2`
   (Apple II: catalog, BASIC listings, text files, hi-res screens as PNG).
2. **Otherwise rebuild from what players saw**: play transcripts (Usenet, magazines, walkthroughs,
   "let's play" threads), manuals and help text, maps, hint books, reviews. Room descriptions, messages and
   responses quoted anywhere are `seen` and go in verbatim, typos and capitals included.
3. **Model the world from the evidence**: the map (rooms and exits from transcripts and maps), objects and
   where they start, the parser's vocabulary (every verb and noun anyone typed and what it answered,
   including the error messages: "I DON'T KNOW HOW TO..."), scoring, the turn or time limits, the death and
   winning messages. Anything no source shows is a marked `[DESIGN]` choice in the original's voice, or
   left out and named.
4. **Write it the way it was written, and run it where it ran.** If the original was a BASIC program
   (Applesoft, Microsoft, Atari, Commodore, TRS-80) or another period language, write the reconstruction
   in that language, in the period's style (line numbers, two-word parser, DATA statements for rooms), and
   run it on an in-browser emulator or interpreter of that machine (apple2js, jsbeeb, 8bitworkshop, or a
   small interpreter written for it), so that breaking into it and typing LIST shows a program like the
   original. Publishers' notes on whether disks were protected or listable matter here.
5. **Present it as it was**: the machine's screen (40x24 or 80x24, its font, uppercase-only where the
   machine was, inverse text, the flashing cursor, the prompt: `]` for Applesoft, `?` for INPUT, `>` for
   many adventures), and pacing (a teletype or 300/1200-baud print speed is part of the feel). A stdlib
   Python version for the command line can come too. Journeys replay the surviving transcripts and
   walkthroughs command by command and compare the responses.
6. **Reading printed maps and walkthroughs**: rotate scanned pages until the text reads upright; arrows
   on map lines mean one-way passages; diagonal lines are NE/NW/SE/SW unless labelled; stairs and ladders
   are up/down. Record each reading as `seen` with the page (leaf) number, and each inference about a
   drawn line as `inferred`.

### Desktop and mobile apps

1. **An installer or disk image survives → run it in an in-browser emulator** (v86, DOSBox in the
   browser, Basilisk/SheepShaver for classic Mac, an Android emulator for APKs) with the original files.
   Never run it natively.
2. Otherwise rebuild it as a working app, not a set of screenshots: the manual (archive_search
   `inside`, `pages`), reviews and demo videos list every screen, menu, setting and gesture; frames
   and audio give timing and sound; screenshots give layout and colours. Use original icons, art and
   sounds wherever they can be recovered. It needs a real data model and persistence (localStorage or
   IndexedDB), every interaction the evidence shows (gestures with their thresholds and animations,
   keyboard shortcuts, settings that take effect), empty, loading and error states, and stand-ins for
   dead services that behave like them (sync, sign-in, sharing), labelled as stand-ins.

### When the makers released the source (native apps)

Sometimes the makers publish the code after shutdown (`community.py github "<name>" --makers "<people,
studio>"` flags it as an official release). It usually cannot be built or installed today, so the thing
is still gone for its users, but the code is the best evidence there is. Mine it rather than guessing:
- **Theme and constants:** theme plists or JSON (colours, fonts, sizes, timings, thresholds), and
  `#define`/`static let` values near gesture and animation code.
- **Strings:** `Localizable.strings` and string literals give every label and alert verbatim.
- **Assets:** the asset catalogue (`*.xcassets`, `res/`) holds the icons, images and launch art: original files.
- **Behaviour:** gesture recognisers, animation blocks and view controllers give the exact rules; port
  them line for line into the web rebuild, citing file and line in the ledger.
- **Data:** the storage schema (SQL, Core Data model) gives the fields and ordering rules.
Respect the licence (record it as `legal` evidence; keep notices). Fonts and third-party assets are often
excluded from the release: see the font rule below.

### Fonts

Use the original font when it can be recovered (an archived webfont, a font file in the package or source).
Commercial fonts (H&FJ, Monotype, ...) stay **local only**: list them in the README and MANIFEST as
licensed, never publish or share them, and give the CSS a close fallback so the app still works without
them. In share-safe mode, use the fallback only.

### Customisation ecosystems

When people wrote their own code against the original (profile layouts, skins, themes, user styles,
mods, bookmarklets), the class names, ids, element structure, doctype or quirks mode, and URL and
parameter shapes are part of what must come back exactly. Restoring the original pages keeps them
for free. Add a journey that applies an era-style customisation and checks that it takes effect.
Sanitise what users paste (no scripts, handlers or `javascript:` URLs) and render it sandboxed.

## Verification hooks and fixtures

- Seeded randomness (`?seed=`) and test controls behind `?test=1`.
- Camera and microphone: a generated fallback source, plus `verify.py run --fake-media`.
- Timers: journeys with `"clock": true` and `advance` steps.
- Mark journeys that set state through hooks with `"via": "hooks"`.

## Workflow per feature

1. Confirm its journey(s) exist in `codex/journeys.json`.
2. Restore it (run → port → reconstruct).
3. `verify.py run --serve app`; fix until green; five attempts max, then leave it `built` and say why
   in `codex/LEFT-OUT.md` (`verify.py` marks it `broken` on its own when its journey fails).
4. `verify.py feature F3 --status built`; never write `verified` by hand.
5. Commit if the project uses git.

## README for the restoration

- The original's name and one line on what it was.
- How to run it.
- The scoreboard with the original share.
- Which parts are original files, ported, or reconstructed (from the manifest).
- How it was recovered (evidence counts, link to the museum page).
- The rights notice: original material belongs to its owners and is restored for preservation and
  personal use; ask before redistributing, or use share-safe mode.
- Credits to the archives and communities.
