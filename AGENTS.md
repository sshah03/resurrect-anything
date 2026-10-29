# Notes for AI agents

## Installing resurrect for a user

If someone has asked you to install this skill, these are the steps. Tell them briefly what you're
about to do, then do it.

1. **Check the basics.** Run `python3 --version` (on Windows, `python --version`). It needs 3.9 or
   newer. If Python is missing or older, stop and tell them they can get it from
   https://www.python.org/downloads/.

2. **Install the plugin.** The repo is its own plugin marketplace:

   ```bash
   claude plugin marketplace add sshah03/resurrect-anything
   claude plugin install resurrect@resurrect
   ```

   If it's already installed, run `claude plugin marketplace update resurrect` and
   `claude plugin update resurrect@resurrect` instead.

3. **Check it works.** Run the skill's offline self-test from the installed copy:

   ```bash
   ROOT="$(claude plugin list --json | python3 -c "import json,sys; print(next(p['installPath'] for p in json.load(sys.stdin) if p['id'] == 'resurrect@resurrect'))")"
   python3 "$ROOT/skills/resurrect/scripts/selftest.py"
   ```

   On Windows, use `python` in place of `python3`. It should end with "N/N passed". Groups it skips
   for missing Pillow, ffmpeg or numpy are fine at this point. If anything fails, show them the error
   rather than carrying on.

4. **Offer the optional tools.** Games, apps and sites need them; recipes, rulesets and APIs don't.
   One command installs Pillow, numpy, Playwright with its Chromium, yt-dlp and an image upscaler
   into the skill's own environment in `~/.cache/resurrect` (about 400 MB), which the skill's scripts
   use by themselves. Ask before running it:

   ```bash
   python3 "$ROOT/skills/resurrect/scripts/enhance.py" setup
   ```

   Don't `pip install` these into their Python: Homebrew's and many Linux Pythons refuse it. Check
   `ffmpeg -version` too; if it's missing, tell them to install it with their package manager
   (`brew install ffmpeg` on a Mac, `sudo apt install ffmpeg` on Debian or Ubuntu).

5. **Tell them how to use it.** Keep it short:
   - Start a new Claude Code session first, so the plugin loads.
   - Type `/resurrect` and the thing, with any links or screenshots they have, for example
     `/resurrect "The Helicopter Game", the 2004 Flash game`.
   - A run takes hours and a lot of usage. It saves its work as it goes, so running it again
     picks up where it stopped.
   - It only runs when they type `/resurrect`.

To update later: `claude plugin marketplace update resurrect`, then
`claude plugin update resurrect@resurrect`. To uninstall: `claude plugin uninstall resurrect@resurrect`.

## Working on this repo

- The skill is in `skills/resurrect/`. `SKILL.md` is what Claude follows when someone types
  `/resurrect`. `references/` holds the briefs for the fresh builder, reviewer and fixer and the
  guides the research reads. `scripts/` holds the tools. `.claude-plugin/` makes the repo a plugin
  and its own marketplace; check it with `claude plugin validate .`.
- In SKILL.md and the references, refer to the skill's own files through `${CLAUDE_SKILL_DIR}`, never
  a fixed path.
- Don't add `disable-model-invocation: true` to SKILL.md. Cowork runs a typed skill by having Claude
  load it, and that setting forbids exactly that. The skill stays typed-only through its description
  and its "How it is started" paragraph.
- The scripts have to run on Python 3.9, and the ledger, archive and discussion scripts use only the
  standard library. After a change, run `python3 skills/resurrect/scripts/selftest.py` on 3.9 and on a
  current Python.
- A release bumps `version` in `.claude-plugin/plugin.json`; installed copies only update when it changes.
- GitHub Actions run the self-test on Linux, macOS and Windows (Python 3.9 and 3.14), with the optional tools,
  and from an installed copy of the plugin (`tests.yml`). `smoke.yml` starts a real `/resurrect` run with a
  Claude login and stops before research; it only runs when started by hand.
- Test runs hold archived originals, recordings and private baselines. They live in `test-runs/`,
  which is git-ignored, and are never committed.
- The skill was tuned by blind comparisons against plain Claude Code. Prefer a short principle with
  its reason over a new rule for one case.
- Write docs and comments in plain English: short sentences, no jargon where a normal word works.
