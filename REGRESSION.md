# Regression test

A two-turn exchange that exercises every failure mode found during development.
Run it after any change to the prompt, the extraction, or after a Claude
Desktop update breaks and you re-map the UI labels.

## Setup

Fresh Claude chat, plainclaude running, model warm (`ollama ps` shows it
loaded — the first call after idle pays ~20 s of load time).

## Turn 1

> Derive the Kelly criterion bet fraction from expected log wealth. Keep it
> under 250 words.

## Turn 2

> What happens to my growth rate if I bet 2f* instead?

Claude's answer to turn 2 is reliably dense: symbols defined one turn earlier,
display math, a numeric table, usually a thinking block. That density is the
test.

## Pass criteria (check the turn-2 rewrite)

1. **Symbols section** at the top, one line per symbol, covering every symbol
   used in the rewrite body (f, f*, p, q, b, g, and whatever else appears) —
   including ones only defined in turn 1. No invented definitions: a symbol
   defined nowhere must say "not defined in the conversation".
2. **Formulas byte-faithful.** Compare each equation against the original
   character by character. Watch for silent algebraic substitution (the model
   replacing an expression with an equivalent form) — equivalent is not
   faithful, and the same habit produces sign/operator corruption.
3. **Tables intact and in place**: rendered with borders, every row, column,
   and value present, positioned where the original puts them — not at the
   end, not restated as bullets, not rebuilt as a LaTeX array.
4. **No leaked UI**: no [Claude activity: ...] bracket text, no composer or
   button labels, no "Claude is AI and can make mistakes".
5. **No red text**: a red block is a KaTeX parse failure (historically:
   unescaped % inside math).
6. **Actions attributed**: if the original ran a command or benchmark, the
   rewrite must say so — measured claims must not become bare assertions.
7. **Exactly one rewrite** per completed response: nothing fires while Claude
   is streaming or between tool-call pauses.

## Code-path check (separate, any coding conversation)

Ask Claude for a response that is mostly a code block. The rewrite must
describe the code (purpose, structure, key parameters) rather than reproduce
or line-by-line rewrite it; surrounding prose still gets the full treatment.
Two additional checks: the rewrite must NOT state or estimate what the code
outputs when run (unexecuted code has no results — inventing them is the
fabrication failure class), and there must be no Symbols section listing code
identifiers — Symbols is for mathematical notation only, omitted entirely
when the response has no math.

## Debugging order when something fails

`--dump` shows the exact prompt the model received — if the defect is already
present there (missing table syntax, leaked buttons, garbled structure), the
bug is in extraction; fix it in code. If the dump is clean and the rewrite is
wrong, the bug is model behavior; sharpen the prompt first, and reach for
deterministic post-processing only when a *sharpened, explicit* instruction
has failed — indirection (placeholder tokens) tested worse than verbatim-copy
instructions with this model class.
