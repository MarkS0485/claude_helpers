# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for a security vulnerability.

Email **info@twinscrollgridbalancer.co.uk** with details and steps to reproduce.
You'll get an acknowledgement within a few days and an indication of next steps
once the report has been assessed.

These scripts read the OAuth token from Claude Code's local credentials file and
talk to Anthropic's API over HTTPS — **anything that could leak that token is
squarely in scope**: writing it to disk or logs, sending it anywhere other than
the Anthropic endpoint, or exposing it in error output. Reports of that kind are
very welcome.

## Supported versions

Fixes land on `main`. Please reproduce against the latest `main` before
reporting.
