# Briefs for the builder and the reviewer

Fill in the `<...>` parts and send the brief as written. Keep it this short: in blind comparisons a plain,
capable build from good material beat the same material built under a long process.

## Build brief

```
Bring back <name>, <one line: what it was, who made it, when> (it is gone: <how it went>). Rebuild it
as close to the real thing as possible, fully working. Personal use: keep the original name, art,
words and sounds.

A research pack is at <workspace>/pack/ (start with README.md): the scope, sourced facts and unknowns
(FACTS.md), the clearest reference for every screen, state or page (reference/), and pieces recovered
from the original (assets/, indexed in assets/INDEX.md: open only what you use). Use it as your
starting material, and research further wherever you need.

Build in <workspace>/app/ <as what: a web page / a browser game / a recipe in RECIPE.md / ...>.
- Everything the pack's scope lists is built. What you cannot build goes in app/LEFT-OUT.md with why.
- Use the pack's recovered pieces before making your own; what you make yourself is listed in
  app/README.md as reconstructed, and what you decide that no evidence settles is listed as your design.
- Real people's content stays out: keep the structure, fill it with obviously fictional entries.
  <If the person said the site is their own: its author's own details in the pack come back too.>
  Publicly named creators keep their names and credits, but nobody's e-mail address, phone number or
  home address is restored.
- Every control does what the original's did, visibly: no placeholder buttons, no tool that changes
  nothing. Every documented keyboard shortcut and right-click menu works.
- It works for anyone who opens it: in Chrome, Safari and Firefox (no browser-only feature without a
  fallback), from a fresh start with no saved data.
- Where people who made or used it describe how it worked (in FACTS.md), follow them over your own
  inference.
- Before you finish, use it the way a person would: every screen, control and transition, beside the
  references, and open anything it produces (an export, a save, a print). Batch these checks into a few
  scripts rather than many small steps, and mute them: a headless browser can play through the user's
  speakers, so check sound by what starts and when, not by playing it aloud.
- For your own scripts that need Pillow, numpy or Playwright, use <the environment's Python, as `init`
  printed it> (the default python3 may lack them).
<Test servers: port and cleanup. Paths it must not read, if any.>

Reply with what you built, what came from the pack vs. what you made, what is still unknown, and the
entry file.
```

## Review brief

```
You know <name> from the evidence in <workspace>/pack/ (start with README.md; the references, facts and
recovered pieces), and from your own search: the pack may have missed sources, so look for the ones it
lacks (first-hand accounts by people who made or used it, other recordings, archived copies of blocked
pages) before judging. The rebuild is in <workspace>/app/ (entry: <file>); serve it from inside your
check scripts on port <port>, starting and stopping the server there, so nothing outside can stop it
mid-review (or: the recipe is at <path>).

<Use it / play it / read it> the way someone who knew the original would: every screen, control,
shortcut, right-click menu, transition and sound, and anything it produces (or, for a recipe:
ingredients, method, look, taste and texture as food science predicts them). Do not read its source code, notes or README; judge only what it shows and does.

List the differences that person would notice, most noticeable first: look (art, lettering, layout,
colours, sharpness), behaviour, timing, transitions between screens, sound, anything missing, anything
invented that the evidence contradicts, any control that does nothing, anything that is broken, and any
real person's content or contact details (names and credits of publicly named creators are fine<, and
so are the author's own details: the site is the person's own>). For each: what the evidence shows
(file name, timestamp or URL) and what the rebuild does. Say which findings come from sources the pack
lacks, so they can be added to it. Leave out what the evidence cannot settle.
Batch your checks into a few scripts (muted: judge sound by what starts and when, not aloud), and stop at
the 15 most noticeable differences. Under 500 words. For your own scripts that need Pillow, numpy or
Playwright, use <the environment's Python, as `init` printed it> (the default python3 may lack them).
Start no servers you leave running.
```

## Fix brief

Add the reviewer's new sources to the pack (and the ledger) first. Then start a fresh agent with:

```
A rebuild of <name> is in <workspace>/app/ (start with app/README.md for how it is laid out). A reviewer
who knows the original from the research pack at <workspace>/pack/ and its own search found these
differences (its new sources are in pack/REVIEW-SOURCES.md):

<the reviewer's list, as written>

Fix each one the evidence settles: open only the files and references each fix needs, check the fix
beside its reference, and keep everything else working (use the screens and controls you touched the
way a person would). Where sources conflict, say which you followed and why. Update app/README.md and
app/LEFT-OUT.md to match. For your own scripts that need Pillow, numpy or Playwright, use <the
environment's Python, as `init` printed it> (the default python3 may lack them). <Test servers: port and
cleanup. Paths it must not read, if any.>

Reply with what you changed and what you left (and why).
```
