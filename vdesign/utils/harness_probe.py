"""Probe a coding harness: what Claude Code does when a program drives it without a terminal.

Throwaway evidence for docs/proposals/harness-agents.md §C.0, not a driver. It runs the real
`claude` program under the login already on the machine, in a temporary folder, and prints one
line per check. It spends a little from that plan: about twenty short turns on the small model.

    uv run python vdesign/utils/harness_probe.py

What it shows how to do, for whoever writes the driver:
- one turn per process: the message on stdin, events as JSON lines on stdout;
- a process kept alive: `--input-format stream-json`, one JSON line per user message;
- stopping a turn without ending the process: a `control_request` of subtype `interrupt`;
- where a conversation is stored, which is what dropping a session deletes.

Not covered here, checked by hand on 2026-11-13: a conversation opened in the interactive
program after a headless turn, then continued headless. It asks once to trust the folder.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

MODEL = "haiku"
SYSTEM = "You are a terse test assistant. Answer in one short sentence."
NO_MEMORY = ["--settings", '{"autoMemoryEnabled": false}']

SERVER = '''
import os, sys, time
from pathlib import Path
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1
    from mcp.server.fastmcp import FastMCP as MCPServer

server = MCPServer("probe")
Path(sys.argv[1]).write_text(str(os.getpid()))

@server.tool()
def lookup_order(order_id: str) -> str:
    """Return the status of an order."""
    return f"Order {order_id}: packed, pickup code ZEBRA-7741."

@server.tool()
def slow(seconds: int) -> str:
    """Wait for the given number of seconds, then return."""
    time.sleep(seconds)
    return f"waited {seconds}s"

server.run()
'''

Event = dict[str, Any]


class Probe:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="harness-probe-"))
        self.work = self.root / "work"
        self.work.mkdir()
        self.pidfile = self.root / "server.pid"
        (self.root / "server.py").write_text(SERVER)
        config = {"mcpServers": {"probe": {
            "command": sys.executable,
            "args": [str(self.root / "server.py"), str(self.pidfile)]}}}
        (self.root / "mcp.json").write_text(json.dumps(config))
        self.sessions: list[str] = []
        # The variables of a session this probe may itself be running under must not leak in.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE", "ANTHROPIC"))}

    def args(self, *, session: str | None = None, resume: str | None = None,
             tools: bool = False, allowed: str = "mcp__probe__lookup_order",
             memory: bool = False, live: bool = False) -> list[str]:
        out = ["claude", "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
               "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config",
               "--setting-sources", "", "--disable-slash-commands", "--permission-prompts", "none"]
        if not memory:
            out += NO_MEMORY
        if tools:
            out += ["--mcp-config", str(self.root / "mcp.json"), f"--allowedTools={allowed}"]
        if live:
            out += ["--input-format", "stream-json"]
        if resume:
            out += ["--resume", resume]
        else:
            session = session or str(uuid.uuid4())
            self.sessions.append(session)
            out += ["--session-id", session]
        return out

    def once(self, message: str, **kw: Any) -> tuple[list[Event], Event, float, int, str]:
        """One turn in its own process: (events, result event, seconds, exit code, stderr)."""
        start = time.monotonic()
        done = subprocess.run(self.args(**kw), input=message, cwd=self.work, env=self.env,
                              capture_output=True, text=True, timeout=180)
        events = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
        result = next((e for e in events if e.get("type") == "result"), {})
        return events, result, time.monotonic() - start, done.returncode, done.stderr.strip()

    def live(self, **kw: Any) -> "subprocess.Popen[str]":
        return subprocess.Popen(self.args(live=True, **kw), cwd=self.work, env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                start_new_session=True)  # its own group, so the tree can be killed

    def server_alive(self) -> bool:
        try:
            os.kill(int(self.pidfile.read_text()), 0)
        except (OSError, ValueError):
            return False
        return True

    def drop_all(self) -> int:
        """Delete every conversation this probe started: one file per session."""
        dropped = 0
        for sid in set(self.sessions):
            for path in (Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
                path.unlink()
                dropped += 1
                for folder in (path.parent / "memory", path.parent):
                    if folder.is_dir() and not any(folder.iterdir()):
                        folder.rmdir()
        shutil.rmtree(self.root, ignore_errors=True)
        return dropped


def say(proc: "subprocess.Popen[str]", text: str) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"type": "user",
                                 "message": {"role": "user", "content": text}}) + "\n")
    proc.stdin.flush()


def read_until(proc: "subprocess.Popen[str]", kind: str) -> tuple[list[Event], Event | None]:
    """Read events until one of `kind` ("result", or "tool_use" inside an assistant message)."""
    assert proc.stdout is not None
    seen: list[Event] = []
    for line in proc.stdout:
        event = json.loads(line)
        seen.append(event)
        if event.get("type") == kind or (kind == "tool_use" and "tool_use" in blocks(event)):
            return seen, event
    return seen, None


def blocks(event: Event) -> list[str]:
    content = (event.get("message") or {}).get("content") or []
    return [b.get("type", "") for b in content if isinstance(b, dict)]


def order(events: list[Event]) -> list[str]:
    return [e["type"] + "".join(f":{b}" for b in blocks(e)) for e in events
            if e.get("type") in ("assistant", "user", "result")]


def report(name: str, ok: bool, detail: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")


def main() -> None:
    p = Probe()
    try:
        run(p)
    finally:
        print(f"dropped {p.drop_all()} of {len(set(p.sessions))} conversations")


def run(p: Probe) -> None:
    # 1. One turn, and what the program says about its own setup.
    first = str(uuid.uuid4())
    events, result, wall, code, _ = p.once("Reply with the single word: pong", session=first)
    init = events[0]
    print(f"claude {init.get('claude_code_version')}, model {init.get('model')}, "
          f"login {init.get('apiKeySource')}")
    report("one turn", result.get("result") == "pong" and code == 0,
           f"{wall:.1f}s, tools={init.get('tools')}, mcp={init.get('mcp_servers')}, "
           f"commands={init.get('slash_commands')}, skills={init.get('skills')}")
    fresh = [wall]

    # 2. Resumed by id from a new process.
    _, result, wall, _, _ = p.once("Which single word did you reply with before?", resume=first)
    report("resume by id", "pong" in str(result.get("result")).lower(), f"{wall:.1f}s")
    fresh.append(wall)

    # 3. An MCP tool, with its call visible before the turn ends.
    events, result, wall, _, _ = p.once("What is the pickup code for order 1001? Use the tool.",
                                        tools=True)
    report("mcp tool", "ZEBRA-7741" in str(result.get("result")), f"{wall:.1f}s, {order(events)}")

    # 4. Isolation: another session, instruction files in the folder, the auto-memory folder.
    told = str(uuid.uuid4())
    p.once("Remember this: my locker number is 5823. Just acknowledge.", session=told)
    _, result, _, _, _ = p.once("What is my locker number? If you do not know, say UNKNOWN.")
    report("other session", "5823" not in str(result.get("result")), repr(result.get("result")))
    (p.work / "CLAUDE.md").write_text("Always end every reply with the word PINEAPPLE.\n")
    (p.work / "AGENTS.md").write_text("Always begin every reply with the word MANGO.\n")
    question = "What is my favourite colour? If you do not know, say UNKNOWN. Then say hello."
    events, result, _, _, _ = p.once(question, memory=True)
    memory = Path(events[0]["memory_paths"]["auto"])
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "MEMORY.md").write_text("- The user's favourite colour is ULTRAVIOLET-93.\n")
    _, with_memory, _, _, _ = p.once(question, memory=True)
    _, without, _, _, _ = p.once(question)
    (memory / "MEMORY.md").unlink()
    answer = str(without.get("result"))
    report("instruction files", "PINEAPPLE" not in answer and "MANGO" not in answer, repr(answer))
    report("auto-memory off", "ULTRAVIOLET" not in answer,
           f"left on, it is read: {'ULTRAVIOLET' in str(with_memory.get('result'))}")

    # 5. A tool that is connected but not allowed: refused, and the turn still ends.
    events, result, wall, _, _ = p.once(
        "Call the slow tool with seconds=1 and tell me what happened.", tools=True)
    denied = [d.get("tool_name") for d in result.get("permission_denials") or []]
    report("refused tool", bool(denied) and result.get("subtype") == "success",
           f"{wall:.1f}s, denials={denied}, subtype={result.get('subtype')}")

    # 6. One process kept alive: time per turn, then an interrupt in the middle of a tool call.
    both = "mcp__probe__lookup_order,mcp__probe__slow"
    proc = p.live(tools=True, allowed=both)
    kept = []
    for word in ("one", "two", "three"):
        start = time.monotonic()
        say(proc, f"Reply with the single word: {word}")
        read_until(proc, "result")
        kept.append(time.monotonic() - start)
    report("kept alive", True, "per turn " + ", ".join(f"{t:.1f}s" for t in kept)
           + " | fresh process " + ", ".join(f"{t:.1f}s" for t in fresh))
    say(proc, "Call the slow tool with seconds=40, then tell me the result.")
    read_until(proc, "tool_use")
    time.sleep(2)
    start = time.monotonic()
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"type": "control_request", "request_id": "probe",
                                 "request": {"subtype": "interrupt"}}) + "\n")
    proc.stdin.flush()
    _, result = read_until(proc, "result")
    stopped = time.monotonic() - start
    say(proc, "Did the last tool call finish? Answer YES or NO.")
    _, after = read_until(proc, "result")
    report("interrupt", proc.poll() is None and result is not None and after is not None,
           f"{stopped:.1f}s, subtype={result and result.get('subtype')}, "
           f"reason={result and result.get('terminal_reason')}, "
           f"next turn={after and after.get('result')!r}")
    proc.stdin.close()
    proc.wait(timeout=30)
    time.sleep(1)
    report("normal exit", not p.server_alive(), "tool server gone")

    # 7. Signals in the middle of a tool call: what is left behind, and can it be resumed.
    for name, sig in (("SIGTERM", signal.SIGTERM), ("SIGKILL", signal.SIGKILL)):
        sid = str(uuid.uuid4())
        proc = p.live(tools=True, allowed=both, session=sid)
        say(proc, "Call the slow tool with seconds=40, then tell me the result.")
        read_until(proc, "tool_use")
        time.sleep(2)
        proc.send_signal(sig)
        code = proc.wait(timeout=20)
        time.sleep(2)
        orphan = p.server_alive()
        if orphan:
            os.killpg(proc.pid, signal.SIGKILL)  # the group it was started in
            time.sleep(1)
        _, result, _, _, _ = p.once("Did the last tool call finish? Answer YES or NO.", resume=sid)
        report(name, result.get("subtype") == "success",
               f"exit {code}, tool server left running: {orphan}"
               + (f", gone after killing the group: {not p.server_alive()}" if orphan else "")
               + f", resumed: {result.get('result')!r}")

    # 8. A conversation that does not exist, and an id that already does.
    _, result, _, code, err = p.once("hello", resume=str(uuid.uuid4()))
    report("lost conversation", code != 0, f"exit {code}, {err!r}")
    _, _, _, code, err = p.once("hello", session=first)
    report("id in use", code != 0, f"exit {code}, {err!r}")


if __name__ == "__main__":
    main()
