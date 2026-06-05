# Contributing to claude_helpers

**Anyone is welcome to contribute** — bug fixes, new helper scripts, tests,
documentation, or a well-described bug report. If it makes the tools better,
it's welcome.

Please be civil; this project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

---

## Send patches, not file replacements

This is the one thing we ask you to take seriously.

**Every change should be the smallest diff that does the job.** Prefer focused,
incremental **patches** over wholesale file replacements.

Concretely, a good contribution:

- **Touches only the lines it needs to.** Don't rewrite a whole file to change a
  few lines.
- **Doesn't reformat untouched code.** No drive-by whitespace or import
  reshuffles in code you aren't actually changing — they bury the real change.
- **Does one thing.** Keep unrelated changes in separate commits or separate
  pull requests. A reviewer should be able to read the diff and see *exactly*
  what changed and *why*.

---

## Getting set up

Plain Python — any 3.8+ interpreter is all you need (no pip installs).

```bash
python -m unittest discover -s tests    # runs the test suite, no network needed
python claude_usage.py                  # needs a logged-in Claude Code install
```

---

## Making a change

1. **Fork** (or branch off `main`).
2. Make your focused change, **with tests** for any behaviour change.
3. Run `python -m unittest discover -s tests` and make sure it's green locally.
4. Open a **pull request against `main`**. CI (compile + tests on Python 3.8
   and latest) runs automatically — keep it green.
5. Small, single-purpose PRs get reviewed and merged fastest.

---

## What a good change looks like here

A little context, so your patch fits the grain of the code:

- **Scripts stay stdlib-only.** No pip dependencies — anyone with a Python
  install should be able to run them, nothing to set up. Don't add a
  requirements.txt for a convenience import.
- **Keep network and rendering separate.** Fetching (`fetch_usage`) and
  display (`render`, `bar`, `fmt_reset`) are distinct functions — that's what
  makes the display logic unit-testable without a token or network. Keep new
  logic pure where you can and cover it with a test against canned data.
- **Credentials are read at runtime, never stored.** Tokens come from Claude
  Code's own `~/.claude/.credentials.json` at the moment of use. Never log,
  cache, or write them anywhere — and never commit sample responses with real
  account identifiers.

---

## Bugs, features, and security

- **Bugs and features:** please use the
  [issue templates](.github/ISSUE_TEMPLATE). Include the (redacted) endpoint
  JSON for display bugs.
- **Security:** do **not** open a public issue for a vulnerability — follow
  [SECURITY.md](SECURITY.md) instead.

---

## Licensing of contributions

This project is licensed under the **GNU GPL v3.0 (or later)** — see
[LICENSE](LICENSE). By submitting a contribution, you agree that it is licensed
under the same terms.

Thanks for helping out.
