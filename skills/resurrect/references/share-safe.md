# Share-safe mode (opt-in)

The default resurrection is a **restoration**: as exact as the surviving traces allow, built from
the original's own files wherever they survive. That is what personal use and preservation want.
It also means the result contains the owner's copyrighted code, art, audio, text and trademarks.

When the user wants something **to publish or distribute** (a public repo, a hosted site, a
shareable game), they can ask for share-safe mode (`ledger.py init ... --share-safe`). Then the
build follows clean-room rules instead:

- **Facts, not expression.** Learn numbers, sizes, timings, rules, layouts, labels and colours from the
  originals, but ship none of their code, art, outlines, audio, fonts, logos, packaging or prose.
- **Dirty room.** Program files (SWF, JS bundles, ROMs, disassemblies) are read only by a subagent that
  writes facts to the ledger (`subagent-briefs.md`, dirty-room brief). The builder never opens
  `dossier/originals/` or the dirty room's raw notes.
- **Own name and wordmark.** Give the rebuild a codename and its own wordmark. Never set it in the
  original's logo style, typeface or mascot (trade dress).
- **Characters.** Licensed characters (cartoon, anime, sports, film tie-ins) are replaced with original
  stand-ins that play the same role.
- **Recreated assets.** Redraw art in the same style (palette, proportions, silhouette), use system or
  open-licensed fonts that feel similar, and synthesise sounds to the measured character.
- **Notice.** The README and museum carry a not-affiliated notice.

A restoration can be converted later. `app/original/MANIFEST.json` lists every original file, so
share-safe mode replaces exactly those files with recreations and re-runs `verify.py` (the scores show how
much fidelity the swap cost).

None of this is legal advice. Trademark, copyright, terms-of-service and patent questions depend on
the material and the jurisdiction.
