# CLAUDE.md — All-4-url (Replot)

Read `AGENT.md` first. It is the canonical briefing for this repo — the
working contract with moon, the architecture, the hard lessons, and the
do-not-build list. Everything below is Claude-specific; anything that
conflicts with `AGENT.md` loses to `AGENT.md`.

## How moon uses Claude here

Moon consults Claude for a second pair of eyes and for fixes, then relays
your work to his assistant as `.patch` files. He may also paste your
suggestions or your code into the chat for triage. Keep that in mind:

- **Patches must apply cleanly and compile.** Moon's assistant
  compile-checks and tests every patch before it ships; a patch that
  doesn't apply or doesn't compile wastes his round-trip. Test your diff
  against the current `main` before handing it over (`python -m py_compile`
  on touched modules, `node --check` on any page `<script>` you changed).
- **Never claim a fix is verified** unless you actually ran the relevant
  test. "Should work" is not verified. Say what you tested and what you
  didn't.
- **Keep patches small and scoped.** One bug or one feature per patch.
  Moon picks numbered options; a patch that bundles adjacent improvements
  he didn't ask for will be rejected or split apart.
- **Plain words for moon, code for the patch.** He is not a developer:
  explain the change in plain language first, then give the patch.
- **No secrets in patches, messages, or code.** No tokens, cookie values,
  signed URLs, or `ndus` values — ever. If a fix needs a credential, say
  which env var it needs and let moon set it in Koyeb himself.
- **Do not push, deploy, or redeploy.** You don't have that access and
  moon doesn't share his Koyeb login. Ship = hand moon a clean patch.
- **Do not re-pitch rejected ideas.** `AGENT.md` lists the dead ones
  (batches of automated-income ideas, `/pipe` video mode, Mega, remux,
  subscription tokens, HF migration, and the old unpicked pitches). If he
  asks for fresh suggestions, offer a numbered pick-list and stop there.
