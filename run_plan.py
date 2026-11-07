#!/usr/bin/env python3
"""
Run one plan as one Claude Code session per repo, in tmux, ordered by dependency.

    run_plan.py read     <plan.md>        Opus reads plan.md (+ sibling tasks.md) -> sessions.json, prints the preview
    run_plan.py model    <KEY> <repo|all> <opus|sonnet|haiku|claude-...>   pick the model a repo session runs on
    run_plan.py exercise <KEY> [repo...]  Opus reads the plan against each repo's real code, read-only, before any is written
    run_plan.py start    <KEY|plan.md>    tmux session plan-<KEY>: orchestrator + watcher; the watcher starts repo sessions
    run_plan.py status   <KEY>            where every repo session stands, its model and what it cost
    run_plan.py watch  <KEY>              the watcher loop (start runs it in its own window; rerun it if it died)
    run_plan.py stop   <KEY>              kill the tmux session (worktrees and reports stay)

Layout, all under $TMUX_ORCH_HOME (default <repos>/.tmux-orch):

    <KEY>/sessions.json          the reading: repo sessions, their tasks and depends_on (edit it before start if needed)
    <KEY>/state.json             watcher state
    <KEY>/sessions/<repo>/       exercise.json, brief.md (in), report.md / question.md (out), cost.json
    <KEY>/worktrees/<repo>/      git worktree on branch share/<KEY>, cut from the remote default branch
    <KEY>/costs.json             what read and exercise cost; sessions keep theirs in sessions/<repo>/cost.json

Costs are Claude Code's own total_cost_usd: API-priced. On a subscription (Pro, Max, Team) that
is an estimate of the equivalent API cost, not what you are billed.

A repo session starts once every repo it depends on has a report with `status: done`. A report
with `partial` or `blocked` leaves its dependents unstarted until a later report says done.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOS = Path(os.environ.get("TMUX_ORCH_REPOS", "~/code")).expanduser()
HOME = Path(os.environ.get("TMUX_ORCH_HOME", REPOS / ".tmux-orch")).expanduser()
CLAUDE = os.environ.get("TMUX_ORCH_CLAUDE") or shutil.which("claude") or "claude"
READ_MODEL = os.environ.get("TMUX_ORCH_READ_MODEL", "opus")
ORCH_MODEL = os.environ.get("TMUX_ORCH_ORCH_MODEL", "opus")
REPO_MODEL = os.environ.get("TMUX_ORCH_REPO_MODEL", "sonnet")
EXERCISE_MODEL = os.environ.get("TMUX_ORCH_EXERCISE_MODEL", "opus")
EXERCISE_PARALLEL = int(os.environ.get("TMUX_ORCH_EXERCISE_PARALLEL", "4"))
EXERCISE_BUDGET = os.environ.get("TMUX_ORCH_EXERCISE_BUDGET_USD")  # per repo, optional cap
POLL_SECONDS = float(os.environ.get("TMUX_ORCH_POLL", "10"))
IDLE_MINUTES = float(os.environ.get("TMUX_ORCH_IDLE_MINUTES", "10"))

TERMINAL = {"done", "partial", "blocked"}
MODEL_ALIASES = ("opus", "sonnet", "haiku")
ORCH = "_orchestrator"  # its cost lives in sessions/_orchestrator/cost.json


def model_ok(model: str) -> bool:
    return model in MODEL_ALIASES or model.startswith("claude-")


# ---------------------------------------------------------------------------------------------
# Costs


def cost_of(stdout: str) -> float:
    """total_cost_usd from `claude -p --output-format json`."""
    try:
        return float(json.loads(stdout).get("total_cost_usd") or 0)
    except (ValueError, AttributeError):
        return 0.0


def read_costs(key: str) -> dict:
    try:
        c = json.loads((HOME / key / "costs.json").read_text())
    except (OSError, ValueError):
        c = {}
    return {"read": c.get("read", 0.0), "exercise": c.get("exercise", {})}


def add_cost(key: str, phase: str, usd: float, repo: str = ""):
    """read is one number (re-reading adds to it); exercise is per repo (a re-run adds too: it was spent)."""
    c = read_costs(key)
    if phase == "read":
        c["read"] += usd
    else:
        c["exercise"][repo] = c["exercise"].get(repo, 0.0) + usd
    f = HOME / key / "costs.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2))
    tmp.replace(f)


def session_cost(key: str, repo: str) -> float:
    """What an interactive session spent, as its status line last reported: every Claude process
    the window ran (a `claude --continue` after a crash starts its own count), summed."""
    try:
        by = json.loads((HOME / key / "sessions" / repo / "cost.json").read_text()).get("by_session", {})
        return sum(float(v.get("usd", 0)) for v in by.values())
    except (OSError, ValueError, AttributeError):
        return 0.0


def usd(x: float) -> str:
    return "<$0.01" if 0 < x < 0.005 else f"${x:,.2f}"

# ---------------------------------------------------------------------------------------------
# Plan files and keys


def plan_key(plan: Path) -> str:
    """`PROJ-123-x.md` -> PROJ-123; `tasks/PROJ-123-x/plan.md` -> PROJ-123; else the file name."""
    def key_of(name):
        m = re.match(r"([A-Z][A-Z0-9]*-\d+)", name) or re.match(r"([A-Za-z]+-[^-]+)", name)
        return m and m.group(1)
    return (key_of(plan.stem) or key_of(plan.parent.name) or plan.stem)[:40]


def plan_texts(plan: Path) -> dict:
    """plan.md, and tasks.md when it sits beside it (the lg-task layout)."""
    out = {"plan": plan.read_text()}
    tasks = plan.parent / "tasks.md"
    if tasks.exists() and tasks != plan:
        out["tasks"] = tasks.read_text()
    return out


def texts_hash(texts: dict) -> str:
    return hashlib.sha256(json.dumps(texts, sort_keys=True).encode()).hexdigest()[:16]


def local_repos() -> list:
    return sorted(p.name for p in REPOS.iterdir() if (p / ".git").exists()) if REPOS.exists() else []


def mentioned_repos(texts: dict, repos: list) -> set:
    """Repos the plan names in a way no reading should miss: `**Repo:** x`, or `x` in a heading."""
    found = set()
    for text in texts.values():
        for line in text.splitlines():
            m = re.match(r"\s*-\s*\*\*Repo:\*\*\s*(.*)", line, re.I)
            if m:
                found |= {r for r in repos if re.search(rf"(?<![\w-]){re.escape(r)}(?![\w-])", m.group(1))}
            if line.startswith("#"):
                found |= {r for r in re.findall(r"`([\w.-]+)`", line) if r in repos}
    return found


# ---------------------------------------------------------------------------------------------
# Sessions: validation and order


def validate(sessions: list, repos: list) -> list:
    """Problems that make a run impossible. Empty when the sessions can run."""
    problems, names = [], [s["repo"] for s in sessions]
    for s in sessions:
        if s["repo"] not in repos:
            problems.append(f"{s['repo']}: not a git checkout under {REPOS}")
        for d in s.get("depends_on", []):
            if d not in names:
                problems.append(f"{s['repo']}: depends on {d}, which has no session in this plan")
            if d == s["repo"]:
                problems.append(f"{s['repo']}: depends on itself")
    if len(set(names)) != len(names):
        problems.append("a repo has more than one session; merge its tasks into one")
    if not problems:
        try:
            waves(sessions)
        except ValueError as e:
            problems.append(str(e))
    return problems


def waves(sessions: list) -> list:
    """Repos grouped by the round they can start in. Raises on a cycle."""
    deps = {s["repo"]: set(s.get("depends_on", [])) for s in sessions}
    done, out = set(), []
    while len(done) < len(deps):
        ready = sorted(r for r, d in deps.items() if r not in done and d <= done)
        if not ready:
            raise ValueError("depends_on forms a cycle among: " + ", ".join(sorted(set(deps) - done)))
        out.append(ready)
        done |= set(ready)
    return out


def ready_to_start(sessions: list, state: dict) -> list:
    """Pending repos whose every dependency has reported done."""
    st = state["repos"]
    return [
        s["repo"] for s in sessions
        if st[s["repo"]]["status"] == "pending" and all(st[d]["status"] == "done" for d in s.get("depends_on", []))
    ]


def print_preview(data: dict):
    print(f"\nPlan {data['key']}: {len(data['sessions'])} repo session(s)\n")
    by_repo = {s["repo"]: s for s in data["sessions"]}
    for i, wave in enumerate(waves(data["sessions"]), 1):
        print(f"Wave {i}" + (" (parallel)" if len(wave) > 1 else ""))
        for r in wave:
            s = by_repo[r]
            after = f"  after {', '.join(s['depends_on'])}" if s.get("depends_on") else ""
            base = f"  from origin/{s['base'].removeprefix('origin/')}" if s.get("base") else ""
            ex = exercise_state(data, s)
            print(f"  - {r}  [{s.get('model') or REPO_MODEL}]{base}{after}  ({ex})")
            for t in s["tasks"]:
                print(f"      {t.get('id') or '-'}  {t['title']}")
    if data.get("unassigned"):
        print("\nNot given to any session (yours to handle):")
        for u in data["unassigned"]:
            print(f"  - {u['title']}  ({u['why']})")
    if data.get("notes"):
        print("\nRules every session gets:")
        for n in data["notes"]:
            print(f"  - {n}")
    c = read_costs(data["key"])
    spent = c["read"] + sum(c["exercise"].values())
    if spent:
        print(f"\nSpent so far: read {usd(c['read'])} · exercise {usd(sum(c['exercise'].values()))}")
    print(f"\nModel: {Path(sys.argv[0]).name} model {data['key']} <repo|all> <opus|sonnet|haiku>"
          f"   Exercise: {Path(sys.argv[0]).name} exercise {data['key']}")
    print()


# ---------------------------------------------------------------------------------------------
# read: Opus turns the plan into sessions.json

READ_SCHEMA = {
    "type": "object",
    "required": ["sessions", "unassigned", "notes"],
    "properties": {
        "sessions": {"type": "array", "items": {
            "type": "object", "required": ["repo", "tasks", "depends_on"],
            "properties": {
                "repo": {"type": "string"},
                "tasks": {"type": "array", "items": {
                    "type": "object", "required": ["id", "title", "detail"],
                    "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "detail": {"type": "string"}}}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "base": {"type": "string"},
            }}},
        "unassigned": {"type": "array", "items": {
            "type": "object", "required": ["title", "why"],
            "properties": {"title": {"type": "string"}, "why": {"type": "string"}}}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}

READ_PROMPT = """You split an implementation plan into coding sessions: exactly one session per git repo that has code work in it. Each session is a Claude Code agent working alone in its own worktree of that repo.

Repos on this machine (use these exact names, nothing else):
{repos}

Return:
- sessions: one per repo with code work.
  - tasks: every piece of that repo's work, in order. id = the plan's own id (T2, S1, step 3...) or "". detail = complete instructions for the engineer: files, functions, line anchors, tests, gates and pitfalls from the plan, kept verbatim where it matters. Do not summarise away specifics.
  - depends_on: repos whose session must FINISH before this one can start, because this repo's work consumes something that repo's work produces and cannot be written or tested without it (a new API, library version, schema, generated client). If both can be written in parallel, leave it empty. Only repos that have a session.
  - base: the remote branch this repo's work must start from when the plan says so (an unmerged upstream PR branch, say), without "origin/". Empty means the repo's default branch.
- unassigned: work that is not code in one of the repos above (deploys, migrations run on an environment, manual ops, Jira, config domains, repos not on this machine), each with why.
- notes: up to 8 rules from the plan that every session must follow.

Skip work the plan marks as already done (DONE, ☑, Status: done, "not needed").

{files}"""


def run_read(plan: Path):
    plan = plan.resolve()
    texts = plan_texts(plan)
    repos = local_repos()
    if not repos:
        sys.exit(f"No git checkouts under {REPOS}")
    files = "\n\n".join(f"=== {name}.md ===\n{text}" for name, text in texts.items())
    prompt = READ_PROMPT.format(repos="\n".join(f"- {r}" for r in repos), files=files)
    print(f"Reading {plan} ({', '.join(f'{k}.md' for k in texts)}) with {READ_MODEL}...", file=sys.stderr)
    proc = subprocess.run(
        [CLAUDE, "-p", "--model", READ_MODEL, "--output-format", "json", "--tools", "",
         "--json-schema", json.dumps(READ_SCHEMA)],
        input=prompt, capture_output=True, text=True,
    )
    key = plan_key(plan)
    add_cost(key, "read", cost_of(proc.stdout))  # spent even if the answer is unusable
    if proc.returncode != 0:
        sys.exit(f"claude -p failed ({proc.returncode}):\n{proc.stderr or proc.stdout}")
    reading = parse_reading(proc.stdout)
    print(f"Read with {READ_MODEL}: {usd(cost_of(proc.stdout))}", file=sys.stderr)

    data = {
        "key": key, "plan": str(plan),
        "tasks_file": str(plan.parent / "tasks.md") if "tasks" in texts else None,
        "plan_hash": texts_hash(texts), "read_at": datetime.now().isoformat(timespec="seconds"),
        "read_model": READ_MODEL, **reading,
    }
    out = HOME / key / "sessions.json"
    try:  # a model you already picked for a repo survives reading the plan again
        picked = {s["repo"]: s.get("model") for s in json.loads(out.read_text())["sessions"]}
    except (OSError, ValueError, KeyError):
        picked = {}
    for s in data["sessions"]:
        s["model"] = picked.get(s["repo"]) or REPO_MODEL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    print_preview(data)
    missed = mentioned_repos(texts, repos) - {s["repo"] for s in data["sessions"]} - {
        r for u in data["unassigned"] for r in repos if r in u["title"] or r in u["why"]}
    for r in sorted(missed):
        print(f"WARNING: the plan names `{r}` as a repo but no session or unassigned item covers it.")
    for p in validate(data["sessions"], repos):
        print(f"PROBLEM: {p}")
    print(f"Saved {out}\nEdit it if the split or order is wrong, then: {Path(sys.argv[0]).name} start {key}")


def save_sessions(data: dict):
    f = HOME / data["key"] / "sessions.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(f)


def run_model(key: str, repo: str, model: str):
    data = load(key)
    if not model_ok(model):
        sys.exit(f"Unknown model {model!r}: use {', '.join(MODEL_ALIASES)} or a full id such as claude-sonnet-5-5")
    names = [s["repo"] for s in data["sessions"]]
    if repo != "all" and repo not in names:
        sys.exit(f"{repo} has no session in {key}. Sessions: {', '.join(names)}")
    for s in data["sessions"]:
        if repo in ("all", s["repo"]):
            s["model"] = model
    save_sessions(data)
    state = HOME / key / "state.json"
    running = json.loads(state.read_text())["repos"] if state.exists() else {}
    late = [r for r, st in running.items() if st.get("status") != "pending" and repo in ("all", r)]
    print_preview(data)
    if late:
        print(f"Note: {', '.join(late)} already started; the new model applies only to sessions not yet started.")


def parse_reading(stdout: str) -> dict:
    """The structured output of `claude -p --output-format json --json-schema`."""
    outer = json.loads(stdout)
    if isinstance(outer.get("structured_output"), dict):
        return outer["structured_output"]
    text = outer.get("result", "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        sys.exit(f"The reading came back without JSON:\n{text[:2000]}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------------------------------------
# tmux


SOCKET = os.environ.get("TMUX_ORCH_SOCKET")
TMUX = ["tmux", *(["-L", SOCKET] if SOCKET else [])]


def env_flags() -> list:
    """tmux windows do not inherit this shell's environment: hand our settings on explicitly."""
    return [x for k, v in os.environ.items() if k.startswith("TMUX_ORCH_") for x in ("-e", f"{k}={v}")]


def tmux(*args, check=True) -> str:
    r = subprocess.run([*TMUX, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def tmux_name(s: str) -> str:
    return re.sub(r"[^\w-]", "_", s)


def has_session(name: str) -> bool:
    return subprocess.run([*TMUX, "has-session", "-t", f"={name}"], capture_output=True).returncode == 0


def windows(session: str) -> set:
    return set(tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}", check=False).split())


def new_window(session: str, name: str, cwd: Path, argv: list):
    tmux("new-window", "-d", "-t", f"={session}:", "-n", name, "-c", str(cwd), *env_flags(), shlex.join(argv))
    pin_name(session, name)


def pin_name(session: str, name: str):
    # Claude sets the terminal title; without this tmux renames the window and targets break.
    # remain-on-exit keeps a window that exits (crash, /exit) readable instead of vanishing.
    for opt, val in (("automatic-rename", "off"), ("allow-rename", "off"), ("remain-on-exit", "on")):
        tmux("set-option", "-w", "-t", f"={session}:{name}", opt, val, check=False)


def send(session: str, window: str, message: str):
    target = f"={session}:{window}"
    tmux("send-keys", "-t", target, "-l", message)
    time.sleep(0.5)
    tmux("send-keys", "-t", target, "Enter")


def pane_dead(session: str, window: str) -> bool:
    return tmux("display-message", "-p", "-t", f"={session}:{window}", "#{pane_dead}", check=False).strip() == "1"


def capture(session: str, window: str, lines: int = 60) -> str:
    return tmux("capture-pane", "-p", "-t", f"={session}:{window}", "-S", f"-{lines}", check=False)


# ---------------------------------------------------------------------------------------------
# git


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def prepare_worktree(repo: str, key: str, base: str = "") -> dict:
    """A worktree on share/<key>, cut from the freshly fetched remote `base` (default branch when empty), reused if present."""
    repo_path, wt = REPOS / repo, HOME / key / "worktrees" / repo
    branch = f"share/{key}"
    git(repo_path, "fetch", "--quiet", "origin")
    if base:
        base_ref = base if base.startswith("origin/") else f"origin/{base}"
    else:
        try:
            base_ref = git(repo_path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        except subprocess.CalledProcessError:
            base_ref = "origin/main"
    try:
        base_sha = git(repo_path, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
    except subprocess.CalledProcessError:
        raise RuntimeError(f"{repo}: base {base_ref} does not exist on origin (merged and deleted? fix `base` in sessions.json)")
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo_path, "worktree", "prune")
        exists = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                                capture_output=True).returncode == 0
        if exists:
            git(repo_path, "worktree", "add", str(wt), branch)
        else:
            git(repo_path, "worktree", "add", "-b", branch, str(wt), base_sha)
    else:
        # Reused (the exercise made it, or an earlier start): it sits where it was cut, not on
        # whatever origin moved to since. Say so, so the brief and the exercise agree.
        try:
            base_sha = git(wt, "merge-base", "HEAD", base_ref)
        except subprocess.CalledProcessError:
            pass
    return {"worktree": str(wt), "branch": branch, "base_ref": base_ref, "base_sha": base_sha}


# ---------------------------------------------------------------------------------------------
# Session prompts and settings

REPO_SETTINGS = {
    "permissions": {
        "allow": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
        "ask": [
            "Bash(git push:*)", "Bash(gh pr create:*)", "Bash(gh pr merge:*)", "Bash(gh pr ready:*)",
            "Bash(gh pr edit:*)", "Bash(gh pr close:*)", "Bash(gh pr comment:*)", "Bash(gh pr review:*)",
            "Bash(gh api:*)", "Bash(gh release:*)", "Bash(gh workflow:*)", "Bash(kubectl:*)", "Bash(helm:*)",
            "Bash(terraform:*)", "Bash(terragrunt:*)", "Bash(gcloud:*)", "Bash(gsutil:*)", "Bash(aws:*)",
            "Bash(mysql:*)", "Bash(psql:*)", "Bash(mongosh:*)", "Bash(redis-cli:*)", "Bash(npm publish:*)",
            "Bash(docker push:*)",
        ],
        "deny": ["Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push --force-with-lease:*)"],
    }
}

GUARDED_TOOLS = "Bash|Edit|Write|MultiEdit|NotebookEdit"


def cost_status_line(session_dir) -> dict:
    """Claude Code hands its status line the session's running total_cost_usd; statusline.py keeps it."""
    return {"type": "command", "command": shlex.join([sys.executable, str(HERE / "statusline.py"), str(session_dir)])}


def repo_settings(worktree, session_dir) -> dict:
    """Permission rules plus guard.py as a PreToolUse hook: writes stay in the worktree and session folder."""
    guard = shlex.join([sys.executable, str(HERE / "guard.py"), "--writable", str(worktree),
                        "--writable", str(session_dir), "--protect", str(REPOS), "--protect", str(HOME)])
    return {**REPO_SETTINGS, "statusLine": cost_status_line(session_dir), "hooks": {"PreToolUse": [
        {"matcher": GUARDED_TOOLS, "hooks": [{"type": "command", "command": guard, "timeout": 10}]}]}}


SESSION_RULES = """You are a repo session in a multi-repo plan run by an orchestrator over tmux. Your brief is {brief}.
- Do only the tasks in your brief. Anything else goes under Open questions in your report.
- Work only in your worktree ({worktree}) and your session folder ({session_dir}). Commit small, meaningful commits on {branch}. Never rebase, never force-push, never touch the main checkout under {repos}.
- git push and gh pr create wait for the user's approval in this window. Do them last, only once tests pass, and only if the brief asks for them.
- Stop and report with status partial on: the same test/build error twice after a fix attempt; 15 minutes without progress; a change to a high-risk area the plan did not name (migrations, payments, auth, shared config, public APIs, shared libraries); or a migration turning out to be needed.
- If you cannot continue without an answer, write {session_dir}/question.md (the question, what you tried, the options you see) and stop. The orchestrator will answer here.
- When finished, write {session_dir}/report.md in exactly this shape, and that is your last action:

---
status: done | partial | blocked
---
## Summary
What changed and why, in a few sentences.
## Commits
- <short sha> <subject>
## Decisions
- choices the plan left open, each with its reason
## Open questions
- anything the orchestrator or the user must decide
## Next step
- (partial/blocked only) the one thing that would unblock you; the exact error if there is one"""

ORCH_RULES = """You are the orchestrator for plan {key} ({plan}). One Claude Code session per repo runs in its own window of tmux session {session}; a watcher (window "watcher") starts each repo session when the repos it depends on have reported done, and sends you a line starting with [watcher] whenever something happens.
- Never edit repo code. Repo sessions do the work; you read their reports and keep the user posted.
- On "[watcher] <repo> reported": read {home}/sessions/<repo>/report.md and tell the user: status, what changed, commits, open questions, anything that needs them.
- On "[watcher] <repo> asks": read {home}/sessions/<repo>/question.md. Answer only when the plan clearly answers it; otherwise ask the user. Send the answer with: {send} {session}:<repo> "<answer>". Then rename the question file to question-answered-<time>.md.
- On "[watcher] <repo> is waiting for approval" or "looks idle": look with tmux capture-pane -p -t {session}:<repo> and tell the user what it waits for. Approvals are the user's to give in that window; never approve a push or PR yourself.
- To see the whole run: {status}
- sessions.json (the split and order) is {home}/sessions.json. If the user asks to change it, edit it and restart the watcher with: {watch}"""


def brief_for(data: dict, s: dict, info: dict, parents: list, exercise=None) -> str:
    tasks = tasks_text(s)
    upstream = "\n".join(f"- {p['repo']} (branch share/{data['key']}): report at {p['report']}\n{indent(p['summary'])}" for p in parents)
    notes = "\n".join(f"- {n}" for n in data.get("notes", []))
    return f"""# Brief: {s['repo']} for {data['key']}

Plan: {data['plan']}{chr(10) + 'Tasks: ' + data['tasks_file'] if data.get('tasks_file') else ''}
Read the whole plan for context; only the tasks below are yours.

Repo: {s['repo']}
Worktree: {info['worktree']}
Branch: {info['branch']} (cut from {info['base_ref']} @ {info['base_sha'][:12]}, fetched just now)

## Your tasks

{tasks}
{f'''
## Upstream sessions that finished before you

{upstream}
''' if parents else ''}{f'''
## Rules from the plan

{notes}
''' if notes else ''}{chr(10) + render_exercise(exercise) if exercise else ''}"""


def indent(text: str) -> str:
    return "\n".join("  " + line for line in text.strip().splitlines())


def report_status(path: Path):
    if not path.exists():
        return None
    m = re.search(r"^status:\s*(done|partial|blocked)\b", path.read_text(), re.M | re.I)
    return m.group(1).lower() if m else "partial"


def report_summary(path: Path) -> str:
    text = path.read_text()
    m = re.search(r"^## Summary\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return (m.group(1) if m else text)[:1500].strip()


# ---------------------------------------------------------------------------------------------
# exercise: Opus reads the plan against each repo's real code, read-only, before any is written

EXERCISE_SCHEMA = {
    "type": "object",
    "required": ["summary", "changes", "tests", "risks", "questions"],
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object", "required": ["file", "change"],
            "properties": {"file": {"type": "string"}, "change": {"type": "string"}}}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}

EXERCISE_PROMPT = """You are exercising an approved implementation plan against the real code, before anyone writes any. Do not edit anything: read only.

Repo: {repo}. You are in its worktree, {worktree}, cut from {base_ref} at {sha}: the exact code the implementing session will start from.

This repo's part of the plan:
{tasks}

Read the plan below, then the code it touches. Return:
- summary: two or three sentences: what this repo's change is, and whether the plan fits the code as it is.
- changes: each file to change and what changes in it, citing file:line for the code it touches.
- tests: the test cases to add or update, by file and name.
- risks: what could break, and where.
- questions: wherever the code disagrees with the plan, or the plan leaves a decision open.
Only what the plan asks for.

{files}"""

READONLY_BASH = [f"Bash({c}:*)" for c in (
    "git log", "git show", "git diff", "git grep", "git blame", "git ls-files", "git status", "git rev-parse",
    "ls", "cat", "head", "tail", "wc", "find", "rg", "grep", "jq", "tree", "graphify query", "graphify path",
    "graphify explain")]


def exercise_settings() -> dict:
    """Read tools and read-only shell. guard.py with nothing writable denies every write, even in the worktree."""
    guard = shlex.join([sys.executable, str(HERE / "guard.py"), "--protect", str(REPOS), "--protect", str(HOME)])
    return {"permissions": {"allow": ["Read", "Grep", "Glob", *READONLY_BASH]},
            "hooks": {"PreToolUse": [{"matcher": GUARDED_TOOLS, "hooks": [{"type": "command", "command": guard, "timeout": 10}]}]}}


def exercise_hash(data: dict, s: dict) -> str:
    """An edited plan, other tasks or another base needs a new exercise."""
    return hashlib.sha256(json.dumps([data.get("plan_hash"), s["repo"], s.get("base", ""), s["tasks"]],
                                     sort_keys=True).encode()).hexdigest()[:16]


def exercise_file(key: str, repo: str) -> Path:
    return HOME / key / "sessions" / repo / "exercise.json"


def load_exercise(data: dict, s: dict):
    """The kept exercise for this exact plan and these tasks, or None."""
    try:
        ex = json.loads(exercise_file(data["key"], s["repo"]).read_text())
    except (OSError, ValueError):
        return None
    return ex if ex.get("hash") == exercise_hash(data, s) and not ex.get("error") else None


def exercise_state(data: dict, s: dict) -> str:
    f = exercise_file(data["key"], s["repo"])
    if not f.exists():
        return "not exercised"
    try:
        ex = json.loads(f.read_text())
    except ValueError:
        return "not exercised"
    if ex.get("hash") != exercise_hash(data, s):
        return "exercise out of date: plan, tasks or base changed"
    if ex.get("error"):
        return "exercise failed"
    q = len(ex.get("questions", []))
    return f"exercised{f', {q} open question' + ('s' if q != 1 else '') if q else ''}"


def tasks_text(s: dict) -> str:
    return "\n\n".join(f"### {t['id'] + ' — ' if t.get('id') else ''}{t['title']}\n{t['detail']}" for t in s["tasks"])


def exercise_repo(data: dict, s: dict) -> dict:
    """One read-only `claude -p` in the repo's worktree. Always returns what it spent, even on failure."""
    key, repo = data["key"], s["repo"]
    sdir = HOME / key / "sessions" / repo
    sdir.mkdir(parents=True, exist_ok=True)
    out = {"repo": repo, "hash": exercise_hash(data, s), "model": EXERCISE_MODEL,
           "at": datetime.now().isoformat(timespec="seconds"), "cost_usd": 0.0}
    try:
        info = prepare_worktree(repo, key, s.get("base", ""))
        out.update(base_ref=info["base_ref"], base_sha=info["base_sha"])
        plan = Path(data["plan"])
        texts = plan_texts(plan) if plan.exists() else {}
        files = "\n\n".join(f'<{name} path="{plan.parent / (name + ".md")}">\n{text}\n</{name}>' for name, text in texts.items())
        prompt = EXERCISE_PROMPT.format(repo=repo, worktree=info["worktree"], base_ref=info["base_ref"],
                                        sha=info["base_sha"][:12], tasks=tasks_text(s), files=files)
        settings = sdir / "exercise-settings.json"
        settings.write_text(json.dumps(exercise_settings(), indent=2))
        cmd = [CLAUDE, "-p", "--model", EXERCISE_MODEL, "--output-format", "json", "--no-session-persistence",
               "--settings", str(settings), "--json-schema", json.dumps(EXERCISE_SCHEMA),
               *(["--max-budget-usd", EXERCISE_BUDGET] if EXERCISE_BUDGET else []),
               "--tools", "Read", "Grep", "Glob", "Bash", "--permission-mode", "default"]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=info["worktree"])
        out["cost_usd"] = cost_of(proc.stdout)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[-600:] or f"exit {proc.returncode}")
        outer = json.loads(proc.stdout)
        result = outer.get("structured_output")
        if not isinstance(result, dict):
            raise RuntimeError(f"no structured answer ({outer.get('subtype') or 'unknown'})")
        out.update(result)
    except Exception as e:  # one repo failing must not lose the others, nor what this one cost
        out["error"] = str(e)
    exercise_file(key, repo).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    add_cost(key, "exercise", out["cost_usd"], repo)
    return out


def run_exercise(key: str, only: list, force: bool):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    data = load(key)
    names = [s["repo"] for s in data["sessions"]]
    unknown = [r for r in only if r not in names]
    if unknown:
        sys.exit(f"No session for {', '.join(unknown)} in {key}. Sessions: {', '.join(names)}")
    todo = [s for s in data["sessions"] if (not only or s["repo"] in only) and (force or not load_exercise(data, s))]
    skipped = [s["repo"] for s in data["sessions"] if (not only or s["repo"] in only) and s not in todo]
    if skipped:
        print(f"Already exercised for this plan: {', '.join(skipped)} (--force to redo)")
    if not todo:
        return
    print(f"Exercising {len(todo)} repo(s) with {EXERCISE_MODEL}, {min(EXERCISE_PARALLEL, len(todo))} at a time. "
          "Read-only; this takes a few minutes per repo.\n", flush=True)
    total = 0.0
    with ThreadPoolExecutor(max_workers=max(1, EXERCISE_PARALLEL)) as pool:
        for fut in as_completed([pool.submit(exercise_repo, data, s) for s in todo]):
            ex = fut.result()
            total += ex["cost_usd"]
            if ex.get("error"):
                print(f"✗ {ex['repo']}  {usd(ex['cost_usd'])}  failed: {ex['error']}\n", flush=True)
                continue
            print(f"✓ {ex['repo']}  {usd(ex['cost_usd'])}  {len(ex['changes'])} change(s), {len(ex['tests'])} test(s), "
                  f"{len(ex['risks'])} risk(s)\n  {ex['summary']}", flush=True)
            for q in ex["questions"]:
                print(f"  ? {q}", flush=True)
            print(flush=True)
    print(f"Exercise cost: {usd(total)}. Details: {HOME / key / 'sessions' / '<repo>' / 'exercise.json'}")
    print(f"Questions above are worth settling in the plan before: {Path(sys.argv[0]).name} start {key}")


def render_exercise(ex: dict) -> str:
    lst = lambda items: "\n".join(f"- {x}" for x in items) if items else "- (none)"
    changes = "\n".join(f"- `{c['file']}`: {c['change']}" for c in ex.get("changes", [])) or "- (none)"
    return f"""## Exercise

{ex.get('model', 'Opus')} read the plan against this code (at {ex.get('base_sha', '')[:12]}) before you started. Follow it;
where the code does not match it, say so in Open questions rather than improvise.

{ex['summary']}

### Changes
{changes}

### Tests
{lst(ex.get('tests'))}

### Risks
{lst(ex.get('risks'))}

### Open questions
{lst(ex.get('questions'))}
"""


# ---------------------------------------------------------------------------------------------
# start / watch / status / stop


def load(key_or_plan: str) -> dict:
    p = Path(key_or_plan)
    key = plan_key(p.resolve()) if p.suffix == ".md" else key_or_plan
    f = HOME / key / "sessions.json"
    if not f.exists():
        sys.exit(f"No {f}. Run: {Path(sys.argv[0]).name} read <plan.md> first")
    return json.loads(f.read_text())


def session_name(key: str) -> str:
    return tmux_name(f"plan-{key}")


def run_start(key_or_plan: str, force: bool):
    data = load(key_or_plan)
    key, home = data["key"], HOME / data["key"]
    problems = validate(data["sessions"], local_repos())
    if problems:
        sys.exit("Cannot start:\n" + "\n".join(f"- {p}" for p in problems))
    plan = Path(data["plan"])
    if plan.exists() and texts_hash(plan_texts(plan)) != data["plan_hash"] and not force:
        sys.exit("The plan changed since it was read. Run `read` again, or `start --force` to run the old reading.")
    sess = session_name(key)
    if has_session(sess):
        sys.exit(f"Already running. Attach with: tmux attach -t {sess}")

    state_file = home / "state.json"
    if not state_file.exists():
        save_state(home, {"repos": {s["repo"]: {"status": "pending"} for s in data["sessions"]}})

    me = shlex.quote(str(Path(__file__).resolve()))
    orch_settings = home / "orchestrator-settings.json"
    (home / "sessions" / ORCH).mkdir(parents=True, exist_ok=True)
    orch_settings.write_text(json.dumps({"statusLine": cost_status_line(home / "sessions" / ORCH), "permissions": {"allow": [
        "Read", f"Bash({HERE / 'send-claude-message.sh'}:*)", f"Bash(python3 {Path(__file__).resolve()}:*)",
        "Bash(tmux capture-pane:*)", "Bash(tmux list-windows:*)", "Bash(mv:*)", "Bash(cat:*)", "Bash(ls:*)",
        "Bash(git -C:*)"]}}, indent=2))
    not_exercised = [s["repo"] for s in data["sessions"] if not load_exercise(data, s)]
    if not_exercised:
        print(f"Not exercised: {', '.join(not_exercised)}. Those sessions start from the plan alone "
              f"(run `exercise {key}` first to give them Opus's reading of their code).")
    rules = ORCH_RULES.format(
        key=key, plan=data["plan"], session=sess, home=home, send=HERE / "send-claude-message.sh",
        status=f"python3 {me} status {key}", watch=f"python3 {me} watch {key} (in the watcher window)")

    tmux("new-session", "-d", "-s", sess, "-n", "orchestrator", "-c", str(home), *env_flags(),
         shlex.join([CLAUDE, "--model", ORCH_MODEL, "--settings", str(orch_settings),
                     "--add-dir", str(plan.parent), str(HERE),
                     "--append-system-prompt", rules, "--name", f"orchestrator {key}",
                     f"You are the orchestrator for plan {key}. Read {data['plan']} and {home / 'sessions.json'}, "
                     "then tell me in a few lines which repo sessions will run and in what order. "
                     "The watcher is starting the first wave now; wait for its [watcher] messages."]))
    pin_name(sess, "orchestrator")
    new_window(sess, "watcher", home, [sys.executable, str(Path(__file__).resolve()), "watch", key])
    print(f"Started tmux session {sess}: orchestrator + watcher.\nAttach with: tmux attach -t {sess}")


def save_state(home: Path, state: dict):
    tmp = home / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(home / "state.json")


def launch_repo(data: dict, s: dict, state: dict, sess: str):
    key, home, repo = data["key"], HOME / data["key"], s["repo"]
    sdir = home / "sessions" / repo
    sdir.mkdir(parents=True, exist_ok=True)
    info = prepare_worktree(repo, key, s.get("base", ""))
    parents = [{"repo": d, "report": str(home / "sessions" / d / "report.md"),
                "summary": report_summary(home / "sessions" / d / "report.md")} for d in s.get("depends_on", [])]
    (sdir / "brief.md").write_text(brief_for(data, s, info, parents, load_exercise(data, s)))
    settings = sdir / "settings.json"
    settings.write_text(json.dumps(repo_settings(info["worktree"], sdir), indent=2))
    rules = SESSION_RULES.format(brief=sdir / "brief.md", worktree=info["worktree"], session_dir=sdir,
                                 branch=info["branch"], repos=REPOS)
    win = tmux_name(repo)
    new_window(sess, win, Path(info["worktree"]), [
        CLAUDE, "--model", s.get("model") or REPO_MODEL, "--permission-mode", "acceptEdits",
        "--settings", str(settings), "--add-dir", str(sdir), Path(data["plan"]).parent.as_posix(),
        "--append-system-prompt", rules, "--name", f"{key} {repo}",
        f"Read your brief at {sdir / 'brief.md'} and the plan it points to, then do your tasks. "
        f"Finish by writing {sdir / 'report.md'}.",
    ])
    state["repos"][repo].update(status="running", window=win, started_at=time.time(), **info)


def run_watch(key: str):
    data = load(key)
    home, sess = HOME / key, session_name(key)
    by_repo = {s["repo"]: s for s in data["sessions"]}
    state = json.loads((home / "state.json").read_text())
    for r in by_repo:
        state["repos"].setdefault(r, {"status": "pending"})
    seen = {}  # repo -> (pane hash, since)

    def notify(msg):
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)
        if "orchestrator" in windows(sess):
            send(sess, "orchestrator", f"[watcher] {msg}")

    print(f"Watching {key} every {POLL_SECONDS:g}s. Ctrl-C stops the watcher only.", flush=True)
    while True:
        changed = False
        for repo, st in state["repos"].items():
            sdir = home / "sessions" / repo
            report, question = sdir / "report.md", sdir / "question.md"
            if st["status"] == "pending":
                continue
            # Reports: a new or rewritten report.md moves the status, partial -> done included.
            if report.exists() and report.stat().st_mtime != st.get("report_mtime"):
                st["report_mtime"] = report.stat().st_mtime
                st["status"] = report_status(report)
                changed = True
                notify(f"{repo} reported {st['status']}: {report}")
            # Questions
            if question.exists() and question.stat().st_mtime != st.get("question_mtime"):
                st["question_mtime"] = question.stat().st_mtime
                changed = True
                notify(f"{repo} asks: {question}")
            # Approvals and idleness, read off the pane
            if st["status"] == "running" and st.get("window") in windows(sess):
                pane = capture(sess, st["window"])
                h = hashlib.md5(pane.encode()).hexdigest()
                waiting = re.search(r"Do you want to (proceed|make this edit|create)|❯\s*1\.\s*Yes", pane)
                if waiting and st.get("asked_hash") != h:
                    st["asked_hash"] = h
                    notify(f"{repo} is waiting for approval in window {sess}:{st['window']}")
                prev = seen.get(repo)
                if not prev or prev[0] != h:
                    seen[repo] = (h, time.time())
                    st.pop("idle_noted", None)
                elif not waiting and time.time() - prev[1] > IDLE_MINUTES * 60 and not st.get("idle_noted"):
                    st["idle_noted"] = True
                    notify(f"{repo} looks idle for {IDLE_MINUTES:g} min with no report (window {sess}:{st['window']})")
            if st["status"] == "running" and (st.get("window") not in windows(sess) or pane_dead(sess, st["window"])):
                st["status"] = "partial"
                changed = True
                notify(f"{repo}: its Claude exited (or its window is gone) without a report. "
                       f"Restart it in window {sess}:{st.get('window')} with `claude --continue`, then ask it to finish")

        for repo in ready_to_start(data["sessions"], state):
            try:
                launch_repo(data, by_repo[repo], state, sess)
                notify(f"{repo} started in window {sess}:{state['repos'][repo]['window']}"
                       + (f" after {', '.join(by_repo[repo]['depends_on'])}" if by_repo[repo].get("depends_on") else ""))
            except Exception as e:  # a failed fetch or worktree should not kill the watcher
                state["repos"][repo]["status"] = "blocked"
                state["repos"][repo]["error"] = str(e)
                notify(f"{repo} could not start: {e}")
            changed = True

        if changed:
            save_state(home, state)
        statuses = {r: st["status"] for r, st in state["repos"].items()}
        if all(v == "done" for v in statuses.values()):
            notify("all repo sessions reported done. " + ", ".join(statuses))
            return
        stuck = [r for r, v in statuses.items() if v == "pending"]
        if stuck and not any(v == "running" for v in statuses.values()) and not ready_to_start(data["sessions"], state):
            if not state.get("stuck_noted"):
                state["stuck_noted"] = True
                save_state(home, state)
                notify(f"nothing is running and {', '.join(stuck)} wait on sessions that did not finish done. "
                       "Resume those (send them a message) and the watcher picks their new report up.")
        else:
            state.pop("stuck_noted", None)
        time.sleep(POLL_SECONDS)


def run_status(key: str):
    data = load(key)
    f = HOME / key / "state.json"
    state = json.loads(f.read_text()) if f.exists() else {"repos": {}}
    sess = session_name(key)
    costs = read_costs(key)
    print(f"{key}  tmux: {sess} ({'running' if has_session(sess) else 'not running'})")
    print(f"  {'':7} {'repo':<28} {'model':<8} {'status':<11} {'exercise':>9} {'session':>9}")
    sessions_total = 0.0
    for wave_n, wave in enumerate(waves(data["sessions"]), 1):
        for r in wave:
            s = next(x for x in data["sessions"] if x["repo"] == r)
            st = state["repos"].get(r, {"status": "not started"})
            deps = s.get("depends_on", [])
            extra = f"  after {', '.join(deps)}" if deps else ""
            q = HOME / key / "sessions" / r / "question.md"
            spent = session_cost(key, r)
            sessions_total += spent
            print(f"  wave {wave_n:<2} {r:<28} {(s.get('model') or REPO_MODEL):<8} {st['status']:<11} "
                  f"{usd(costs['exercise'].get(r, 0.0)):>9} {usd(spent):>9}{extra}"
                  f"{'  QUESTION OPEN' if q.exists() else ''}{'  ' + st['error'] if st.get('error') else ''}")
    orch = session_cost(key, ORCH)
    exercise_total = sum(costs["exercise"].values())
    total = costs["read"] + exercise_total + sessions_total + orch
    print(f"\n  read {usd(costs['read'])} · exercise {usd(exercise_total)} · repo sessions {usd(sessions_total)}"
          f" · orchestrator {usd(orch)} · total {usd(total)}")
    print("  (Claude Code's API-priced total_cost_usd; on a Pro/Max/Team plan an estimate, not your bill)")


def run_stop(key: str):
    sess = session_name(key)
    if has_session(sess):
        tmux("kill-session", "-t", f"={sess}")
        print(f"Killed {sess}. Worktrees, briefs and reports stay under {HOME / key}.")
    else:
        print(f"{sess} is not running.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read").add_argument("plan")
    p = sub.add_parser("start")
    p.add_argument("plan")
    p.add_argument("--force", action="store_true", help="run a reading of a plan that has since changed")
    for name in ("status", "watch", "stop"):
        sub.add_parser(name).add_argument("key")
    sub.add_parser("preview").add_argument("key")
    p = sub.add_parser("model", help="pick the model a repo session runs on")
    p.add_argument("key")
    p.add_argument("repo", help="a repo in the plan, or all")
    p.add_argument("model", help="opus, sonnet, haiku, or a full claude-... id")
    p = sub.add_parser("exercise", help="Opus reads the plan against each repo's code, read-only")
    p.add_argument("key")
    p.add_argument("repos", nargs="*", help="only these repos (default: every session)")
    p.add_argument("--force", action="store_true", help="redo repos already exercised for this plan")
    a = ap.parse_args()
    if a.cmd == "read":
        run_read(Path(a.plan))
    elif a.cmd == "model":
        run_model(a.key, a.repo, a.model)
    elif a.cmd == "exercise":
        run_exercise(a.key, a.repos, a.force)
    elif a.cmd == "start":
        run_start(a.plan, a.force)
    elif a.cmd == "preview":
        print_preview(load(a.key))
    elif a.cmd == "status":
        run_status(a.key)
    elif a.cmd == "watch":
        run_watch(a.key)
    elif a.cmd == "stop":
        run_stop(a.key)


if __name__ == "__main__":
    main()
