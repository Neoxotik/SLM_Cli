"""Test suite for slm_cli.

Deliberately free of `pytest-asyncio`: coroutines are executed via
`asyncio.run(...)` inside synchronous tests, so `pytest` stays the only dev
dependency.
"""
import asyncio

import pytest

from slm_cli import (
    CommandRouter, Vocab, FreeText,
    Command, CommandError, ParseResult, RepairNeeded,
)


def run(coro):
    """Execute a coroutine inside a synchronous test."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Fixture: a universal router covering every case.
# --------------------------------------------------------------------------
@pytest.fixture
def router():
    r = CommandRouter(fuzzy_threshold=0.78)

    devices = Vocab({
        "printer": ["print", "printing"],
        "heater":  ["heating", "radiator"],
        "door":    [],
    })
    rooms = Vocab({"living_room": ["lounge"], "office": ["study"]})

    @r.command(name="TURN_ON", aliases=["activate", "enable"], target=devices,
               help="Turn a device on.")
    async def turn_on(target):
        return ("on", target)

    @r.command(name="SET_TEMPERATURE", aliases=["set_temp"], target=rooms, quantity=True)
    async def set_temp(target, quantity=1):
        return ("temp", target, quantity)

    @r.command(name="READ_FILE", aliases=["read", "open"], target=FreeText())
    async def read_file(target):
        if target == "absent.txt":
            raise FileNotFoundError(f"not found: {target}")
        return ("file", target)

    @r.command(name="WAIT", aliases=["pause"], quantity=True)
    def wait(quantity=1):                       # deliberately SYNCHRONOUS handler
        return ("wait", quantity)

    return r


# ============================ RESOLUTION ===================================
def test_exact_canonical(router):
    res = router.parse("[CMD: TURN_ON | printer]")
    assert res.ok
    assert res.commands[0].name == "TURN_ON"
    assert res.commands[0].target == "printer"


def test_alias_resolution(router):
    res = router.parse("[CMD: activate | print]")
    assert res.commands[0].name == "TURN_ON"
    assert res.commands[0].target == "printer"


def test_case_insensitive(router):
    res = router.parse("[cmd: turn_on | PRINTER]")
    assert res.ok and res.commands[0].target == "printer"


def test_fuzzy_typo_accepted_target(router):
    res = router.parse("[CMD: TURN_ON | printr]")      # ratio ~0.92
    assert res.ok and res.commands[0].target == "printer"


def test_fuzzy_typo_accepted_action(router):
    res = router.parse("[CMD: TRUN_ON | heater]")      # transposed verb
    assert res.ok and res.commands[0].name == "TURN_ON"


def test_threshold_rejects_collision(router):
    # a different word sharing a root must be rejected below the threshold
    res = router.parse("[CMD: TURN_ON | doorbell]")    # ratio door/doorbell ~0.67
    assert not res.ok
    assert res.errors[0].kind == "unknown_target"


# ============================ TYPING =======================================
def test_quantity_is_int(router):
    res = router.parse("[CMD: SET_TEMPERATURE | office | 21]")
    c = res.commands[0]
    assert c.quantity == 21 and isinstance(c.quantity, int)


def test_filename_digits_preserved(router):
    res = router.parse("[CMD: READ_FILE | report_2024_Q3.txt]")
    assert res.commands[0].target == "report_2024_Q3.txt"   # no digit swallowed


def test_invalid_quantity_rejected(router):
    res = router.parse("[CMD: SET_TEMPERATURE | office | very hot]")
    assert not res.ok and res.errors[0].kind == "invalid_quantity"


def test_quantity_trailing_unit_ignored(router):
    res = router.parse("[CMD: WAIT | 30 seconds]")
    assert res.commands[0].quantity == 30


# ============================ ERRORS =======================================
def test_unknown_command(router):
    res = router.parse("[CMD: TELEPORT | office]")
    assert res.errors[0].kind == "unknown_command"


def test_missing_target(router):
    res = router.parse("[CMD: TURN_ON]")
    assert res.errors[0].kind == "missing_target"


def test_unknown_target(router):
    res = router.parse("[CMD: TURN_ON | zzzzzz]")
    assert res.errors[0].kind == "unknown_target"


# ============================ TOLERANCE ====================================
def test_meta_tags_ignored(router):
    txt = "[THOUGHT] thinking about all this\n[Note: nothing important]\n[CMD: WAIT | 5]"
    res = router.parse(txt)
    assert len(res.commands) == 1 and res.commands[0].name == "WAIT"
    assert res.ok                                          # no false error


def test_stutter_resolved(router):
    res = router.parse("[TURN_ON | CMD: TURN_ON | printer]")
    assert res.ok and res.commands[0].target == "printer"


def test_prose_line_not_command(router):
    # the keyword "action" mid-sentence must NOT trigger a command
    txt = "We should take action: review everything carefully."
    res = router.parse(txt)
    assert res.commands == [] and res.errors == []


def test_bracket_prose_ignored(router):
    res = router.parse("Everything [important] is ready.")  # not command-like
    assert res.commands == [] and res.errors == []


def test_duplicate_commands_collapsed(router):
    res = router.parse("[CMD: WAIT | 5]\n[CMD: WAIT | 5]")
    assert len(res.commands) == 1


# ============================ ASYNC / DISPATCH =============================
def test_async_dispatch_outputs(router):
    res = router.parse("[CMD: TURN_ON | printer]")
    outs = run(router.dispatch(res))
    assert outs == [("on", "printer")]


def test_sync_handler_works(router):
    res = router.parse("[CMD: WAIT | 3]")
    outs = run(router.dispatch(res))
    assert outs == [("wait", 3)]


def test_mixed_async_and_sync(router):
    res = router.parse("[CMD: TURN_ON | printer]\n[CMD: WAIT | 2]")
    outs = run(router.dispatch(res))
    assert ("on", "printer") in outs and ("wait", 2) in outs


# ============================ EXECUTION REPAIR (V2) ========================
def test_execution_error_caught(router):
    res = router.parse("[CMD: READ_FILE | absent.txt]")
    run(router.dispatch(res))
    assert any(e.kind == "execution_error" for e in res.errors)
    assert res.needs_repair


def test_execution_error_in_repair_prompt(router):
    res = router.parse("[CMD: READ_FILE | absent.txt]")
    run(router.dispatch(res))
    assert "EXECUTION" in res.repair_prompt.upper()


def test_dispatch_raise_on_error(router):
    res = router.parse("[CMD: READ_FILE | absent.txt]")
    with pytest.raises(FileNotFoundError):
        run(router.dispatch(res, raise_on_error=True))


def test_run_recovers_from_execution_failure(router):
    async def retry(prompt, attempt):
        return "[CMD: READ_FILE | present.txt]"
    res = run(router.run("[CMD: READ_FILE | absent.txt]", retry=retry, max_retries=2))
    assert res.ok
    assert ("file", "present.txt") in res.outputs


def test_run_recovers_from_parse_failure(router):
    async def retry(prompt, attempt):
        return "[CMD: SET_TEMPERATURE | office | 20]"
    res = run(router.run("[CMD: SET_TEMPERATURE | office | a lot]",
                         retry=retry, max_retries=2))
    assert res.ok and ("temp", "office", 20) in res.outputs


def test_run_sync_retry_callback(router):
    def retry(prompt, attempt):                  # SYNCHRONOUS retry (must work too)
        return "[CMD: WAIT | 1]"
    res = run(router.run("[CMD: TELEPORT | x]", retry=retry, max_retries=1))
    assert res.ok and ("wait", 1) in res.outputs


def test_run_gives_up_after_max_retries(router):
    async def retry(prompt, attempt):
        return "[CMD: STILL_WRONG | x]"          # never corrected
    res = run(router.run("[CMD: NOPE | x]", retry=retry, max_retries=2))
    assert res.needs_repair


# ============================ MESSAGE / MARKDOWN ===========================
def test_reply_section_extracted(router):
    txt = "[THOUGHT] internal\n[REPLY] Here is your answer.\n\n[CMD: WAIT | 1]"
    res = router.parse(txt)
    assert res.message == "Here is your answer."


def test_strip_markdown_true(router):
    txt = "[REPLY] ### Top\n**I do** `all` this.\n[CMD: WAIT | 1]"
    res = router.parse(txt, strip_markdown=True)
    assert "**" not in res.message and "#" not in res.message and "`" not in res.message


def test_strip_markdown_false(router):
    txt = "[REPLY] **bold**\n[CMD: WAIT | 1]"
    res = router.parse(txt, strip_markdown=False)
    assert "**bold**" in res.message


# ============================ MISC =========================================
def test_system_prompt_block_lists_commands(router):
    block = router.system_prompt_block()
    for name in ("TURN_ON", "SET_TEMPERATURE", "READ_FILE", "WAIT"):
        assert name in block


def test_raise_for_status_raises(router):
    with pytest.raises(RepairNeeded):
        router.parse("[CMD: FLY | moon]").raise_for_status()


def test_raise_for_status_passthrough(router):
    res = router.parse("[CMD: WAIT | 1]").raise_for_status()
    assert isinstance(res, ParseResult) and res.ok


def test_require_command_flags_empty():
    r = CommandRouter(require_command=True)

    @r.command(name="PING")
    def ping():
        return "pong"

    res = r.parse("just chatter, no command here")
    assert res.errors and res.errors[0].kind == "no_command"


def test_decorator_returns_callable_intact(router):
    res = router.parse("[CMD: WAIT | 7]")
    assert res.commands[0].name == "WAIT"


def test_command_str():
    c = Command(name="TURN_ON", target="printer", quantity=1)
    assert str(c) == "TURN_ON | printer"
    c2 = Command(name="WAIT", target=None, quantity=5)
    assert str(c2) == "WAIT | x5"


# ============================ V2.1 HARDENING ===============================
def test_threshold_validation():
    with pytest.raises(ValueError):
        CommandRouter(fuzzy_threshold=1.5)
    with pytest.raises(ValueError):
        CommandRouter(action_fuzzy_threshold=-0.1)


def test_freetext_preserves_pipes(router):
    res = router.parse("[CMD: READ_FILE | a | b | c]")
    assert res.ok and res.commands[0].target == "a | b | c"


def test_freetext_preserves_url_with_query(router):
    res = router.parse("[CMD: READ_FILE | https://x.com/p?a=1&b=2|c]")
    assert res.ok and res.commands[0].target == "https://x.com/p?a=1&b=2|c"


def test_freetext_multiline(router):
    res = router.parse("[CMD: READ_FILE | line1\nline2]")
    assert res.ok and res.commands[0].target == "line1\nline2"


def test_unclosed_bracket_still_parses(router):
    res = router.parse("[CMD: WAIT | 5")
    assert res.ok and res.commands[0].quantity == 5


def test_unclosed_bracket_does_not_swallow_later_brackets(router):
    # an unclosed tag must stop at the newline, not eat the next line's bracket
    res = router.parse("[CMD: WAIT | 5\nI saw [it] today.")
    assert res.ok and res.commands[0].quantity == 5


def test_markdown_link_produces_no_error(router):
    res = router.parse("See the [docs](https://example.com) for details.")
    assert res.commands == [] and res.errors == []


def test_markdown_preserves_math(router):
    res = router.parse("[REPLY] the answer is 3 * 4 * 5\n[CMD: WAIT | 1]")
    assert "3 * 4 * 5" in res.message


def test_markdown_preserves_glob(router):
    res = router.parse("[REPLY] remove all *.py files\n[CMD: WAIT | 1]")
    assert "*.py" in res.message


def test_markdown_still_strips_real_bold(router):
    res = router.parse("[REPLY] this is **important** stuff\n[CMD: WAIT | 1]")
    assert "**" not in res.message and "important" in res.message


def test_dispatch_idempotent_on_execution_error(router):
    res = router.parse("[CMD: READ_FILE | absent.txt]")
    run(router.dispatch(res))
    run(router.dispatch(res))            # second call must not double the errors
    assert sum(1 for e in res.errors if e.kind == "execution_error") == 1


def test_message_keeps_short_prose_brackets(router):
    # no [REPLY] tag -> fallback path: command spans are blanked, but prose
    # brackets like [1] / [important] are preserved (only meta labels are dropped)
    res = router.parse("Step [1] is ready and [important] too. [CMD: WAIT | 1]")
    assert "[1]" in res.message and "[important]" in res.message


# ============================ V2.2: NAMED PARAMS ===========================
from slm_cli import Str, Int, Float, Bool, Enum   # noqa: E402


@pytest.fixture
def prouter():
    r = CommandRouter(fuzzy_threshold=0.78)

    @r.command(name="SEND_EMAIL", aliases=["email"], params={
        "to": Enum(["boss", "team", "john"], required=True),
        "subject": Str(required=True),
        "priority": Int(default=3),
        "urgent": Bool(default=False),
    })
    async def send_email(to, subject, priority=3, urgent=False):
        return ("email", to, subject, priority, urgent)

    @r.command(name="SET_VOLUME", params={"level": Float(required=True)})
    def set_volume(level):
        return ("vol", level)

    return r


def test_named_happy_path(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to=boss | subject=Meeting | urgent=true]")
    assert res.ok
    c = res.commands[0]
    assert c.params == {"to": "boss", "subject": "Meeting", "priority": 3, "urgent": True}


def test_named_dispatch(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to=team | subject=Hi]")
    outs = run(prouter.dispatch(res))
    assert outs == [("email", "team", "Hi", 3, False)]


def test_named_enum_alias_and_fuzzy(prouter):
    res = prouter.parse("[CMD: email | to=bosss | subject=x]")   # 'bosss' ~ 'boss' (>0.78)
    assert res.ok and res.commands[0].params["to"] == "boss"


def test_named_key_fuzzy(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | too=boss | subjct=Hello]")  # misspelled keys
    assert res.ok and res.commands[0].params["subject"] == "Hello"


def test_named_missing_required(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | subject=NoRecipient]")
    assert not res.ok and res.errors[0].kind == "missing_param"


def test_named_unknown_param(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to=boss | subject=x | colour=red]")
    assert not res.ok and res.errors[0].kind == "unknown_param"


def test_named_invalid_enum_value(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to=zzzzz | subject=x]")
    assert not res.ok and res.errors[0].kind == "invalid_param"


def test_named_malformed_segment(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to boss | subject=x]")   # missing '='
    assert not res.ok and res.errors[0].kind == "malformed_param"


def test_named_int_coercion(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to=boss | subject=x | priority=1]")
    c = res.commands[0]
    assert c.params["priority"] == 1 and isinstance(c.params["priority"], int)


def test_named_float_coercion(prouter):
    res = prouter.parse("[CMD: SET_VOLUME | level=0.75]")
    c = res.commands[0]
    assert c.params["level"] == 0.75 and isinstance(c.params["level"], float)


def test_named_bool_false_is_valid_not_missing(prouter):
    # 'urgent=false' must coerce to False, NOT be treated as invalid/None
    res = prouter.parse("[CMD: SEND_EMAIL | to=boss | subject=x | urgent=no]")
    assert res.ok and res.commands[0].params["urgent"] is False


def test_named_value_with_spaces(prouter):
    res = prouter.parse("[CMD: SEND_EMAIL | to=boss | subject=Project status update]")
    assert res.commands[0].params["subject"] == "Project status update"


def test_named_repair_loop(prouter):
    async def retry(prompt, attempt):
        return "[CMD: SEND_EMAIL | to=boss | subject=Fixed]"
    res = run(prouter.run("[CMD: SEND_EMAIL | subject=Oops]", retry=retry, max_retries=2))
    assert res.ok and ("email", "boss", "Fixed", 3, False) in res.outputs


def test_named_grammar_block(prouter):
    block = prouter.system_prompt_block()
    assert "to" in block and "subject" in block and "boss" in block


# ============================ V2.2: AMBIGUITY GUARD ========================
@pytest.fixture
def arouter():
    r = CommandRouter()
    for nm in ["CREATE_TASK", "CREATE_TICKET", "CREATE_USER"]:
        r.command(name=nm)(lambda **k: nm)
    return r


def test_ambiguous_action_refused(arouter):
    res = arouter.parse("[CMD: CREATE_TKT | x]")     # near-tied task/ticket
    assert not res.ok and res.errors[0].kind == "ambiguous_command"


def test_clear_typo_still_resolves(arouter):
    res = arouter.parse("[CMD: CREATE_TASKK]")        # clearly task
    assert res.ok and res.commands[0].name == "CREATE_TASK"


def test_ambiguity_disabled():
    r = CommandRouter(ambiguity_margin=0.0)
    for nm in ["CREATE_TASK", "CREATE_TICKET"]:
        r.command(name=nm)(lambda **k: nm)
    res = r.parse("[CMD: CREATE_TKT]")                # picks best, no refusal
    assert res.ok


# ============================ V2.2: STOP_ON_ERROR ==========================
def test_stop_on_error_halts_chain():
    r = CommandRouter()
    calls = []

    @r.command(name="STEP_A")
    def a():
        calls.append("A"); return "A"

    @r.command(name="STEP_B")
    def b():
        calls.append("B"); raise RuntimeError("boom")

    @r.command(name="STEP_C")
    def c():
        calls.append("C"); return "C"

    res = r.parse("[CMD: STEP_A]\n[CMD: STEP_B]\n[CMD: STEP_C]")
    run(r.dispatch(res, stop_on_error=True))
    assert calls == ["A", "B"]                        # C never ran
    assert any(e.kind == "execution_error" for e in res.errors)


def test_no_stop_runs_all_by_default():
    r = CommandRouter()
    calls = []

    @r.command(name="STEP_A")
    def a():
        calls.append("A"); raise RuntimeError("boom")

    @r.command(name="STEP_B")
    def b():
        calls.append("B"); return "B"

    res = r.parse("[CMD: STEP_A]\n[CMD: STEP_B]")
    run(r.dispatch(res))
    assert calls == ["A", "B"]                        # B still ran


# ============================ V2.2: NAMESPACES (free) ======================
def test_namespaced_command_resolves():
    r = CommandRouter()

    @r.command(name="FILE.OPEN", target=FreeText())
    def file_open(target):
        return ("open", target)

    @r.command(name="MAIL.SEND", target=FreeText())
    def mail_send(target):
        return ("send", target)

    res = r.parse("[CMD: FILE.OPEN | report.txt]")
    assert res.ok and res.commands[0].name == "FILE.OPEN"
    assert res.commands[0].target == "report.txt"
