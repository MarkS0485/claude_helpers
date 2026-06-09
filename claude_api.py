#!/usr/bin/env python3
"""Talk to the Anthropic API with the token Claude Code already has on disk,
and let Claude compact its own session transcripts.

Claude Code stores every session as a JSONL transcript under
~/.claude/projects/<slug>/<session-id>.jsonl (one folder per working
directory). The credentials it uses live in ~/.claude/.credentials.json. This
module reuses both: it reads the OAuth token, exposes a tiny stdlib-only
Messages-API client, and turns a session transcript into a structured
"resume handoff" by asking Claude to summarise it - the same idea as the
in-session /compact, but callable programmatically.

What it can and cannot do:
  - CAN  read any session transcript and produce a compaction summary you can
         save and paste into (or --append onto) a fresh session as a handoff.
  - CANT reach into a *running* CLI and swap out its live context - the running
         Claude Code process owns that; /compact is the in-session command.

Auth: prefers ANTHROPIC_API_KEY (x-api-key) if set; otherwise the Claude Code
OAuth token from ~/.claude/.credentials.json. The OAuth path needs the
oauth beta header and a system prompt that leads with the Claude Code identity
line - both are applied automatically. Stdlib only - no pip installs needed.

Usage:
  python claude_api.py whoami                       auth source, scopes, expiry
  python claude_api.py sessions [--project DIR]     list this project's sessions
  python claude_api.py rename "New title" [SESSION] rename a session
  python claude_api.py count [SESSION]              token count of a transcript
  python claude_api.py compact [SESSION] [--out F]  Claude-compact a session
  python claude_api.py ask "prompt" [--model M]     one-shot prompt, prints reply
  python claude_api.py models                       list available models

SESSION may be a session id, a path to a .jsonl, or omitted (defaults to the
live session - the most recently modified transcript in the project).
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # block chars on cp1252 consoles

CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
# the OAuth token is scoped to Claude Code's identity - the API rejects an
# inference call whose system prompt does not start with this exact line.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

DEFAULT_MODEL = "claude-sonnet-4-6"
COMPACT_MODEL = "claude-sonnet-4-6"
# rough chars-per-token; used only to chunk huge transcripts for rolling
# summarisation so each piece fits comfortably in one request.
CHARS_PER_TOKEN = 3.5
CHUNK_TOKENS = 120_000


# ----------------------------------------------------------------- auth ---

def read_oauth():
    """Return the {accessToken, scopes, expiresAt, ...} block, or None."""
    try:
        with open(CREDS_PATH, encoding="utf-8") as f:
            return json.load(f)["claudeAiOauth"]
    except (OSError, ValueError, KeyError):
        return None


def resolve_auth():
    """Decide how to authenticate. Returns a dict describing the choice:
        {source, headers, oauth?}  - headers go on every API request.
    Raises SystemExit with a clear message if nothing usable is on disk."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return {"source": "ANTHROPIC_API_KEY (env)",
                "headers": {"x-api-key": api_key}}
    oauth = read_oauth()
    if oauth and oauth.get("accessToken"):
        if oauth.get("expiresAt", 0) / 1000 < time.time():
            sys.exit("Claude Code OAuth token has expired - open Claude Code "
                     "once to refresh it, then retry.")
        return {
            "source": "Claude Code OAuth (~/.claude/.credentials.json)",
            "oauth": oauth,
            "headers": {
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": OAUTH_BETA,
            },
        }
    sys.exit(f"No credentials found - set ANTHROPIC_API_KEY or log in to "
             f"Claude Code (expected a token in {CREDS_PATH}).")


# ------------------------------------------------------------ API client ---

class AnthropicClient:
    """Minimal Messages-API client over urllib. One instance picks auth once."""

    def __init__(self, auth=None, timeout=120):
        self.auth = auth or resolve_auth()
        self.timeout = timeout
        self.is_oauth = "oauth" in self.auth

    def _request(self, method, path, payload=None):
        headers = {
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "User-Agent": "claude-api-helper",
            **self.auth["headers"],
        }
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(API_BASE + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError):
                detail = detail[:500]
            raise RuntimeError(f"API {e.code}: {detail}") from None

    def _system_blocks(self, system):
        """Build the system field. With OAuth the first block MUST be the
        Claude Code identity line, so prepend it and keep the caller's text
        as a second block."""
        blocks = []
        if self.is_oauth:
            blocks.append({"type": "text", "text": CLAUDE_CODE_IDENTITY})
        if system:
            blocks.append({"type": "text", "text": system})
        return blocks or None

    def message(self, messages, system=None, model=DEFAULT_MODEL,
                max_tokens=4096, temperature=None):
        """POST /v1/messages. `messages` is a list of {role, content}.
        Returns the full response dict."""
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": messages}
        sys_blocks = self._system_blocks(system)
        if sys_blocks:
            payload["system"] = sys_blocks
        if temperature is not None:
            payload["temperature"] = temperature
        return self._request("POST", "/v1/messages", payload)

    def text(self, prompt, system=None, model=DEFAULT_MODEL, max_tokens=4096):
        """Convenience: one user prompt in, concatenated reply text out."""
        resp = self.message([{"role": "user", "content": prompt}],
                            system=system, model=model, max_tokens=max_tokens)
        return "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text")

    def count_tokens(self, messages, system=None, model=DEFAULT_MODEL):
        """POST /v1/messages/count_tokens -> input_tokens (int)."""
        payload = {"model": model, "messages": messages}
        sys_blocks = self._system_blocks(system)
        if sys_blocks:
            payload["system"] = sys_blocks
        return self._request("POST", "/v1/messages/count_tokens",
                             payload)["input_tokens"]

    def models(self):
        """GET /v1/models -> list of model dicts (id, display_name, ...)."""
        return self._request("GET", "/v1/models?limit=100").get("data", [])


# ----------------------------------------------------------- transcripts ---

def project_slug(path):
    """Map a working directory to its Claude Code project-folder name.
    Claude Code replaces each non-alphanumeric character in the absolute path
    with '-' (no collapsing), so 'D:\\claude' -> 'D--claude'."""
    abspath = os.path.abspath(path)
    return "".join(ch if ch.isalnum() else "-" for ch in abspath)


def project_dir(path=None):
    """The ~/.claude/projects/<slug> folder for a working directory."""
    return os.path.join(PROJECTS_DIR, project_slug(path or os.getcwd()))


def session_title(jsonl_path):
    """A human label for a session: the saved ai-title if present, else the
    first user prompt, truncated."""
    title = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "ai-title" and o.get("aiTitle"):
                    return o["aiTitle"].strip()
                if title is None and o.get("type") == "user":
                    text = _user_text(o.get("message", {}))
                    if text and not text.startswith("<"):
                        title = text.strip().replace("\n", " ")
    except OSError:
        return "(unreadable)"
    return (title[:70] + "...") if title and len(title) > 70 else (title or "(no prompt)")


def list_sessions(proj_dir):
    """Sessions in a project folder, newest first:
        [{id, path, mtime, size, title}, ...]."""
    sessions = []
    for path in glob.glob(os.path.join(proj_dir, "*.jsonl")):
        st = os.stat(path)
        sessions.append({
            "id": os.path.splitext(os.path.basename(path))[0],
            "path": path,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "title": session_title(path),
        })
    return sorted(sessions, key=lambda s: s["mtime"], reverse=True)


def resolve_session(ref, proj_dir):
    """Turn a CLI SESSION argument into a transcript path.
    - a path to an existing .jsonl -> itself
    - a session id -> <project>/<id>.jsonl
    - None -> the live session (most recently modified transcript)."""
    if ref:
        if os.path.isfile(ref):
            return ref
        candidate = os.path.join(proj_dir, ref if ref.endswith(".jsonl")
                                 else ref + ".jsonl")
        if os.path.isfile(candidate):
            return candidate
        sys.exit(f"no transcript for session '{ref}' in {proj_dir}")
    sessions = list_sessions(proj_dir)
    if not sessions:
        sys.exit(f"no sessions found in {proj_dir}")
    return sessions[0]["path"]


def set_session_title(jsonl_path, new_title):
    """Rename a session by setting the title Claude Code shows for it. The
    title lives in an `ai-title` line inside the transcript; update that line
    in place (or append one if the session was never titled). Every other line
    is preserved byte-for-byte. Returns True if an existing title was changed,
    False if a new one was added. Best used on a session that isn't live -
    rewriting the file under a running CLI could race its own writes."""
    sid = os.path.splitext(os.path.basename(jsonl_path))[0]
    out, replaced = [], False
    with open(jsonl_path, encoding="utf-8") as f:
        for raw in f:
            try:
                obj = json.loads(raw)
            except ValueError:
                out.append(raw)
                continue
            if obj.get("type") == "ai-title":
                obj["aiTitle"] = new_title
                out.append(json.dumps(obj) + "\n")
                replaced = True
            else:
                out.append(raw)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(json.dumps({"type": "ai-title", "aiTitle": new_title,
                               "sessionId": sid}) + "\n")
    tmp = jsonl_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, jsonl_path)
    return replaced


def _user_text(message):
    """Plain text from a user message (str content or list of blocks)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                parts.append(_tool_result_text(b))
        return "\n".join(p for p in parts if p)
    return ""


def _tool_result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def render_transcript(jsonl_path, include_thinking=False, trunc=2000):
    """Flatten a session JSONL into readable turn-by-turn text for
    summarisation. Skips bookkeeping lines (modes, snapshots, attachments);
    truncates oversized tool I/O so one runaway log can't dominate."""
    lines = []

    def clip(text):
        text = text or ""
        return text if len(text) <= trunc else text[:trunc] + " ...[truncated]"

    with open(jsonl_path, encoding="utf-8") as f:
        for raw in f:
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            t = o.get("type")
            msg = o.get("message", {}) if isinstance(o.get("message"), dict) else {}
            if t == "user":
                text = _user_text(msg).strip()
                if text:
                    lines.append(f"USER: {clip(text)}")
            elif t == "assistant":
                for b in msg.get("content", []):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        lines.append(f"ASSISTANT: {clip(b['text'].strip())}")
                    elif bt == "thinking" and include_thinking:
                        lines.append(f"(thinking) {clip(b.get('thinking', ''))}")
                    elif bt == "tool_use":
                        inp = json.dumps(b.get("input", {}))[:300]
                        lines.append(f"ASSISTANT -> tool {b.get('name')}: {inp}")
    return "\n".join(lines)


def chunk_text(text, max_chars):
    """Split on line boundaries into pieces of at most max_chars."""
    chunks, buf, size = [], [], 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > max_chars and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


# ------------------------------------------------------------ compaction ---

COMPACT_SYSTEM = (
    "You compact a software-engineering session into a precise resume handoff "
    "so a fresh assistant can pick up with zero loss of working context. Be "
    "concrete and specific; preserve exact file paths, identifiers, commands, "
    "and decisions. Omit chit-chat. Do not invent anything not in the "
    "transcript."
)

COMPACT_PROMPT = """Summarise the session transcript below as a structured \
resume handoff. Use exactly these sections, each as a markdown heading:

## Task & context
What the user is trying to achieve and any standing constraints.

## Work done
What was actually built, changed, run, or decided - in order.

## Current state
Where things stand right now: what works, what's verified, what's broken or open.

## Files & artifacts
Concrete paths/identifiers touched or created, each with a one-line note.

## Key decisions
Choices made and the reason, so they aren't re-litigated.

## Next step / resume point
The single most useful thing to do next, specifically.

Keep it tight - facts over prose. If a section has nothing, write "none".
{focus}
--- TRANSCRIPT START ---
{body}
--- TRANSCRIPT END ---"""

ROLLING_PROMPT = """This is part {n} of {total} of a long session transcript. \
Here is the running handoff so far (may be empty):

{running}

Update and extend it using the new transcript segment below, keeping the same \
section structure (Task & context / Work done / Current state / Files & \
artifacts / Key decisions / Next step). Carry forward everything still \
relevant; integrate the new material; do not drop earlier facts.

--- SEGMENT {n}/{total} ---
{body}
--- END SEGMENT ---"""


def compact_session(client, jsonl_path, focus=None, model=COMPACT_MODEL,
                    include_thinking=False, max_tokens=4096):
    """Render a transcript and have Claude produce a resume handoff. For
    transcripts that don't fit one request, summarise rolling chunk-by-chunk.
    Returns the handoff text."""
    body = render_transcript(jsonl_path, include_thinking=include_thinking)
    if not body.strip():
        return "(transcript had no conversational content to compact)"
    focus_line = f"\nFocus especially on: {focus}\n" if focus else ""

    max_chars = int(CHUNK_TOKENS * CHARS_PER_TOKEN)
    if len(body) <= max_chars:
        prompt = COMPACT_PROMPT.format(focus=focus_line, body=body)
        return client.text(prompt, system=COMPACT_SYSTEM, model=model,
                           max_tokens=max_tokens)

    chunks = chunk_text(body, max_chars)
    running = ""
    for i, chunk in enumerate(chunks, 1):
        prompt = ROLLING_PROMPT.format(n=i, total=len(chunks),
                                       running=running or "(empty)", body=chunk)
        running = client.text(prompt, system=COMPACT_SYSTEM, model=model,
                             max_tokens=max_tokens)
    return running


# -------------------------------------------------------------------- CLI ---

def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def human_age(mtime):
    secs = max(time.time() - mtime, 0)
    if secs < 3600:
        return f"{secs/60:.0f}m ago"
    if secs < 86400:
        return f"{secs/3600:.0f}h ago"
    return f"{secs/86400:.0f}d ago"


def cmd_whoami(args):
    auth = resolve_auth()
    print(f"auth source : {auth['source']}")
    if "oauth" in auth:
        o = auth["oauth"]
        exp = datetime.fromtimestamp(o.get("expiresAt", 0) / 1000).astimezone()
        secs = exp.timestamp() - time.time()
        when = (f"in {secs/3600:.0f}h" if secs > 3600 else
                f"in {secs/60:.0f}m" if secs > 0 else "EXPIRED")
        print(f"subscription: {o.get('subscriptionType', '?')}")
        print(f"scopes      : {', '.join(o.get('scopes', []))}")
        print(f"expires     : {exp:%Y-%m-%d %H:%M} ({when})")
        infer = "user:inference" in o.get("scopes", [])
        print(f"inference   : {'available' if infer else 'NOT GRANTED'}")
    proj = project_dir()
    print(f"project dir : {proj}{'' if os.path.isdir(proj) else '  (none yet)'}")


def cmd_sessions(args):
    proj = args.project or project_dir()
    sessions = list_sessions(proj)
    if not sessions:
        print(f"no sessions in {proj}")
        return
    print(f"# {len(sessions)} session(s) in {proj}  (newest first)\n")
    for i, s in enumerate(sessions):
        live = "  <- live" if i == 0 else ""
        print(f"{s['id']}")
        print(f"  {human_age(s['mtime']):<10} {human_size(s['size']):>7}  "
              f"{s['title']}{live}")


def cmd_rename(args):
    proj = project_dir()
    path = resolve_session(args.session, proj)
    old = session_title(path)
    replaced = set_session_title(path, args.title)
    verb = "renamed" if replaced else "titled"
    print(f"{verb} {os.path.basename(path)}")
    print(f"  {old!r} -> {args.title!r}")


def cmd_count(args):
    proj = project_dir()
    path = resolve_session(args.session, proj)
    body = render_transcript(path, include_thinking=args.thinking)
    client = AnthropicClient()
    est = int(len(body) / CHARS_PER_TOKEN)
    tokens = client.count_tokens([{"role": "user", "content": body or "."}],
                                 model=args.model)
    print(f"session : {os.path.basename(path)}")
    print(f"rendered: {len(body):,} chars  (~{est:,} est. tokens)")
    print(f"counted : {tokens:,} input tokens (API, model {args.model})")


def cmd_compact(args):
    proj = project_dir()
    path = resolve_session(args.session, proj)
    client = AnthropicClient()
    print(f"compacting {os.path.basename(path)} "
          f"({human_size(os.path.getsize(path))}) with {args.model}...",
          file=sys.stderr)
    handoff = compact_session(client, path, focus=args.focus, model=args.model,
                              include_thinking=args.thinking,
                              max_tokens=args.max_tokens)
    header = (f"# Resume handoff - {os.path.basename(path)}\n"
              f"# compacted {datetime.now():%Y-%m-%d %H:%M} via {args.model}\n\n")
    if args.out:
        mode = "a" if args.append else "w"
        with open(args.out, mode, encoding="utf-8") as f:
            if args.append:
                f.write("\n\n")
            f.write(header + handoff + "\n")
        print(f"{'appended to' if args.append else 'wrote'} {args.out}",
              file=sys.stderr)
    else:
        print(header + handoff)


def cmd_ask(args):
    client = AnthropicClient()
    reply = client.text(args.prompt, system=args.system, model=args.model,
                        max_tokens=args.max_tokens)
    print(reply)


def cmd_models(args):
    client = AnthropicClient()
    for m in client.models():
        print(f"{m.get('id'):<34} {m.get('display_name', '')}")


def build_parser():
    p = argparse.ArgumentParser(
        description="Anthropic API access + session compaction using Claude "
                    "Code's own on-disk token.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="auth source, scopes, expiry").set_defaults(
        func=cmd_whoami)

    s = sub.add_parser("sessions", help="list this project's sessions")
    s.add_argument("--project", help="project folder (default: cwd's project)")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("rename", help="rename a session (sets its title)")
    s.add_argument("title", help="new session title")
    s.add_argument("session", nargs="?", help="session id / .jsonl (default: live)")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("count", help="token count of a session transcript")
    s.add_argument("session", nargs="?", help="session id / .jsonl (default: live)")
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.add_argument("--thinking", action="store_true", help="include thinking blocks")
    s.set_defaults(func=cmd_count)

    s = sub.add_parser("compact", help="Claude-compact a session into a handoff")
    s.add_argument("session", nargs="?", help="session id / .jsonl (default: live)")
    s.add_argument("--out", help="write to this file instead of stdout")
    s.add_argument("--append", action="store_true",
                   help="append to --out instead of overwriting")
    s.add_argument("--focus", help="extra emphasis for the summary")
    s.add_argument("--model", default=COMPACT_MODEL)
    s.add_argument("--thinking", action="store_true",
                   help="feed assistant thinking into the summary too")
    s.add_argument("--max-tokens", type=int, default=4096, dest="max_tokens")
    s.set_defaults(func=cmd_compact)

    s = sub.add_parser("ask", help="one-shot prompt, prints the reply")
    s.add_argument("prompt")
    s.add_argument("--system", help="optional system prompt")
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.add_argument("--max-tokens", type=int, default=4096, dest="max_tokens")
    s.set_defaults(func=cmd_ask)

    sub.add_parser("models", help="list available models").set_defaults(
        func=cmd_models)
    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
