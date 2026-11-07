#!/usr/bin/env python3
"""
PreToolUse hook that keeps a repo session inside its own worktree.

    guard.py --writable <worktree> --writable <session dir> --protect <repos dir>

Claude Code pipes each tool call in as JSON on stdin. The hook answers:

    deny   Edit/Write/MultiEdit/NotebookEdit outside the writable dirs (a symlink inside them
           that points out counts as outside); Bash that writes into a protected path (the main
           checkouts and other sessions' worktrees: anything under --protect that is not writable);
           git rebase; force-push in any spelling.
    ask    any git push, gh pr create/merge/..., and commands that reach past the machine
           (kubectl, terraform, gcloud, aws, DB clients, publish, HTTP writes), wherever they sit
           in the command line: `git -C x push`, `env A=1 kubectl apply`, `cd y && gh pr create`.
    —      everything else: no output, so the session's own permission rules decide.

Bash is matched on its text, so a script that writes through an interpreter can slip past it.
It is a speed bump against mistakes, not a sandbox; turn on Claude Code's sandbox for that.
"""
import argparse
import json
import os
import re
import shlex
import sys

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Shell that writes files, and git that changes a checkout.
SHELL_WRITES = re.compile(
    r"(^|[\s;&|(])(rm|mv|cp|tee|touch|mkdir|rmdir|truncate|ln|chmod|chown|install|rsync|unzip|tar)\s"
    r"|>|\bsed\s+(-\w*\s+)*-i|\bperl\s+(-\w*\s+)*-i"
    r"|\bgit\b.*\b(commit|checkout|switch|reset|merge|pull|stash|add|rm|mv|restore|clean|cherry-pick|revert|tag|am|apply|worktree|branch\s+-[dDmMf])\b"
)
HARMLESS_REDIRECTS = re.compile(r"\d*>>?\s*&\s*\d+|&>\s*/dev/null|\d*>>?\s*/dev/null")

REBASE = re.compile(r"\bgit\b.*\brebase\b")
FORCE_PUSH = re.compile(r"\bgit\b.*\bpush\b.*(\s--force|\s-[a-zA-Z]*f[a-zA-Z]*\b|\s\+\S)")
PUSH = re.compile(r"\bgit\b.*\bpush\b|\bgh\b.*\bpr\s+(create|merge|ready|edit|close|reopen|comment|review)\b")
DANGER = re.compile(
    r"^\s*(kubectl\s+(apply|create|delete|edit|patch|replace|scale|rollout|annotate|label|exec)|"
    r"helm\s+(install|upgrade|uninstall|rollback|delete)|terraform\s+(apply|destroy|import|state)|"
    r"terragrunt\s+(apply|destroy|run-all|import)|gcloud\b|gsutil\s+(cp|mv|rm|rsync)|aws\b|"
    r"mysql\b|psql\b|mongo(sh)?\b|redis-cli\b|(npm|pnpm|yarn)\s+publish|docker\s+push|"
    r"curl\s+.*(-X\s*['\"]?(POST|PUT|PATCH|DELETE)|--data\b|-d\s|--form\b|-F\s)|wget\s+.*--post|"
    r"gh\s+api\s+.*(-X|--method|-f\s|-F\s|--field|--raw-field|--input)|"
    r"gh\s+(release|repo|secret|variable|workflow)\s+(create|delete|edit|set|run|rename))",
    re.I,
)
# Wrappers that run the command after them: `env X=1`, `sudo`, `sh -c "…"`, `xargs`, a subshell.
WRAPPERS = re.compile(r"^(?:(?:env|sudo|command|exec|nohup|time|xargs|nice)\s+(?:-\S+\s+)*|\w+=\S*\s+|(?:ba|z)?sh\s+-c\s+['\"]?|\(\s*)+")


def parts(cmd: str) -> list:
    return [WRAPPERS.sub("", p.strip()) for p in re.split(r"&&|\|\||;|\||\n|&", cmd) if p.strip()]


def inside(child: str, parent: str) -> bool:
    child, parent = os.path.normpath(child), os.path.normpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def real(p: str) -> str:
    return os.path.realpath(p)


def resolve(cwd: str, p: str) -> str:
    return os.path.normpath(os.path.join(cwd or "/", os.path.expanduser(str(p))))


def writable_path(target: str, writable: list) -> bool:
    return any(inside(target, w) and inside(real(target), real(w)) for w in writable)


def protected_path(target: str, writable: list, protect: list) -> bool:
    if writable_path(target, writable):
        return False
    return any(inside(target, p) or inside(real(target), real(p)) for p in protect)


def path_tokens(cmd: str) -> list:
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()
    out = []
    for t in tokens:
        t = t.split("=", 1)[1] if t.startswith("-") and "=" in t else t
        if t.startswith(("/", "~", "./", "../")) or t in (".", ".."):
            out.append(t)
    return out


COPY_LIKE = re.compile(r"^(cp|rsync|install|ln)\s")
REDIRECT_TARGET = re.compile(r"\d*>>?\|?\s*(['\"]?)([^\s'\";&|]+)\1")


def write_targets(part: str, here: str) -> list:
    """The paths one command of a chain would write. Reading a path is never a hit."""
    cleaned = HARMLESS_REDIRECTS.sub(" ", part)
    if not SHELL_WRITES.search(cleaned):
        return []
    targets = [resolve(here, m.group(2)) for m in REDIRECT_TARGET.finditer(cleaned)]
    without_redirects = REDIRECT_TARGET.sub(" ", cleaned)
    if not SHELL_WRITES.search(without_redirects):
        return targets  # only a redirect writes: the target is all that matters
    paths = [resolve(here, t) for t in path_tokens(without_redirects)]
    if COPY_LIKE.match(without_redirects.strip()):
        paths = paths[-1:]  # cp src... dest: the sources are only read
    # A relative write (rm x, git commit) lands where the command runs.
    return targets + (paths or [here])


def decide(tool: str, tool_input: dict, cwd: str, writable: list, protect: list):
    """(decision, reason), or None to leave it to the session's permission rules."""
    if tool in WRITE_TOOLS:
        target = resolve(cwd, tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if not writable_path(target, writable):
            return "deny", f"This session writes only inside {' and '.join(writable)}; {target} is outside them."
        return None

    if tool != "Bash":
        return None
    cmd = str(tool_input.get("command", ""))
    line = re.sub(r"\s+", " ", cmd)

    if REBASE.search(line):
        return "deny", "Never rebase in a plan session; merge the base branch in instead."
    if FORCE_PUSH.search(line):
        return "deny", "Never force-push from a plan session."

    # Writes into a main checkout or another session's worktree, by path or after a cd there.
    here, hits = cwd, []
    for part in parts(cmd):
        m = re.match(r"cd\s+(\S+)\s*$", part)
        if m:
            here = resolve(here, m.group(1).strip("'\""))
            continue
        hits += [t for t in write_targets(part, here) if protected_path(t, writable, protect)]
    if hits:
        return "deny", (f"{hits[0]} is outside this session's worktree (a main checkout or another session's). "
                        f"Write only inside {' and '.join(writable)}.")

    for part in parts(cmd):
        if PUSH.search(part):
            return "ask", "Push or pull-request change: needs the user's approval."
        if DANGER.search(part):
            return "ask", "Reaches past this machine (cluster, cloud, database, publish or HTTP write): needs the user's approval."
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--writable", action="append", default=[])
    ap.add_argument("--protect", action="append", default=[])
    args = ap.parse_args()
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return  # nothing to judge; let the permission rules decide
    verdict = decide(event.get("tool_name", ""), event.get("tool_input") or {}, event.get("cwd", ""),
                     [os.path.normpath(w) for w in args.writable], [os.path.normpath(p) for p in args.protect])
    if verdict:
        decision, reason = verdict
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
