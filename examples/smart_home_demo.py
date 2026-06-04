"""Demonstration -- a smart-home / assistant agent driven by slm_cli.

Run it with:  python examples/smart_home_demo.py
"""
import asyncio

from slm_cli import CommandRouter, Vocab, FreeText, RepairNeeded, Str, Int, Bool, Enum


router = CommandRouter(fuzzy_threshold=0.78, strip_markdown=True)

DEVICES = Vocab({
    "living_room_light": ["lamp", "living room light", "lounge light", "living room"],
    "kitchen_light":     ["kitchen light"],
    "heater":            ["heating", "radiator", "thermostat"],
    "tv":                ["television", "telly"],
    "fan":               ["ventilator"],
    "blinds":            ["shades", "shutters", "curtains"],
})
ROOMS = Vocab({
    "living_room": ["lounge"],
    "bedroom":     ["master bedroom"],
    "kitchen":     [],
    "office":      ["study"],
})
CONTACTS = Vocab({
    "boss":    ["manager", "supervisor", "director"],
    "john":    ["john smith"],
    "support": ["helpdesk", "it", "tech support"],
    "team":    ["colleagues", "everyone"],
})

# Simulated filesystem to prove EXECUTION REPAIR.
FILES_AVAILABLE = {"report_2024_Q3.txt", "notes.txt", "report.txt"}


# --------- ASYNC handlers (the normal case for agents) ---------
@router.command(name="TURN_ON", aliases=["activate", "enable", "start", "power_on"],
                target=DEVICES, help="Turn a device on.")
async def turn_on(target):
    await asyncio.sleep(0)               # simulate async I/O (API, MQTT...)
    return f"[TURN_ON]      -> {target}"

@router.command(name="TURN_OFF", aliases=["deactivate", "disable", "stop", "power_off"],
                target=DEVICES, help="Turn a device off.")
async def turn_off(target):
    await asyncio.sleep(0)
    return f"[TURN_OFF]     -> {target}"

@router.command(name="SET_TEMPERATURE", aliases=["set_temp", "temperature", "set_heat"],
                target=ROOMS, quantity=True, help="Set a room's temperature (deg C).")
async def set_temperature(target, quantity=1):
    await asyncio.sleep(0)
    return f"[SET_TEMP]     -> {target} to {quantity} deg C"

@router.command(name="READ_FILE", aliases=["read", "open", "cat"],
                target=FreeText(), help="Read a file (target = free-form path).")
async def read_file(target):
    await asyncio.sleep(0)
    if target not in FILES_AVAILABLE:    # PHYSICAL FAILURE -> execution_error
        raise FileNotFoundError(f"File not found: {target}")
    return f"[READ_FILE]    -> contents of {target}"

@router.command(name="SEND_EMAIL", aliases=["email", "mail", "write_to"],
                target=CONTACTS, quantity=True, help="Send N email(s) to a contact.")
async def send_email(target, quantity=1):
    await asyncio.sleep(0)
    return f"[SEND_EMAIL]   -> {quantity} email(s) to {target}"

# --------- A deliberately SYNCHRONOUS handler (must work too) ---------
@router.command(name="WAIT", aliases=["pause", "sleep", "hold"],
                quantity=True, help="Wait N seconds.")
def wait(quantity=1):                    # NOT async: the framework handles both
    return f"[WAIT]         -> {quantity}s (sync handler)"

@router.command(name="SEARCH_WEB", aliases=["search", "google", "lookup"],
                target=FreeText(), help="Web search (target = free-form query).")
async def search_web(target):
    await asyncio.sleep(0)
    return f"[SEARCH_WEB]   -> '{target}'"

# --------- A NAMED-PARAMETER command (typed key=value schema) ---------
@router.command(name="SCHEDULE", params={
    "with": Enum(["boss", "team", "john"], required=True),
    "title": Str(required=True),
    "duration": Int(default=30),
    "urgent": Bool(default=False),
}, help="Schedule a meeting (named, typed parameters).")
async def schedule(**kwargs):
    await asyncio.sleep(0)
    return f"[SCHEDULE]     -> {kwargs}"


def banner(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


async def main():
    banner("AUTO-GENERATED GRAMMAR (to inject into the SLM's system prompt)")
    print(router.system_prompt_block())

    monster = """
Sure, I'll take care of everything when I get home. Let me think about the order.

[THOUGHT] It's dark and cold, and he has unread emails. I'll turn on the lights,
raise the bedroom temperature, then handle his messages. Let's stay methodical.
[REPLY] On it: I'll take care of the house and your messages!

Here is my plan of action:
1. [CMD: TURN_ON | living room ligth]
2. [cmd: activate | television]
3. [CMD: SET_TEMPERATURE | bedroom | 21]
4. **[CMD: TRUN_ON | heater]**
5. [CMD: READ_FILE | report_2024_Q3.txt]
6. [CMD: SEND_EMAIL | boss | urgent]
7. [CMD: SEND_EMAIL | john | 3]
8. [CMD: WAIT | 30 seconds]
9. [TURN_OFF | CMD: TURN_OFF | fan]
10. [CMD: TELEPORT | the moon]
11. [CMD: SEARCH_WEB | weather in Paris tomorrow]
12. [Note: I think that covers everything, let me know otherwise]
[ACTION] [CMD: TURN_ON | tractor]
"""
    banner("RAW INPUT (an SLM hallucinating its syntax)")
    print(monster)

    res = router.parse(monster)

    banner("EXTRACTED MESSAGE (what the agent says)")
    print(repr(res.message))

    banner("VALID COMMANDS + TYPES (real async execution via dispatch)")
    outs = await router.dispatch(res)
    for c, out in zip(res.commands, outs):
        print(f"  {out:<40}  target={c.target!r} ({type(c.target).__name__}), "
              f"quantity={c.quantity!r} ({type(c.quantity).__name__})")

    banner("TOLERANT TYPING PROOF (digits in the target are not swallowed)")
    f = next((c for c in res.commands if c.name == "READ_FILE"), None)
    print("  File target INTACT:", repr(f.target) if f else "MISSING!")
    print("  Quantity 'urgent' REJECTED:",
          any(e.kind == "invalid_quantity" for e in res.errors))

    banner("PARSING / TYPING ERRORS")
    for e in res.errors:
        print(f"  [{e.kind}] {e.message}")

    # ---------- V2: EXECUTION REPAIR ----------
    banner("EXECUTION REPAIR: format is correct but the handler fails")
    text = "Let me open the requested file. [CMD: READ_FILE | secret.txt]"
    print("Input:", text)

    async def slm_retry_exec(prompt: str, attempt: int) -> str:
        print(f"\n--- Attempt #{attempt}: error prompt sent back to the model ---")
        print(prompt)
        return "Sorry, trying an accessible file: [CMD: READ_FILE | notes.txt]"

    res_exec = await router.run(text, retry=slm_retry_exec, max_retries=2)
    print("\n--- Final result ---")
    print("  ok      :", res_exec.ok)
    print("  commands:", [str(c) for c in res_exec.commands])
    print("  outputs :", res_exec.outputs)

    # ---------- PARSING repair via run() ----------
    banner("TYPING REPAIR via run() (non-integer quantity)")
    broken = "On it! [CMD: SEND_EMAIL | boss | a lot]"
    print("Input:", broken)

    async def slm_retry_parse(prompt: str, attempt: int) -> str:
        print(f"\n--- Attempt #{attempt} ---")
        return "Fixed: [CMD: SEND_EMAIL | boss | 5]"

    res2 = await router.run(broken, retry=slm_retry_parse, max_retries=2)
    print("\n--- Final result ---")
    print("  ok      :", res2.ok)
    print("  outputs :", res2.outputs)

    # ---------- V2: strip_markdown ----------
    banner("MARKDOWN CLEANUP (strip_markdown)")
    md = ("[REPLY]\n### Perfect!\n**I'll handle** `everything` right away.\n\n"
          "[CMD: WAIT | 5]")
    print("  strip_markdown=True :", repr(router.parse(md).message))
    print("  strip_markdown=False:", repr(router.parse(md, strip_markdown=False).message))

    # ---------- EXCEPTION variant ----------
    banner("EXCEPTION VARIANT (raise_for_status)")
    try:
        router.parse("[CMD: FLY | the moon]").raise_for_status()
    except RepairNeeded as exc:
        print("  RepairNeeded raised:", exc.result.errors[0].message)

    # ---------- V2.2: NAMED, TYPED PARAMETERS ----------
    banner("NAMED PARAMETERS (typed key=value, fuzzy keys, defaults)")
    text = "[CMD: SCHEDULE | wth=team | title=Sprint review | urgent=yes]"
    print("Input:", text, "   (note the misspelled key 'wth')")
    res_p = router.parse(text)
    out = await router.dispatch(res_p)
    print("  parsed params:", res_p.commands[0].params)
    print("  dispatch     :", out)

    banner("NAMED PARAMETERS: a missing required field goes to repair")
    bad = "[CMD: SCHEDULE | title=No attendee]"
    print("Input:", bad)

    async def slm_retry_named(prompt: str, attempt: int) -> str:
        print(f"\n--- Attempt #{attempt}: repair prompt ---")
        print(prompt)
        return "[CMD: SCHEDULE | with=boss | title=No attendee]"

    res_pr = await router.run(bad, retry=slm_retry_named, max_retries=2)
    print("\n--- Final result ---")
    print("  ok     :", res_pr.ok)
    print("  outputs:", res_pr.outputs)


if __name__ == "__main__":
    asyncio.run(main())
