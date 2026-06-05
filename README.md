# slm_cli

**A "CLI for SLMs": a tolerant verb-target command interpreter that drives actions from the raw text output of a small, local language model.**

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![Tests](https://img.shields.io/badge/tests-69%20passing-brightgreen)

A single file, zero external dependencies, drop it in a folder and prototype an agent in five minutes — home automation, robotics, virtual assistant, system automation. The model writes readable tags instead of strict JSON:

```
[CMD: ACTION | TARGET | QUANTITY]
```

…and `slm_cli` parses them tolerantly, types them cleanly, runs the associated handlers, and returns a re-injectable error prompt whenever something fails.

---

## Why not JSON?

Small models (1B or less) frequently break strict JSON: missing quotes, trailing commas, unclosed braces. You *can* constrain decoding (GBNF, Ollama structured outputs), but that isn't always available and it weighs down the stack.

`slm_cli` takes the other path: **let the model write, then repair.** A `[CMD: ...]` tag is trivial for any model to produce, and the parser absorbs the noise around it (prose, typos, casing, a forgotten bracket, a repeated tag, Markdown numbering).

> **Design honesty.** A "parse after the fact" approach repairs what the model emitted; it does not *prevent* aberrant output the way constrained decoding does. Its robustness ceiling is therefore structurally a bit lower. In exchange: universal, frugal, readable, and independent of the inference engine. It's a deliberate ergonomics/portability trade-off.

---

## Features

- **Zero dependencies.** Pure standard library (`re`, `difflib`, `inspect`, `dataclasses`, `asyncio`). Fuzzy matching relies on `difflib.get_close_matches`.
- **`@command` decorator** to register a handler and its argument schema.
- **Two-step matching:** exact lookup first, then fuzzy with an adjustable confidence threshold — to avoid ridiculous false positives.
- **Clean argument typing:** the target stays a string as-is (the digits in a file name are *not* swallowed), the quantity is cast to an integer only from its dedicated field.
- **Named, typed parameters (optional):** a command can declare a `key=value` schema with `Str / Int / Float / Bool / Enum`, with required fields, defaults, and fuzzy-matched keys.
- **Ambiguity guard:** when two similar action names tie under fuzzy matching (`create_task` vs `create_ticket`), the router refuses and asks, instead of silently guessing wrong.
- **Self-correction loop** covering **parsing**, **typing** *and* **execution** failures (a handler that raises an exception feeds the retry).
- **Async-first**, with synchronous handlers supported.
- **`stop_on_error`** to halt a command sequence at the first failure (a halt primitive, not a rollback).
- **Optional Markdown cleanup** of the text "spoken" by the agent.
- **Auto-generated grammar** for the system prompt: a single source of truth, so the parser and the documentation sent to the model never drift apart.

---

## Installation

The core of the project is a single file. Two options:

**1. As a real package (recommended for development)**

```bash
git clone https://github.com/<your-account>/slm_cli.git
cd slm_cli
pip install -e .
```

**2. As a drop-in**

Just copy `slm_cli.py` into your project. No installation required.

Python ≥ 3.8.

---

## Quickstart

```python
import asyncio
from slm_cli import CommandRouter, Vocab, FreeText

router = CommandRouter(fuzzy_threshold=0.78)

@router.command(name="TURN_ON", aliases=["activate", "enable"],
                target=Vocab({"living_room_light": ["lamp", "living room"]}))
async def turn_on(target):
    # ... your real logic (MQTT, home-automation API, GPIO...) ...
    return f"Turned on: {target}"

@router.command(name="READ_FILE", aliases=["read", "open"], target=FreeText())
async def read_file(target):
    with open(target) as f:        # a FileNotFoundError goes to repair
        return f.read()

async def main():
    slm_output = "Sure thing! [CMD: activate | lamp]"
    res = await router.run(slm_output)
    print(res.outputs)             # ['Turned on: living_room_light']

asyncio.run(main())
```

---

## The grammar

The contract sent to the model fits on one line:

```
[CMD: ACTION | TARGET | QUANTITY]
```

- **ACTION** — a verb (or one of its aliases). Case-insensitive, typo-tolerant.
- **TARGET** — depending on the command's schema: a closed vocabulary (`Vocab`) or free text (`FreeText`). Optional if the command expects none.
- **QUANTITY** — an optional integer, only if the command declares `quantity=True`.

Tolerated without complaint: surrounding prose, a forgotten closing bracket (`[CMD: STOP`), alternative keywords (`COMMAND`, `ACTION`), stutter repetition (`[X | CMD: X | target]`), numbered Markdown lists, and thought (`[THOUGHT] ...`) or speech (`[REPLY] ...`) blocks.

A `FreeText` target (without a quantity) is taken **verbatim** after the verb, so it may contain `|` (URLs with query strings, regexes, formulas). Inside a *closed* bracket the value may also span **multiple lines** (code, JSON, paragraphs) — `[CMD: WRITE | line1\nline2]`. The only character a value cannot contain is a literal square bracket `[` or `]`, since those are the delimiters.

---

## Concepts

### The `@command` decorator

```python
@router.command(
    name="SET_TEMPERATURE",         # canonical name (upper-cased)
    aliases=["set_temp", "set_heat"],# accepted synonyms
    target=Vocab({...}),            # target schema (or FreeText(), or None)
    quantity=True,                  # does the command expect an integer?
    help="Set the temperature.",    # description (also feeds the auto-generated grammar)
)
async def set_temperature(target, quantity=1):
    ...
```

The handler is returned **untouched**: it stays callable normally elsewhere in your code. `slm_cli` only inspects its signature at dispatch time to pass it only the arguments it accepts (`target`, `quantity`).

> Verbs are matched as a single token (the first word of the command), so use single-word aliases (`switch_on`, not `switch on`).

### `Vocab` vs `FreeText`

`Vocab` constrains the target to a closed vocabulary and maps aliases to a canonical form:

```python
Vocab({
    "printer": ["print", "printing"],     # "print" -> "printer"
    "heater":  ["heating", "radiator"],
})
```

`FreeText` accepts any string (file name, search query, person's name) and returns it as-is.

### Two-step matching and the confidence threshold

For each target (and each verb), `slm_cli` first tries an **exact lookup**, then and only then a **thresholded fuzzy match**. The default threshold, `fuzzy_threshold=0.78`, is calibrated to separate genuine typos from substring collisions:

| Input | Target | `difflib` ratio | Result at 0.78 |
|---|---|---|---|
| `printr` | `printer` | ~0.92 | ✅ accepted (typo) |
| `heatr` | `heater` | ~0.91 | ✅ accepted |
| `doorbell` | `door` | ~0.67 | ❌ rejected (different word) |

The verb threshold can be tuned separately via `action_fuzzy_threshold`.

### Argument typing

This is the sensitive part, handled explicitly: **digits are never torn out of the target.** The target/quantity split follows the declared schema, not a greedy regex. Consequences:

```python
"[CMD: READ_FILE | report_2024_Q3.txt]"    # target = "report_2024_Q3.txt" (intact)
"[CMD: SET_TEMPERATURE | office | 21]"      # target = "office", quantity = 21 (int)
"[CMD: SET_TEMPERATURE | office | very hot]"  # -> invalid_quantity error
```

---

## Named, typed parameters (optional)

The positional `ACTION | TARGET | QUANTITY` form is the default. When a command needs more than that — several arguments, each with its own type — declare a `params` schema and the parser switches to `key=value` mode for that command:

```python
from slm_cli import CommandRouter, Str, Int, Float, Bool, Enum

router = CommandRouter()

@router.command(name="SEND_EMAIL", params={
    "to":       Enum(["boss", "team", "john"], required=True),
    "subject":  Str(required=True),
    "priority": Int(default=3),
    "urgent":   Bool(default=False),
})
async def send_email(to, subject, priority=3, urgent=False):
    ...
```

The model then writes:

```
[CMD: SEND_EMAIL | to=boss | subject=Quarterly review | urgent=yes]
```

What the parser does, tolerantly:

- **Keys are fuzzy-matched** too — `subjct=...` resolves to `subject`.
- **Values are coerced and validated** per type: `Int`/`Float` parse the number, `Bool` accepts `true/yes/1/on` and `false/no/0/off`, `Enum` resolves aliases and typos like a `Vocab`. A bad value yields an `invalid_param` error; an unknown key yields `unknown_param`; a missing `required=True` field yields `missing_param` — all of which feed the same repair loop.
- **Values may contain spaces** (`subject=Quarterly review`); a value is everything up to the next `|`.
- **Defaults** are filled in; the handler receives each parameter as a keyword argument (a `**kwargs` handler also works).

Types accept `required=False` and `default=None`. Use the positional form for simple verbs; reach for named parameters only when a command genuinely has several typed fields.

---

## Self-repair

When a command fails, the result carries a `repair_prompt`: a formatted error message, ready to be re-injected as-is into the model's prompt so it can correct itself.

### Loop mode (string)

```python
async def call_the_slm(repair_prompt, attempt):
    return await my_slm(repair_prompt)       # returns the new text

res = await router.run(slm_output, retry=call_the_slm, max_retries=2)
```

`run()` executes the valid commands, sends the failures to repair, and accumulates outputs across attempts.

### Exception mode

```python
from slm_cli import RepairNeeded

try:
    res = router.parse(slm_output).raise_for_status()
except RepairNeeded as exc:
    prompt = exc.result.repair_prompt
    # ... re-run the model with `prompt` ...
```

### Execution Repair

The key V2 addition: if **parsing succeeds** but the handler **fails physically** (a `FileNotFoundError`, an API returning 404…), the exception is intercepted and turned into an `execution_error`, which feeds the **same** repair loop. The model then receives:

```
[!] EXECUTION FAILED - the format was correct, but the action failed.
Fix the target, or choose another action to reach the goal.

  - Command 'READ_FILE | secret.txt' failed during execution: File not found: secret.txt
```

…and can try another target or another strategy. To bypass the interception (debugging, or a critical non-replayable action): `dispatch(result, raise_on_error=True)`.

---

## Async-first

`dispatch()` and `run()` are coroutines. `async def` handlers are awaited, synchronous handlers are called directly — the two coexist without ceremony. The `retry` callback you supply can itself be synchronous or asynchronous.

```python
@router.command(name="TURN_ON", target=devices)
async def turn_on(target):          # async: network I/O, MQTT...
    await client.publish(...)
    return "ok"

@router.command(name="WAIT", quantity=True)
def wait(quantity=1):               # sync: no problem
    return f"{quantity}s"
```

---

## Cleaning up the "spoken" text

SLMs often format their reply in bold without being asked. `strip_markdown` (on by default) cleans the `message` field:

```python
router = CommandRouter(strip_markdown=True)      # default
router.parse(text).message                       # **bold**, `code`, ### headings removed
router.parse(text, strip_markdown=False).message # raw text preserved
```

The `message` is extracted on a best-effort basis: an explicit `[REPLY]` / `[SPEECH]` section is preferred; otherwise, commands and thought blocks are stripped out.

---

## Auto-generated grammar

The registry documents itself. `system_prompt_block()` generates the grammar block to paste into your system prompt, derived from the **same registry that parses** — so the documentation sent to the model can never drift apart from the parser.

```python
print(router.system_prompt_block())
```

```
AVAILABLE COMMANDS
To act, insert one or more tags in the format: [CMD: ACTION | TARGET]

  [CMD: TURN_ON | <target>]
      targets: living_room_light, heater, tv
      -> Turn a device on.
  [CMD: SET_TEMPERATURE | <target> | <n>]
      targets: living_room, bedroom, kitchen
      -> Set the temperature.
```

---

## Demo and tests

```bash
# Full demo (a home-automation agent fed a hallucinated SLM output)
pip install -e .
python examples/smart_home_demo.py

# Test suite
pip install pytest
pytest -q
```

---

## Known limitations

Honest boundaries to keep in mind:

1. **Two argument *shapes*, not arbitrary nesting.** A command is either positional (`verb + target + optional integer`) or named (`key=value` typed parameters). That covers the vast majority of agent actions, including multi-field ones like `SEND_EMAIL | to=… | subject=…`. What it does *not* model is nested or recursive structures (a list of objects, a tree) — for those, pass a single `FreeText`/`Str` payload (JSON, say) and parse it in your handler.
2. **`FreeText` cannot be validated.** By nature, a hallucinated file name or query passes through as-is. This is intentional; validation, if you want it, happens inside your handler (and a failure goes to execution repair).
3. **A value cannot contain literal `[` or `]`.** Those are the delimiters. `|` and newlines inside a closed bracket are fine; square brackets are not (no escaping mechanism, by design).
4. **Message extraction is heuristic.** Telling "thought" from "speech" without an explicit tag is intrinsically ambiguous, and an explicit `[REPLY]` section ends at the next bracket. With a clear `[REPLY]`/`[SPEECH]` tag it's reliable; otherwise it's a best effort. It's the least robust link, and it only affects the cosmetic `message` field — never the parsed commands.
5. **Execution is not atomic.** `run()` dispatches valid commands immediately and only sends failures to repair; outputs accumulate. The prompt insists the model re-emit *only* the corrected commands, but a model that re-emits everything could re-trigger an action that already ran. **For non-idempotent, sensitive actions** (a payment, a bulk send, unlocking a door), use `run(..., execute=False)` to inspect before dispatching yourself, or add an idempotency guard in the handler.

**Performance.** `difflib` runs only on a *miss* (exact matches are an O(1) dict lookup), which is fine for hundreds of entries. For a vocabulary with thousands of aliases, pass `Vocab(mapping, fuzzy=False)` to keep exact-only matching for that target, or pre-filter before delegating to the router.

---

## API reference (summary)

| Element | Role |
|---|---|
| `CommandRouter(fuzzy_threshold=0.78, action_fuzzy_threshold=None, require_command=False, strip_markdown=True, ambiguity_margin=0.08)` | Registry + engine. |
| `@router.command(name, aliases, target, quantity, help, params)` | Register a handler (positional or, via `params`, named). |
| `router.parse(text, strip_markdown=None) -> ParseResult` | Raw text → commands + errors + message. Synchronous, runs nothing. |
| `await router.dispatch(result, raise_on_error=False, stop_on_error=False) -> list` | Execute the valid handlers (async/sync). |
| `await router.run(text, retry=None, max_retries=2, raise_on_fail=False, execute=True, stop_on_error=False) -> ParseResult` | Loop: parse → execute → repair. |
| `router.system_prompt_block() -> str` | Grammar for the system prompt. |
| `Vocab(mapping, fuzzy=True)` / `FreeText()` | Target schemas. |
| `Str / Int / Float / Bool / Enum` | Named-parameter types (each takes `required=`, `default=`). |
| `ParseResult` | `.ok`, `.needs_repair`, `.repair_prompt`, `.commands`, `.errors`, `.outputs`, `.message`, `.raise_for_status()`. |
| `Command` | `.name`, `.target` (str\|None), `.quantity` (int), `.params` (dict), `.raw`. |
| `CommandError` | `.kind`, `.message`, `.raw`, `.command`, `.exception`. |
| `RepairNeeded(Exception)` | Raised by `raise_for_status()` / `run(raise_on_fail=True)`. |

`CommandError.kind` ∈ `unknown_command`, `ambiguous_command`, `missing_target`, `unknown_target`, `invalid_quantity`, `no_command`, `execution_error`, `malformed_param`, `unknown_param`, `invalid_param`, `missing_param`.

**Namespaces** come for free: name a command `FILE.OPEN` or `MAIL.SEND` and use it as `[CMD: FILE.OPEN | …]`. The dot is just part of the name.

---

## License

MIT. See [LICENSE](LICENSE).
