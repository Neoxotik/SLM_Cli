"""
slm_cli.py  —  v2.2 (Production Ready)
======================================
A zero-dependency, ultra-tolerant Command Router & Parser designed for Small Language Models (SLMs).
Replaces fragile JSON Function Calling with a robust Verb-Target CLI approach.
Contract with the model:
    [CMD: ACTION | TARGET | QUANTITY]
Features:
- Execution Repair: Intercepts physical failures (e.g., FileNotFoundError) and feeds them back to the SLM.
- Async-First: Native support for `async def` and `def` handlers.
- Two-Step Fuzzy Matching: Extracts intentions even with typos or surrounding prose.
- Zero External Dependencies: Only uses Python's standard library (re, difflib, inspect, asyncio).
License: MIT
"""
from __future__ import annotations

import re
import inspect
from dataclasses import dataclass, field
from difflib import get_close_matches, SequenceMatcher
from typing import Callable, Optional, Dict, List, Tuple, Any

__all__ = [
    "CommandRouter", "Vocab", "FreeText",
    "Str", "Int", "Float", "Bool", "Enum",
    "Command", "CommandError", "ParseResult", "RepairNeeded",
]


# ==============================================================================
# 0. CONSTANTS & PATTERNS (compiled once)
# ==============================================================================

_KEYWORDS = r"(?:CMD|COMMAND|ACTION)"

# Section labels that must NEVER be treated as commands.
_THINK_TAGS = ["THOUGHT", "REASONING", "PLAN", "INTERNAL", "THINK", "THINKING", "ANALYSIS"]
_SAY_TAGS = ["SPEECH", "SAY", "REPLY", "MESSAGE", "RESPONSE"]
_OTHER_META = ["NOTE", "OBSERVATION", "OBS", "DEBUG", "INFO"]
_META_ALL = {w.upper() for w in (_THINK_TAGS + _SAY_TAGS + _OTHER_META)}


def _alt(words: List[str]) -> str:
    """Build a regex alternation `(?:A|B|C)` (longest first)."""
    uniq = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    return "(?:" + "|".join(uniq) + ")"


# Candidate tag in brackets. Two forms:
#   - CLOSED  "[ ... ]"  -> the body MAY span newlines, so a FreeText target can
#     carry code, JSON or paragraphs. (A body still may not contain '[' or ']'.)
#   - UNCLOSED "[CMD: X"  -> no closing bracket: bounded by end of line, so a stray
#     bracket can never swallow the rest of the document (preserves stutter tolerance).
_BRACKET_RE = re.compile(
    r"\[\s*(?P<body>[^\[\]]+?)\s*\]"
    r"|"
    r"\[\s*(?P<body_open>[^\[\]\n]+?)\s*(?=\n|$)",
    re.UNICODE,
)

# Non-bracketed command: must START a line (otherwise the word "action" inside a
# normal sentence -- "my plan of action:" -- would be read as a command).
# Separators are [ \t], never \s, so a match never spans a newline.
_LINE_RE = re.compile(
    rf"(?m)^(?P<full>[ \t]*{_KEYWORDS}\b[ \t]*[:|][ \t]*(?P<body>[^\n\]]+))",
    re.IGNORECASE | re.UNICODE,
)

_KEYWORD_LEAD_RE = re.compile(rf"^\s*{_KEYWORDS}\b\s*[:|]?\s*", re.IGNORECASE | re.UNICODE)

_THOUGHT_BLOCK_RE = re.compile(
    rf"\[{_alt(_THINK_TAGS)}\b[^\]]*\]\s*.*?(?=\n\s*\n|\n?\[|$)",
    re.IGNORECASE | re.DOTALL | re.UNICODE,
)
_SAY_RE = re.compile(
    rf"\[{_alt(_SAY_TAGS)}\b[^\]]*\]\s*(?P<say>.*?)(?=\n\s*\n|\n?\[|$)",
    re.IGNORECASE | re.DOTALL | re.UNICODE,
)

# Integer extracted ONLY from the quantity field (never from the target).
_INT_RE = re.compile(r"-?\d+")

# Canonical grammar hint, the single source used by BOTH the repair prompt and the
# auto-generated system-prompt block (so the two can never drift apart).
_GRAMMAR_HINT = "[CMD: ACTION | TARGET]"

# A bracketed label, used by the message extractor to drop leftover meta/keyword tags.
_LABEL_RE = re.compile(r"\[(?P<inner>[^\[\]\n]{0,60})\]", re.UNICODE)


def _strip_meta_label(m) -> str:
    """Remove a bracketed label ONLY if it is a meta tag/keyword; keep real prose
    such as "[1]" or "[important]"."""
    inner = m.group("inner").strip()
    if not inner:
        return ""
    first = re.split(r"[\s:|]+", inner, maxsplit=1)[0].upper()
    if first in _META_ALL or re.fullmatch(_KEYWORDS, first):
        return ""
    return m.group(0)


# ==============================================================================
# Small async helper: await if awaitable, otherwise return as-is.
# ==============================================================================
async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# ==============================================================================
# 1. TARGET SCHEMAS (a command's valid "string" type)
# ==============================================================================

class Vocab:
    """Target constrained to a closed vocabulary. Exact resolution, then fuzzy."""

    def __init__(self, mapping: Dict[str, List[str]], fuzzy: bool = True):
        self.canonicals: List[str] = list(mapping.keys())
        self.fuzzy = fuzzy
        self._lookup: Dict[str, str] = {}
        for canon, aliases in mapping.items():
            self._lookup[canon.lower()] = canon
            for a in aliases:
                self._lookup[a.lower()] = canon
        self._keys: List[str] = list(self._lookup.keys())

    def resolve(self, raw: str, threshold: float) -> Optional[str]:
        if not raw:
            return None
        key = raw.strip().lower()
        if key in self._lookup:                    # 1) exact
            return self._lookup[key]
        if self.fuzzy:                             # 2) thresholded fuzzy
            hit = get_close_matches(key, self._keys, n=1, cutoff=threshold)
            if hit:
                return self._lookup[hit[0]]
        return None

    def suggestions(self, raw: str, n: int = 3) -> List[str]:
        hits = get_close_matches((raw or "").strip().lower(), self._keys, n=n, cutoff=0.4)
        out: List[str] = []
        for h in hits:
            c = self._lookup[h]
            if c not in out:
                out.append(c)
        return out

    @property
    def options(self) -> List[str]:
        return self.canonicals


class FreeText:
    """Free-form target (file path, query, name). Taken as-is."""

    def resolve(self, raw: str, threshold: float) -> Optional[str]:
        raw = (raw or "").strip()
        return raw or None

    def suggestions(self, raw: str, n: int = 3) -> List[str]:
        return []

    @property
    def options(self) -> List[str]:
        return ["<free text>"]


# ==============================================================================
# 1b. NAMED-PARAMETER TYPES (optional `key=value` argument schema)
# ==============================================================================
# These are used only when a command declares `params={...}`. Each type exposes
# `.resolve(raw, threshold) -> value | None` (None == invalid), plus `.required`
# and `.default`. NOTE: valid values may be falsy (Bool False, Int 0), so callers
# MUST test `is None`, never truthiness.

class _Param:
    """Base for scalar named parameters. required/default are universal."""
    def __init__(self, required: bool = False, default: Any = None):
        self.required = required
        self.default = default

    @property
    def options(self) -> List[str]:
        return [self.__class__.__name__.lower()]


class Str(_Param):
    """Any non-empty string, taken verbatim."""
    def resolve(self, raw: str, threshold: float):
        raw = (raw or "").strip()
        return raw if raw != "" else None


class Int(_Param):
    """An integer (first integer literal found in the value)."""
    def resolve(self, raw: str, threshold: float):
        m = re.search(r"-?\d+", raw or "")
        return int(m.group()) if m else None


class Float(_Param):
    """A float (accepts integer or decimal literals)."""
    def resolve(self, raw: str, threshold: float):
        m = re.search(r"-?\d+(?:\.\d+)?", raw or "")
        return float(m.group()) if m else None


class Bool(_Param):
    """A boolean, tolerant of common spellings (true/yes/1/on, false/no/0/off)."""
    _TRUE = {"true", "yes", "y", "1", "on", "enabled", "enable"}
    _FALSE = {"false", "no", "n", "0", "off", "disabled", "disable"}

    def resolve(self, raw: str, threshold: float):
        k = (raw or "").strip().lower()
        if k in self._TRUE:
            return True
        if k in self._FALSE:
            return False
        return None


class Enum(Vocab):
    """A closed set of allowed values for a named parameter (Vocab in disguise).
    Accepts either a list of options or a {canonical: [aliases]} mapping."""
    def __init__(self, options, required: bool = False, default: Any = None, fuzzy: bool = True):
        mapping = options if isinstance(options, dict) else {o: [] for o in options}
        super().__init__(mapping, fuzzy=fuzzy)
        self.required = required
        self.default = default


# ==============================================================================
# 2. DATA STRUCTURES
# ==============================================================================

@dataclass
class _Spec:
    name: str
    aliases: List[str]
    target: object          # Vocab | FreeText | None
    quantity: bool
    handler: Callable
    help: str = ""
    params: Optional[Dict[str, Any]] = None   # named-parameter schema (opt-in)


@dataclass
class Command:
    """A valid, typed command, ready to be executed."""
    name: str
    target: Optional[str] = None     # always str or None
    quantity: int = 1                # always int
    raw: str = ""
    handler: Optional[Callable] = field(default=None, repr=False)
    params: Dict[str, Any] = field(default_factory=dict)   # named, typed arguments

    def _invoke(self):
        """Build kwargs and call the handler. May return an awaitable.
        Passes target/quantity if the handler accepts them, plus every named
        parameter the handler accepts (or all of them if it takes **kwargs)."""
        if self.handler is None:
            raise RuntimeError(f"No handler bound to command {self.name!r}")
        sig = inspect.signature(self.handler).parameters
        has_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.values())
        kwargs = {}
        if "target" in sig:
            kwargs["target"] = self.target
        if "quantity" in sig:
            kwargs["quantity"] = self.quantity
        for k, v in self.params.items():
            if has_var_kw or k in sig:
                kwargs[k] = v
        return self.handler(**kwargs)

    def __str__(self) -> str:
        bits = [self.name]
        if self.target is not None:
            bits.append(str(self.target))
        if self.quantity != 1:
            bits.append(f"x{self.quantity}")
        for k, v in self.params.items():
            bits.append(f"{k}={v}")
        return " | ".join(bits)


@dataclass
class CommandError:
    """A failed attempt: syntax, typing, OR physical execution."""
    kind: str   # unknown_command | ambiguous_command | missing_target | unknown_target
                # | invalid_quantity | no_command | execution_error
                # | malformed_param | unknown_param | invalid_param | missing_param
    message: str
    raw: str = ""
    command: Optional[str] = None              # command name (for execution errors)
    exception: Optional[BaseException] = field(default=None, repr=False)


@dataclass
class ParseResult:
    """Result of a parse (+ execution outputs if dispatch was run)."""
    raw_text: str
    message: str = ""
    commands: List[Command] = field(default_factory=list)
    errors: List["CommandError"] = field(default_factory=list)
    outputs: List[Any] = field(default_factory=list)   # return values of successful handlers

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def needs_repair(self) -> bool:
        return bool(self.errors)

    @property
    def repair_prompt(self) -> str:
        """Formatted error message, ready to reinject into the SLM prompt.
        Separates FORMAT failures (parsing/typing) from EXECUTION failures."""
        if not self.errors:
            return ""
        parse_errs = [e for e in self.errors if e.kind != "execution_error"]
        exec_errs = [e for e in self.errors if e.kind == "execution_error"]
        lines: List[str] = []
        if parse_errs:
            lines += [
                "[!] INVALID FORMAT - one or more commands could not be read.",
                f"Rewrite using EXACTLY this format: {_GRAMMAR_HINT}",
                "(append ' | N' for an integer quantity).",
                "",
            ]
            lines += [f"  - {e.message}" for e in parse_errs]
            lines.append("")
        if exec_errs:
            lines += [
                "[!] EXECUTION FAILED - the format was correct, but the action failed.",
                "Fix the target, or choose another action to reach the goal.",
                "",
            ]
            lines += [f"  - {e.message}" for e in exec_errs]
            lines.append("")
        lines.append("Re-emit ONLY the corrected command(s), nothing else.")
        return "\n".join(lines)

    def raise_for_status(self) -> "ParseResult":
        """Exception style: raise RepairNeeded if errors remain, otherwise return self."""
        if self.errors:
            raise RepairNeeded(self)
        return self


class RepairNeeded(Exception):
    """Raised when no usable command could be produced/executed."""
    def __init__(self, result: ParseResult):
        self.result = result
        super().__init__(result.repair_prompt)


# ==============================================================================
# 3. THE ROUTER (registry + parsing + async execution + repair)
# ==============================================================================

class CommandRouter:
    def __init__(
        self,
        fuzzy_threshold: float = 0.78,
        action_fuzzy_threshold: Optional[float] = None,
        require_command: bool = False,
        strip_markdown: bool = True,
        ambiguity_margin: float = 0.08,
    ):
        """
        fuzzy_threshold        : difflib cutoff for TARGETS (0..1). 0.78 separates
                                 genuine typos (~0.9) from substring collisions (~0.7).
        action_fuzzy_threshold : cutoff for VERBS (defaults to fuzzy_threshold).
        require_command        : if True, a response with no valid command also
                                 triggers a repair.
        strip_markdown         : if True, `message` is cleaned of **bold**, `code`,
                                 ### headings (SLMs tend to overuse them).
        ambiguity_margin       : when a fuzzy ACTION match has a runner-up within this
                                 score gap, refuse and suggest instead of guessing
                                 (e.g. create_task vs create_ticket). 0 disables.
        """
        for _name, _v in (("fuzzy_threshold", fuzzy_threshold),
                          ("action_fuzzy_threshold", action_fuzzy_threshold)):
            if _v is not None and not (0.0 <= _v <= 1.0):
                raise ValueError(f"{_name} must be within [0.0, 1.0], got {_v!r}")
        self.fuzzy_threshold = fuzzy_threshold
        self.action_fuzzy_threshold = (
            action_fuzzy_threshold if action_fuzzy_threshold is not None else fuzzy_threshold
        )
        self.require_command = require_command
        self.strip_markdown = strip_markdown
        self.ambiguity_margin = ambiguity_margin
        self._specs: Dict[str, _Spec] = {}
        self._action_lookup: Dict[str, str] = {}
        self._action_keys: List[str] = []

    # ----------------------------------------------------------------- API
    def command(self, name: str, aliases: Optional[List[str]] = None,
                target=None, quantity: bool = False, help: str = "",
                params: Optional[Dict[str, Any]] = None):
        """
        Registration decorator. The handler can be sync OR async.

        Positional schema (default):
            @router.command(name="TURN_ON", aliases=["switch_on"], target=Vocab({...}))
            async def turn_on(target): ...

        Named-parameter schema (opt-in): pass `params={name: type}` where type is
        Str/Int/Float/Bool/Enum (each accepts required=/default=). The handler then
        receives those names as keyword arguments.
            @router.command(name="SEND_EMAIL", params={
                "to": Enum(["boss", "team"], required=True),
                "subject": Str(required=True),
                "urgent": Bool(default=False),
            })
            async def send_email(to, subject, urgent=False): ...
        """
        aliases = aliases or []
        canonical = name.upper()

        def deco(fn: Callable) -> Callable:
            spec = _Spec(canonical, [a.upper() for a in aliases], target, quantity, fn, help, params)
            self._specs[canonical] = spec
            self._action_lookup[canonical] = canonical
            for a in spec.aliases:
                self._action_lookup[a] = canonical
            self._action_keys = list(self._action_lookup.keys())
            return fn  # function returned untouched
        return deco

    def parse(self, text: str, strip_markdown: Optional[bool] = None) -> ParseResult:
        """Turn the SLM's raw text into typed commands + errors + message."""
        text = text or ""
        result = ParseResult(raw_text=text)

        # --- 1. Collect candidates (brackets + keyword lines) ---
        candidates: List[Tuple[int, int, str, bool]] = []
        for m in _BRACKET_RE.finditer(text):
            body = m.group("body")
            if body is None:
                body = m.group("body_open")
            candidates.append((m.start(), m.end(), body, False))
        for m in _LINE_RE.finditer(text):
            candidates.append((m.start("full"), m.end("full"), m.group("body"), True))
        candidates.sort(key=lambda c: c[0])

        # --- 2. De-duplicate by span overlap ---
        chosen: List[Tuple[int, int, str, bool]] = []
        occupied: List[Tuple[int, int]] = []
        for (s, e, body, kw) in candidates:
            if any(not (e <= os or s >= oe) for (os, oe) in occupied):
                continue
            occupied.append((s, e))
            chosen.append((s, e, body, kw))

        # --- 3. Interpretation ---
        command_spans: List[Tuple[int, int]] = []
        for (s, e, body, force_kw) in chosen:
            parsed = self._interpret(body, force_kw)
            if parsed is None:
                continue
            kind, payload = parsed
            command_spans.append((s, e))
            (result.commands if kind == "command" else result.errors).append(payload)

        # --- 4. De-duplicate identical commands ---
        seen, uniq = set(), []
        for c in result.commands:
            key = (c.name, c.target, c.quantity)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        result.commands = uniq

        # --- 5. Option: mandatory command ---
        if self.require_command and not result.commands and not result.errors:
            result.errors.append(CommandError(
                kind="no_command",
                message=("No command detected. You must act: reply with at least "
                         f"one command in the format {_GRAMMAR_HINT}."),
                raw=text.strip()[:120],
            ))

        # --- 6. Spoken message (best effort) ---
        sm = self.strip_markdown if strip_markdown is None else strip_markdown
        result.message = self._extract_message(text, command_spans, sm)
        return result

    async def dispatch(self, result: ParseResult, raise_on_error: bool = False,
                       stop_on_error: bool = False) -> List[Any]:
        """
        Execute the handlers of valid commands (async or sync), IN ORDER.
        Intercepts exceptions -> `execution_error` entries appended to the result
        (unless raise_on_error=True). Returns the list of successful outputs.
        Call only once per result.

        stop_on_error: halt the sequence at the first failing command (remaining
        commands are NOT executed). This is a HALT primitive, not a rollback:
        commands that already ran are not undone. True rollback requires
        compensating actions inside your handlers (saga pattern).
        """
        # Idempotent: drop any prior execution outcome on this result so a second
        # call recomputes instead of accumulating duplicate errors/outputs.
        result.errors = [e for e in result.errors if e.kind != "execution_error"]
        result.outputs = []
        outputs: List[Any] = []
        exec_errors: List[CommandError] = []
        for c in result.commands:
            try:
                outputs.append(await _maybe_await(c._invoke()))
            except Exception as exc:
                if raise_on_error:
                    raise
                exec_errors.append(CommandError(
                    kind="execution_error",
                    message=f"Command '{c}' failed during execution: {exc}",
                    raw=c.raw, command=c.name, exception=exc,
                ))
                if stop_on_error:
                    break
        result.errors.extend(exec_errors)
        result.outputs = outputs
        return outputs

    async def run(self, text: str,
                  retry: Optional[Callable[[str, int], Any]] = None,
                  max_retries: int = 2,
                  raise_on_fail: bool = False,
                  execute: bool = True,
                  stop_on_error: bool = False) -> ParseResult:
        """
        Full loop parse -> execute -> repair (syntax, typing AND execution).
        `retry(repair_prompt, attempt) -> new_text` (sync or async) is supplied by
        you (typically a call to the SLM with the repair prompt).

        - Valid commands are dispatched immediately (no waiting).
        - Failures (parsing/typing/execution) go to repair; the prompt asks the
          model to re-emit ONLY the corrected commands -> outputs accumulate
          without re-running actions that already succeeded.
        - raise_on_fail: raise RepairNeeded if errors remain after max_retries.
        - stop_on_error: halt a turn's command sequence at the first failure
          (forwarded to dispatch; halt only, not rollback).
        """
        result = self.parse(text)
        all_outputs: List[Any] = []
        attempt = 0
        while True:
            if execute and result.commands:
                all_outputs.extend(await self.dispatch(result, stop_on_error=stop_on_error))
            if not result.needs_repair:
                break
            if retry is None or attempt >= max_retries:
                break
            attempt += 1
            new_text = await _maybe_await(retry(result.repair_prompt, attempt))
            result = self.parse(new_text or "")
        result.outputs = all_outputs
        if raise_on_fail and result.needs_repair:
            raise RepairNeeded(result)
        return result

    def system_prompt_block(self) -> str:
        """BONUS: the registry documents itself. Generates the grammar block to
        inject into the system prompt -> grammar and parser never drift apart."""
        lines = [
            "AVAILABLE COMMANDS",
            f"To act, insert one or more tags in the format: {_GRAMMAR_HINT}",
            "",
        ]
        for spec in self._specs.values():
            if spec.params is not None:
                sig = f"[CMD: {spec.name}"
                for k, p in spec.params.items():
                    type_label = "|".join(p.options) if isinstance(p, Vocab) else type(p).__name__.lower()
                    opt = "" if getattr(p, "required", False) else "?"
                    sig += f" | {k}{opt}=<{type_label}>"
                sig += "]"
                line = f"  {sig}"
                if spec.help:
                    line += f"\n      -> {spec.help}"
                lines.append(line)
                continue
            sig = f"[CMD: {spec.name}"
            if spec.target is not None:
                sig += " | <target>"
            if spec.quantity:
                sig += " | <n>"
            sig += "]"
            line = f"  {sig}"
            if isinstance(spec.target, Vocab):
                line += f"\n      targets: {', '.join(spec.target.options)}"
            if spec.help:
                line += f"\n      -> {spec.help}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------- INTERNALS
    @staticmethod
    def _scored(query: str, keys: List[str], cutoff: float) -> List[Tuple[str, float]]:
        """All keys whose similarity to query is >= cutoff, ranked best-first."""
        out = [(k, SequenceMatcher(None, query, k).ratio()) for k in keys]
        out = [(k, r) for (k, r) in out if r >= cutoff]
        out.sort(key=lambda kr: kr[1], reverse=True)
        return out

    def _match_action(self, token: str) -> Tuple[Optional[str], List[str]]:
        """Resolve a verb. Returns (canonical, ambiguous_candidates).
        Exact match wins immediately. On a fuzzy match, if a runner-up pointing to a
        DIFFERENT command is within `ambiguity_margin`, refuse (canonical=None) and
        return the tied candidates so the caller can ask the model to disambiguate."""
        if not token:
            return None, []
        up = token.strip().upper()
        if up in self._action_lookup:
            return self._action_lookup[up], []
        cands = self._scored(up, self._action_keys, self.action_fuzzy_threshold)
        if not cands:
            return None, []
        top_key, top_score = cands[0]
        top_canon = self._action_lookup[top_key]
        if self.ambiguity_margin > 0 and len(cands) > 1:
            distinct: List[str] = []
            for k, r in cands:
                c = self._action_lookup[k]
                if c not in distinct:
                    distinct.append(c)
            for k, r in cands:
                c = self._action_lookup[k]
                if c != top_canon and (top_score - r) < self.ambiguity_margin:
                    return None, distinct[:3]
        return top_canon, []

    def _resolve_action(self, token: str) -> Optional[str]:
        """Thin wrapper used by the command-like gate (ambiguous -> None)."""
        return self._match_action(token)[0]

    def _action_suggestions(self, token: str, n: int = 3) -> List[str]:
        hits = get_close_matches(token.strip().upper(), self._action_keys, n=n, cutoff=0.4)
        out: List[str] = []
        for h in hits:
            c = self._action_lookup[h]
            if c not in out:
                out.append(c)
        return out

    @staticmethod
    def _to_int(segment: str) -> Optional[int]:
        """Cast a QUANTITY field to int. Never applied to a target."""
        m = _INT_RE.search(segment or "")
        return int(m.group()) if m else None

    def _interpret(self, body: str, force_kw: bool):
        """None (prose/label) | ('command', Command) | ('error', CommandError)."""
        raw_full = body.strip()
        work = body

        had_kw = force_kw
        m = _KEYWORD_LEAD_RE.match(work)
        if m:
            had_kw = True
            work = work[m.end():]

        segs = [s.strip() for s in work.split("|") if s.strip()]
        if not segs:
            return None

        head = segs[0]
        action_token = re.split(r"\s+", head, maxsplit=1)[0].strip(":|").strip()
        if action_token.upper() in _META_ALL:
            return None

        if not (had_kw or len(segs) > 1 or self._resolve_action(action_token) is not None):
            return None  # prose in brackets

        canonical, ambiguous = self._match_action(action_token)
        if canonical is None:
            if ambiguous:
                return ("error", CommandError(
                    kind="ambiguous_command",
                    message=(f"'{action_token}' is ambiguous between: {', '.join(ambiguous)}. "
                             f"Re-issue the command using one exact action name."),
                    raw=raw_full,
                ))
            sugg = self._action_suggestions(action_token)
            hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
            valid = ", ".join(self._specs.keys())
            return ("error", CommandError(
                kind="unknown_command",
                message=f"'{action_token}' is not a known ACTION.{hint} Valid actions: {valid}.",
                raw=raw_full,
            ))
        spec = self._specs[canonical]

        # Named-parameter schema (opt-in): parse `key=value` segments.
        if spec.params is not None:
            return self._interpret_named(spec, work, raw_full)

        # FreeText target WITHOUT quantity: take everything after the verb VERBATIM,
        # so the value may contain '|' (URLs, regexes, formulas) and newlines (code,
        # JSON, paragraphs). No further '|' splitting, no quantity scan.
        if isinstance(spec.target, FreeText) and not spec.quantity:
            parts = re.split(r"[\s|]+", work.strip(), maxsplit=1)
            raw_target = parts[1].strip() if len(parts) > 1 else ""
            resolved = spec.target.resolve(raw_target, self.fuzzy_threshold)
            if resolved is None:
                return ("error", CommandError(
                    kind="missing_target",
                    message=f"Action {canonical} expects a TARGET, but none was provided.",
                    raw=raw_full,
                ))
            return ("command", Command(canonical, resolved, 1, raw_full, spec.handler))

        arg_parts = segs[1:]

        target_raw, qty_raw = self._split_args(spec, arg_parts)

        quantity = 1
        if spec.quantity and qty_raw is not None:
            q = self._to_int(qty_raw)
            if q is None:
                return ("error", CommandError(
                    kind="invalid_quantity",
                    message=f"'{qty_raw}' is not a valid QUANTITY (integer expected) for {canonical}.",
                    raw=raw_full,
                ))
            quantity = q

        if spec.target is None:
            return ("command", Command(canonical, None, quantity, raw_full, spec.handler))

        if not target_raw:
            valid = ", ".join(spec.target.options)
            return ("error", CommandError(
                kind="missing_target",
                message=f"Action {canonical} expects a TARGET, but none was provided. Valid targets: {valid}.",
                raw=raw_full,
            ))

        resolved = spec.target.resolve(target_raw, self.fuzzy_threshold)
        if resolved is None:
            sugg = spec.target.suggestions(target_raw)
            hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
            valid = ", ".join(spec.target.options)
            return ("error", CommandError(
                kind="unknown_target",
                message=f"'{target_raw}' is not a valid TARGET for {canonical}.{hint} Valid targets: {valid}.",
                raw=raw_full,
            ))

        return ("command", Command(canonical, resolved, quantity, raw_full, spec.handler))

    @staticmethod
    def _split_args(spec: _Spec, arg_parts: List[str]) -> Tuple[str, Optional[str]]:
        """Decide target (string) vs quantity (int) WITHOUT digging digits out of the target.
          - target + quantity, >=2 segs : target = second-to-last, quantity = last.
          - target + quantity, 1 seg     : target = that seg, quantity absent (default).
          - target only                  : target = last seg (robust to stutter).
          - quantity only                : quantity = last seg."""
        if spec.target is not None and spec.quantity:
            if len(arg_parts) >= 2:
                return arg_parts[-2], arg_parts[-1]
            if len(arg_parts) == 1:
                return arg_parts[-1], None
            return "", None
        if spec.target is not None:
            return (arg_parts[-1] if arg_parts else ""), None
        if spec.quantity:
            return "", (arg_parts[-1] if arg_parts else None)
        return "", None

    @staticmethod
    def _parse_kv(segment: str) -> Optional[Tuple[str, str]]:
        """Split a 'key=value' (or 'key:value') segment on the FIRST separator.
        The value runs to the end (may contain spaces, '=', ':', etc.)."""
        m = re.match(r"\s*([\w.\-]+)\s*[=:]\s*(.*)$", segment, re.DOTALL)
        if not m:
            return None
        return m.group(1), m.group(2).strip()

    def _resolve_param_key(self, spec: _Spec, key_raw: str) -> Optional[str]:
        """Match a provided key to a declared parameter name (exact, then fuzzy)."""
        lower_map = {pk.lower(): pk for pk in spec.params}
        k = key_raw.strip().lower()
        if k in lower_map:
            return lower_map[k]
        hit = get_close_matches(k, list(lower_map.keys()), n=1, cutoff=0.7)
        return lower_map[hit[0]] if hit else None

    def _interpret_named(self, spec: _Spec, work: str, raw_full: str):
        """Parse a command declared with a named-parameter schema."""
        segs = [s for s in (p.strip() for p in work.split("|")) if s]
        # Drop the verb token from the first segment; keep any glued 'key=value'.
        if segs:
            parts = re.split(r"\s+", segs[0], maxsplit=1)
            rest = parts[1].strip() if len(parts) > 1 else ""
            segs = ([rest] if rest else []) + segs[1:]

        provided: Dict[str, Any] = {}
        for seg in segs:
            kv = self._parse_kv(seg)
            if kv is None:
                return ("error", CommandError(
                    kind="malformed_param",
                    message=(f"'{seg}' is not a 'key=value' pair for {spec.name}. "
                             f"Valid keys: {', '.join(spec.params.keys())}."),
                    raw=raw_full,
                ))
            key_raw, val_raw = kv
            key = self._resolve_param_key(spec, key_raw)
            if key is None:
                sugg = get_close_matches(key_raw.lower(),
                                         [k.lower() for k in spec.params], n=2, cutoff=0.4)
                hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
                return ("error", CommandError(
                    kind="unknown_param",
                    message=(f"'{key_raw}' is not a valid parameter for {spec.name}.{hint} "
                             f"Valid keys: {', '.join(spec.params.keys())}."),
                    raw=raw_full,
                ))
            ptype = spec.params[key]
            coerced = ptype.resolve(val_raw, self.fuzzy_threshold)
            if coerced is None:                       # NB: valid values may be falsy
                allowed = (f" Allowed: {', '.join(ptype.options)}."
                           if isinstance(ptype, Vocab) else "")
                return ("error", CommandError(
                    kind="invalid_param",
                    message=(f"'{val_raw}' is not a valid value for '{key}' "
                             f"({type(ptype).__name__.lower()}) in {spec.name}.{allowed}"),
                    raw=raw_full,
                ))
            provided[key] = coerced

        # Apply defaults and check required parameters.
        final: Dict[str, Any] = {}
        missing: List[str] = []
        for k, ptype in spec.params.items():
            if k in provided:
                final[k] = provided[k]
            elif getattr(ptype, "required", False):
                missing.append(k)
            elif getattr(ptype, "default", None) is not None:
                final[k] = ptype.default
        if missing:
            return ("error", CommandError(
                kind="missing_param",
                message=f"{spec.name} is missing required parameter(s): {', '.join(missing)}.",
                raw=raw_full,
            ))
        return ("command", Command(name=spec.name, raw=raw_full, handler=spec.handler, params=final))

    @staticmethod
    def _strip_markdown(s: str) -> str:
        """Strip common Markdown formatting while preserving the text.

        Conservative on purpose: emphasis markers must hug non-space text (as in
        real Markdown), so prose like "3 * 4 * 5" or a glob "*.py" is left intact.
        """
        s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)               # fenced code blocks
        s = re.sub(r"`([^`]+)`", r"\1", s)                              # inline code (keep text)
        s = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", s)              # ATX heading prefixes
        s = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"\1", s, flags=re.DOTALL)  # **bold**
        s = re.sub(r"__(\S(?:.*?\S)?)__", r"\1", s, flags=re.DOTALL)      # __bold__
        s = re.sub(r"(?<![\w*])\*(\S(?:.*?\S)?)\*(?![\w*])", r"\1", s)    # *italic*
        s = re.sub(r"(?<![\w_])_(\S(?:.*?\S)?)_(?![\w_])", r"\1", s)      # _italic_
        s = re.sub(r"~~(\S(?:.*?\S)?)~~", r"\1", s)                     # ~~strikethrough~~
        return s

    @staticmethod
    def _extract_message(text: str, command_spans: List[Tuple[int, int]],
                         strip_markdown: bool = True) -> str:
        """Best effort: isolate what the model *says* (vs thinks / commands)."""
        say = _SAY_RE.search(text)
        if say:
            msg = say.group("say")
        else:
            msg = text
            for (s, e) in sorted(command_spans, reverse=True):
                msg = msg[:s] + " " + msg[e:]
            msg = _THOUGHT_BLOCK_RE.sub("", msg)

        msg = _LABEL_RE.sub(_strip_meta_label, msg)          # drop leftover meta/keyword labels only
        if strip_markdown:
            msg = CommandRouter._strip_markdown(msg)
        msg = re.sub(r"(?m)^\s*\d+[\.\)]\s*", "", msg)       # numbering "1." "2)"
        msg = re.sub(r"[ \t]+", " ", msg)
        msg = re.sub(r"\s*\n\s*", " ", msg).strip()
        return msg
