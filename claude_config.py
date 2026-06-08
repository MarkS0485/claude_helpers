#!/usr/bin/env python3
"""Configure Claude Code (claude-cli) from the command line.

Manages ~/.claude/settings.json: permission bypass mode, environment
variables injected into every session (GitHub PAT and friends), and SSH
host/key entries. Stdlib only - no pip installs needed.

Secret values are prompted for with hidden input (or copied from the current
environment) and are written ONLY to your local settings.json - they never
appear in this repo, in command history, or in output (`show` masks them).

Usage:
  python claude_config.py show                          current config, secrets masked
  python claude_config.py bypass on|off                 permission bypass mode
  python claude_config.py env GH_TOKEN                  set a session env var (hidden prompt)
  python claude_config.py env GH_TOKEN --from-env       copy value from current environment
  python claude_config.py env GH_TOKEN --delete         remove it
  python claude_config.py ssh-key PATH                  git ssh identity (GIT_SSH_COMMAND)
  python claude_config.py ssh list                      configured SSH hosts
  python claude_config.py ssh add --id NAME --host USER@HOST --key PATH [--name LABEL]
  python claude_config.py ssh remove ID
  python claude_config.py ssh import                    register ~/.ssh hosts & keys
  python claude_config.py tokens                        list token env vars (masked)
"""

import argparse
import copy
import getpass
import glob
import json
import os
import sys

SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
SSH_DIR = os.path.expanduser("~/.ssh")

# env-var names containing any of these are masked by `show`
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PAT", "PASSWORD", "CREDENTIAL", "AUTH")

# env vars the estate expects for pushing / packaging
EXPECTED_TOKENS = ("GH_TOKEN", "NUGET_API_KEY")


# --------------------------------------------------------------- file I/O ---

def load_settings(path=SETTINGS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
            return settings if isinstance(settings, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError:
        sys.exit(f"{path} is not valid JSON - fix or delete it first "
                 "(refusing to overwrite a file I can't parse).")


def save_settings(settings, path=SETTINGS_PATH):
    """Write atomically, keeping a one-deep .bak of the previous version."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            previous = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(previous)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------- pure settings mutators ---

def set_bypass(settings, enabled):
    perms = settings.setdefault("permissions", {})
    if enabled:
        perms["defaultMode"] = "bypassPermissions"
    else:
        perms.pop("defaultMode", None)
        if not perms:
            del settings["permissions"]
    return settings


def set_env(settings, name, value):
    settings.setdefault("env", {})[name] = value
    return settings


def delete_env(settings, name):
    env = settings.get("env", {})
    if name not in env:
        sys.exit(f"env var {name} is not set in settings.json")
    del env[name]
    if not env:
        del settings["env"]
    return settings


def set_git_ssh_key(settings, key_path):
    """Point git-over-ssh at one identity file via GIT_SSH_COMMAND."""
    posix = key_path.replace("\\", "/")  # ssh on Windows is happy with /
    return set_env(settings, "GIT_SSH_COMMAND",
                   f'ssh -i "{posix}" -o IdentitiesOnly=yes')


def add_ssh_host(settings, host_id, host, key_path, name=None):
    configs = settings.setdefault("sshConfigs", [])
    if any(c.get("id") == host_id for c in configs):
        sys.exit(f"ssh host id '{host_id}' already exists - "
                 "remove it first or pick another id")
    configs.append({
        "id": host_id,
        "name": name or host_id,
        "sshHost": host,
        "sshIdentityFile": key_path,
    })
    return settings


def remove_ssh_host(settings, host_id):
    configs = settings.get("sshConfigs", [])
    kept = [c for c in configs if c.get("id") != host_id]
    if len(kept) == len(configs):
        sys.exit(f"no ssh host with id '{host_id}'")
    if kept:
        settings["sshConfigs"] = kept
    else:
        del settings["sshConfigs"]
    return settings


# ----------------------------------------------------- ssh import (pure) ---

def parse_ssh_config(text):
    """Parse an ssh config into a list of {host, hostname, user, identityfile}
    blocks (one per `Host` stanza; wildcard '*' hosts are skipped)."""
    blocks = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "host":
            if current is not None:
                blocks.append(current)
            current = {"host": value, "hostname": None,
                       "user": None, "identityfile": None}
        elif current is None:
            continue
        elif key == "hostname":
            current["hostname"] = value
        elif key == "user":
            current["user"] = value
        elif key == "identityfile":
            current["identityfile"] = os.path.expanduser(value)
    if current is not None:
        blocks.append(current)
    return [b for b in blocks if "*" not in b["host"]]


def ssh_host_string(block):
    """user@hostname (or just hostname) from a parsed config block."""
    host = block.get("hostname") or block["host"]
    user = block.get("user")
    return f"{user}@{host}" if user else host


def _config_id(block):
    return block["host"]


def import_ssh_entries(settings, config_blocks, pub_keys):
    """Merge ssh config blocks + pub-key files into settings['sshConfigs'],
    idempotently (match by id or by identity file). Returns (settings, added)
    where added is a list of the ids newly registered.

    - Each config block becomes an entry keyed by its Host alias.
    - Each *.pub with no matching Host block (by private-key path) becomes a
      placeholder entry (sshHost 'TODO@TODO') flagging it needs a host.
    """
    configs = settings.setdefault("sshConfigs", [])
    existing_ids = {c.get("id") for c in configs}
    existing_keys = {os.path.normcase(os.path.normpath(c["sshIdentityFile"]))
                     for c in configs if c.get("sshIdentityFile")}
    added = []

    def norm(p):
        return os.path.normcase(os.path.normpath(p)) if p else None

    # config blocks first (they carry a real host)
    config_key_paths = set()
    for block in config_blocks:
        host_id = _config_id(block)
        ident = block.get("identityfile")
        if ident:
            config_key_paths.add(norm(ident))
        if host_id in existing_ids or (ident and norm(ident) in existing_keys):
            continue
        configs.append({
            "id": host_id,
            "name": host_id,
            "sshHost": ssh_host_string(block),
            "sshIdentityFile": ident or "",
        })
        existing_ids.add(host_id)
        if ident:
            existing_keys.add(norm(ident))
        added.append(host_id)

    # public keys with no matching Host block -> placeholder entries
    for pub in pub_keys:
        priv = pub[:-4] if pub.endswith(".pub") else pub  # strip .pub
        key_id = os.path.basename(priv)
        if norm(priv) in config_key_paths or norm(priv) in existing_keys:
            continue
        if key_id in existing_ids:
            continue
        configs.append({
            "id": key_id,
            "name": f"{key_id} (needs a host - set sshHost)",
            "sshHost": "TODO@TODO",
            "sshIdentityFile": priv,
        })
        existing_ids.add(key_id)
        existing_keys.add(norm(priv))
        added.append(key_id)

    return settings, added


# ----------------------------------------------------------------- display ---

def is_secret_name(name):
    upper = name.upper()
    # PAT only as a whole word, else PATH/REPATH would be masked too
    if "PAT" in upper.replace("-", "_").split("_"):
        return True
    return any(hint in upper for hint in SECRET_HINTS if hint != "PAT")


def mask(value):
    if not isinstance(value, str) or len(value) <= 8:
        return "********"
    return value[:4] + "*" * 8  # enough prefix to recognise, no more


def masked_view(settings):
    """Deep copy with secret-looking env values masked - safe to print."""
    view = copy.deepcopy(settings)
    for name, value in view.get("env", {}).items():
        if is_secret_name(name):
            view["env"][name] = mask(value)
    return view


def token_entries(settings):
    """Token-like env entries as (name, masked_value) pairs, name-sorted.
    Never returns raw secret values."""
    env = settings.get("env", {})
    return [(name, mask(env[name]))
            for name in sorted(env) if is_secret_name(name)]


# -------------------------------------------------------------------- CLI ---

def confirm(question):
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def main():
    parser = argparse.ArgumentParser(
        description="Configure Claude Code via ~/.claude/settings.json")
    parser.add_argument("--settings", default=SETTINGS_PATH,
                        help="settings file to edit (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="print current settings, secrets masked")

    p = sub.add_parser("bypass", help="permission bypass mode")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--yes", action="store_true", help="skip confirmation")

    p = sub.add_parser("env", help="set/delete a session env var")
    p.add_argument("name", help="variable name, e.g. GH_TOKEN")
    p.add_argument("--from-env", action="store_true",
                   help="copy the value from the current environment")
    p.add_argument("--delete", action="store_true")

    p = sub.add_parser("ssh-key", help="git ssh identity file")
    p.add_argument("path", help="path to the private key")

    p = sub.add_parser("ssh", help="manage SSH host entries")
    ssh_sub = p.add_subparsers(dest="ssh_command", required=True)
    ssh_sub.add_parser("list")
    pa = ssh_sub.add_parser("add")
    pa.add_argument("--id", required=True, help="short id, e.g. ovh")
    pa.add_argument("--host", required=True, help="user@host")
    pa.add_argument("--key", required=True, help="path to the private key")
    pa.add_argument("--name", help="display label (defaults to id)")
    pr = ssh_sub.add_parser("remove")
    pr.add_argument("id")
    pi = ssh_sub.add_parser(
        "import", help="register ~/.ssh/config hosts and *.pub keys")
    pi.add_argument("--ssh-dir", default=SSH_DIR,
                    help="ssh directory to read (default: %(default)s)")

    sub.add_parser("tokens", help="list token env vars (masked) + expected set")

    args = parser.parse_args()
    settings = load_settings(args.settings)

    if args.command == "show":
        print(f"# {args.settings}")
        print(json.dumps(masked_view(settings), indent=2))
        return

    if args.command == "bypass":
        if args.state == "on" and not args.yes:
            print("Bypass mode lets Claude Code run commands and edit files "
                  "WITHOUT asking first.")
            if not confirm("Enable it?"):
                sys.exit("aborted")
        set_bypass(settings, args.state == "on")
        save_settings(settings, args.settings)
        print(f"permission bypass: {args.state}")
        return

    if args.command == "env":
        if args.delete:
            delete_env(settings, args.name)
            save_settings(settings, args.settings)
            print(f"{args.name}: removed")
            return
        if args.from_env:
            value = os.environ.get(args.name)
            if value is None:
                sys.exit(f"{args.name} is not set in the current environment")
        else:
            value = getpass.getpass(f"Value for {args.name} (input hidden): ")
            if not value:
                sys.exit("empty value - nothing changed")
        set_env(settings, args.name, value)
        save_settings(settings, args.settings)
        shown = mask(value) if is_secret_name(args.name) else value
        print(f"{args.name} = {shown}")
        return

    if args.command == "ssh-key":
        if not os.path.exists(args.path):
            sys.exit(f"key file not found: {args.path}")
        set_git_ssh_key(settings, args.path)
        save_settings(settings, args.settings)
        print(f"GIT_SSH_COMMAND -> {settings['env']['GIT_SSH_COMMAND']}")
        return

    if args.command == "ssh":
        if args.ssh_command == "list":
            for c in settings.get("sshConfigs", []):
                print(f"{c.get('id'):<10} {c.get('sshHost'):<30} "
                      f"{c.get('sshIdentityFile')}  ({c.get('name')})")
            if not settings.get("sshConfigs"):
                print("no SSH hosts configured")
            return
        if args.ssh_command == "add":
            if not os.path.exists(args.key):
                sys.exit(f"key file not found: {args.key}")
            add_ssh_host(settings, args.id, args.host, args.key, args.name)
            save_settings(settings, args.settings)
            print(f"added ssh host '{args.id}' ({args.host})")
            return
        if args.ssh_command == "remove":
            remove_ssh_host(settings, args.id)
            save_settings(settings, args.settings)
            print(f"removed ssh host '{args.id}'")
            return
        if args.ssh_command == "import":
            cfg_path = os.path.join(args.ssh_dir, "config")
            blocks = []
            if os.path.exists(cfg_path):
                with open(cfg_path, encoding="utf-8") as f:
                    blocks = parse_ssh_config(f.read())
            pubs = sorted(glob.glob(os.path.join(args.ssh_dir, "*.pub")))
            _, added = import_ssh_entries(settings, blocks, pubs)
            if added:
                save_settings(settings, args.settings)
                print(f"registered {len(added)} ssh entr"
                      f"{'y' if len(added) == 1 else 'ies'}: "
                      + ", ".join(added))
                placeholders = [c for c in settings.get("sshConfigs", [])
                                if c.get("id") in added
                                and c.get("sshHost") == "TODO@TODO"]
                for c in placeholders:
                    print(f"  NOTE: '{c['id']}' has no Host block - "
                          "set sshHost before use")
            else:
                print("nothing to import - all ssh entries already registered")
            return

    if args.command == "tokens":
        print(f"# token env vars in {args.settings} (masked)")
        entries = token_entries(settings)
        if entries:
            for name, masked in entries:
                print(f"  {name} = {masked}")
        else:
            print("  (none)")
        print("# expected:")
        env = settings.get("env", {})
        for name in EXPECTED_TOKENS:
            print(f"  {name}: {'present' if name in env else 'MISSING'}")
        return


if __name__ == "__main__":
    main()
