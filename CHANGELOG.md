# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and the project adheres to [Semantic Versioning](https://semver.org/).

## [2.2.0]

### Added
- **Named, typed parameters** (opt-in): a command may declare `params={name: type}` with
  `Str / Int / Float / Bool / Enum`, supporting `required=` and `default=`. The model writes
  `key=value` pairs; keys are fuzzy-matched, values are coerced and validated, and the handler
  receives them as keyword arguments. New error kinds: `malformed_param`, `unknown_param`,
  `invalid_param`, `missing_param`.
- **Fuzzy-ambiguity guard:** when a fuzzy ACTION match has a runner-up (a different command)
  within `ambiguity_margin` (default 0.08), the router refuses with `ambiguous_command` and
  lists the candidates instead of silently picking one. Set `ambiguity_margin=0` to disable.
- **`stop_on_error`** on `dispatch()` and `run()`: halt a turn's command sequence at the first
  failure. This is a halt primitive, not a rollback (commands already run are not undone).
- `Command.params` carries the parsed named arguments.

### Notes
- **Namespaced command names** (`FILE.OPEN`, `MAIL.SEND`) already work as a naming convention;
  documented and covered by tests.
- Backward-compatible: existing positional commands are unchanged.

## [2.1.0]

### Added
- **Multi-line values** inside a *closed* bracket: a `FreeText` target may now carry
  code, JSON or paragraphs spanning several lines (e.g. `[CMD: WRITE | line1\nline2]`).
- **Raw `FreeText` targets:** a `FreeText` target without a quantity is taken verbatim
  after the verb, so it may contain `|` (URLs with query strings, regexes, formulas).
- Threshold validation: `CommandRouter` raises `ValueError` at construction if
  `fuzzy_threshold` / `action_fuzzy_threshold` is outside `[0.0, 1.0]`.

### Fixed
- `_strip_markdown` no longer corrupts text containing `*` (e.g. `3 * 4 * 5`, the glob
  `*.py`): emphasis markers must now hug non-space text, and the blanket asterisk
  removal was dropped.
- `dispatch()` is now idempotent: calling it twice on the same result no longer
  duplicates `execution_error` entries or outputs.
- The spoken-message cleanup now drops only bracketed meta/keyword labels; real prose
  brackets such as `[1]` or `[important]` are preserved.

### Changed
- An unclosed bracket is now explicitly bounded by the end of its line, so a stray `]`
  later in the text can never swallow the rest of the document.
- The grammar string is defined once and reused by both `repair_prompt` and
  `system_prompt_block` (no more duplication).

## [2.0.0]

### Added
- **Execution Repair:** `dispatch()` intercepts exceptions raised by handlers and
  turns them into `execution_error` entries that feed the repair loop (the model
  receives the failure and can retry).
- **Async-first:** `dispatch()` and `run()` are coroutines; synchronous handlers
  remain supported, as does a synchronous or async `retry` callback.
- **`strip_markdown`:** optional (on by default) cleanup of bold, italics, code,
  and headings in the text "spoken" by the agent.
- `ParseResult.outputs`: the return values of successfully executed handlers.
- `dispatch(raise_on_error=True)` and `run(raise_on_fail=True, execute=False)`.

### Changed
- `dispatch()` is no longer synchronous: it must now be awaited (`await`).
- `repair_prompt` separates **format** failures from **execution** failures.

## [1.0.0]

### Added
- Verb-target command router with a `@command` decorator.
- Two-step resolution (exact then fuzzy via `difflib`) with an adjustable threshold.
- `Vocab` (closed vocabulary + aliases) and `FreeText` (free text) target schemas.
- Clean argument typing: target (string) and quantity (integer) separated without a
  greedy regex — the digits of a target are never swallowed.
- Self-correction loop returning a re-injectable `repair_prompt`, plus an exception
  variant via `raise_for_status()` / `RepairNeeded`.
- Tolerance: prose, typos, casing, forgotten bracket, stutter, Markdown numbering,
  thought and speech blocks.
- `system_prompt_block()`: grammar generation for the system prompt.
- Zero external dependencies (standard library only).
