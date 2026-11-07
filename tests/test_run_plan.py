"""
python3 -m unittest discover -s tests -v

The end-to-end test runs real tmux (on its own socket), real git worktrees and a fake `claude`
that commits and writes report.md, so no model is called.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import run_plan as rp  # noqa: E402


def S(repo, deps=(), tasks=None):
    return {"repo": repo, "depends_on": list(deps), "tasks": tasks or [{"id": "T1", "title": f"do {repo}", "detail": "x"}]}


class PlanLogic(unittest.TestCase):
    def test_plan_key(self):
        self.assertEqual(rp.plan_key(Path("/x/PROJ-9999-demo-plan.md")), "PROJ-9999")
        self.assertEqual(rp.plan_key(Path("/x/tasks/ABC-123-login-rule/plan.md")), "ABC-123")
        self.assertEqual(rp.plan_key(Path("/x/plans/plan.md")), "plan")

    def test_waves_order_by_dependency(self):
        sessions = [S("fe", ["be"]), S("be", ["lib"]), S("lib"), S("php")]
        self.assertEqual(rp.waves(sessions), [["lib", "php"], ["be"], ["fe"]])

    def test_cycle_and_unknown_repos_are_refused(self):
        self.assertTrue(any("cycle" in p for p in rp.validate([S("a", ["b"]), S("b", ["a"])], ["a", "b"])))
        problems = rp.validate([S("a", ["ghost"]), S("nope")], ["a"])
        self.assertTrue(any("ghost" in p for p in problems))
        self.assertTrue(any("nope: not a git checkout" in p for p in problems))
        self.assertTrue(any("more than one" in p for p in rp.validate([S("a"), S("a")], ["a"])))

    def test_ready_needs_every_parent_done(self):
        sessions = [S("lib"), S("be", ["lib"]), S("fe", ["be", "lib"])]
        state = {"repos": {"lib": {"status": "done"}, "be": {"status": "pending"}, "fe": {"status": "pending"}}}
        self.assertEqual(rp.ready_to_start(sessions, state), ["be"])
        state["repos"]["lib"]["status"] = "partial"
        self.assertEqual(rp.ready_to_start(sessions, state), [])

    def test_mentioned_repos_for_the_cross_check(self):
        texts = {"tasks": "## T2-engine\n- **Repo:** promo-api (`scripts/`)\n## T3\n- **Repo:** web-fe",
                 "plan": "### 1. Wire `order-api`\nnot a heading: `monolith`"}
        repos = ["promo-api", "web-fe", "monolith", "order-api"]
        self.assertEqual(rp.mentioned_repos(texts, repos), {"promo-api", "web-fe", "order-api"})

    def test_report_status_and_summary(self):
        d = Path(tempfile.mkdtemp())
        r = d / "report.md"
        self.assertIsNone(rp.report_status(r))
        r.write_text("---\nstatus: done\n---\n## Summary\nAdded the endpoint.\n## Commits\n- abc x\n")
        self.assertEqual(rp.report_status(r), "done")
        self.assertEqual(rp.report_summary(r), "Added the endpoint.")
        r.write_text("no front matter")
        self.assertEqual(rp.report_status(r), "partial")

    def test_exercise_goes_stale_when_plan_tasks_or_base_change(self):
        data, s = {"key": "K", "plan_hash": "a"}, S("api")
        h = rp.exercise_hash(data, s)
        self.assertNotEqual(h, rp.exercise_hash({**data, "plan_hash": "b"}, s))
        self.assertNotEqual(h, rp.exercise_hash(data, {**s, "base": "feat/x"}))
        self.assertNotEqual(h, rp.exercise_hash(data, S("api", tasks=[{"id": "T9", "title": "other", "detail": ""}])))
        self.assertEqual(h, rp.exercise_hash(data, {**s, "model": "opus"}))  # the model is not part of the plan

    def test_a_failed_exercise_still_counts_what_it_cost_and_is_not_used(self):
        home = Path(tempfile.mkdtemp())
        old = rp.HOME, rp.CLAUDE
        fake = home / "claude"
        fake.write_text("#!/bin/sh\necho '{\"subtype\":\"error_max_budget_usd\",\"total_cost_usd\":0.4}'\nexit 1\n")
        fake.chmod(0o755)
        try:
            rp.HOME, rp.CLAUDE = home, str(fake)
            rp.prepare_worktree = lambda repo, key, base="": {"worktree": str(home), "base_ref": "origin/main", "base_sha": "0" * 40}
            data = {"key": "K", "plan_hash": "a", "plan": str(home / "plan.md")}
            ex = rp.exercise_repo(data, S("api"))
            self.assertIn("error", ex)
            self.assertEqual(rp.read_costs("K")["exercise"], {"api": 0.4})
            self.assertIsNone(rp.load_exercise(data, S("api")))
            self.assertEqual(rp.exercise_state(data, S("api")), "exercise failed")
        finally:
            rp.HOME, rp.CLAUDE = old
            import importlib
            importlib.reload(rp)

    def test_status_line_survives_bad_input_and_sums_processes(self):
        d = Path(tempfile.mkdtemp())
        run = lambda payload: subprocess.run([sys.executable, str(HERE / "statusline.py"), str(d)], input=payload,
                                             capture_output=True, text=True)
        r = run("not json")
        self.assertEqual(r.returncode, 0)
        self.assertIn("$0.00", r.stdout)
        run(json.dumps({"session_id": "a", "model": {"display_name": "Opus 5.5"}, "cost": {"total_cost_usd": 1.5}}))
        r = run(json.dumps({"session_id": "b", "model": {"display_name": "Opus 5.5"}, "cost": {"total_cost_usd": 0.25}}))
        self.assertEqual(r.stdout.strip(), "Opus 5.5 · $1.75 (this run $0.25)")

    def test_graph_is_built_outside_the_worktree_reused_and_rebuilt_after_a_commit(self):
        root = Path(tempfile.mkdtemp())
        wt = root / "wt"
        subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
        g = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "a"], check=True)
        calls = root / "calls"
        fake = root / "graphify"
        fake.write_text(FAKE_GRAPHIFY)
        fake.chmod(0o755)
        old = rp.HOME, rp.GRAPHIFY, rp.GRAPH_ON
        os.environ["FAKE_GRAPHIFY_CALLS"] = str(calls)
        try:
            rp.HOME, rp.GRAPHIFY, rp.GRAPH_ON = root / "home", str(fake), True
            first = rp.ensure_graph("K", "api", str(wt))
            self.assertEqual(first["nodes"], 3)
            self.assertTrue(first["path"].startswith(str(root / "home" / "K" / "graphs" / "api")))
            self.assertFalse((wt / "graphify-out").exists())
            self.assertEqual(rp.ensure_graph("K", "api", str(wt))["sha"], first["sha"])
            self.assertEqual(calls.read_text().count("extract"), 1)          # same commit: reused
            subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "b"], check=True)
            self.assertNotEqual(rp.ensure_graph("K", "api", str(wt))["sha"], first["sha"])
            self.assertEqual(calls.read_text().count("extract"), 2)          # new commit: rebuilt
            self.assertIn("graph 3 nodes @", rp.graph_label("K", "api"))
            guide = rp.graph_guide(rp.ensure_graph("K", "api", str(wt)))
            self.assertIn("graphify explain", guide)
            self.assertIn("--graph", guide)

            os.environ["FAKE_GRAPHIFY_FAIL"] = "1"
            subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "c"], check=True)
            failed = rp.ensure_graph("K", "api", str(wt))
            self.assertIn("error", failed)
            self.assertNotIn("path", failed)
            self.assertEqual(rp.graph_guide(failed), "")
            self.assertTrue(rp.graph_label("K", "api").startswith("no graph (boom"))

            rp.GRAPHIFY = ""
            self.assertEqual(rp.ensure_graph("K", "web", str(wt))["error"], "graphify is not installed")
            rp.GRAPHIFY, rp.GRAPH_ON = str(fake), False
            self.assertIn("disabled", rp.ensure_graph("K", "web", str(wt))["error"])
        finally:
            rp.HOME, rp.GRAPHIFY, rp.GRAPH_ON = old
            os.environ.pop("FAKE_GRAPHIFY_FAIL", None)
            os.environ.pop("FAKE_GRAPHIFY_CALLS", None)

    def test_parse_reading_structured_and_fallback(self):
        body = {"sessions": [], "unassigned": [], "notes": []}
        self.assertEqual(rp.parse_reading(json.dumps({"structured_output": body})), body)
        self.assertEqual(rp.parse_reading(json.dumps({"result": "here:\n" + json.dumps(body)})), body)


FAKE_GRAPHIFY = r"""#!/usr/bin/env python3
import json, os, sys
calls = os.environ.get("FAKE_GRAPHIFY_CALLS")
if calls:
    open(calls, "a").write(" ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_GRAPHIFY_FAIL"):
    print("boom: extractor crashed", file=sys.stderr)
    sys.exit(2)
assert sys.argv[1] == "extract" and "--code-only" in sys.argv
out = os.path.join(sys.argv[sys.argv.index("--out") + 1], "graphify-out")
os.makedirs(out, exist_ok=True)
json.dump({"nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "links": [{"source": "a", "target": "b"}]},
          open(os.path.join(out, "graph.json"), "w"))
"""


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys, time
args = sys.argv[1:]
if "-p" in args:
    prompt = sys.stdin.read()
    if "--no-session-persistence" in args:   # an exercise: answer about this worktree, at 0.25
        repo = os.path.basename(os.getcwd())
        assert "You are exercising" in prompt and os.path.exists(".git"), "exercise must run in the worktree"
        assert "## Code graph (graphify)" in prompt and "graphs/" + repo in prompt, "exercise must get its graph"
        assert not os.path.exists("graphify-out"), "the graph must not land in the worktree"
        settings = json.load(open(args[args.index("--settings") + 1]))
        assert "Edit" not in settings["permissions"]["allow"] and "--writable" not in json.dumps(settings)
        body = {"summary": f"{repo} fits the plan", "changes": [{"file": f"{repo}.go:1", "change": "add X"}],
                "tests": [f"{repo}_test.go: TestX"], "risks": ["none"], "questions": [f"which X for {repo}?"]}
        print(json.dumps({"structured_output": body, "total_cost_usd": 0.25}))
    else:                                    # the reading, at 0.10
        print(json.dumps({"structured_output": json.loads(os.environ["FAKE_READING"]), "total_cost_usd": 0.10}))
    sys.exit(0)

def status_line(usd, sid):
    """What Claude Code does on each refresh: run the settings' statusLine with the session's cost."""
    settings = json.load(open(args[args.index("--settings") + 1]))
    cmd = settings["statusLine"]["command"]
    subprocess.run(cmd, shell=True, input=json.dumps({"session_id": sid, "model": {"display_name": "Fake"},
                   "cost": {"total_cost_usd": usd}}), text=True, capture_output=True, check=True)

prompt = args[-1]
m = re.search(r"Finish by writing (\S+report\.md)", prompt)
if not m:                     # the orchestrator: just stay up and echo what it is sent
    status_line(0.05, "orch")
    while True:
        line = sys.stdin.readline()
        if line:
            open(os.environ["TMUX_ORCH_FAKE_ORCH_LOG"], "a").write(line)
        else:
            time.sleep(0.2)
report = m.group(1)
repo = os.path.basename(os.getcwd())
model = args[args.index("--model") + 1]
status_line(0.30, "run1")
status_line(0.40, "run1")       # a later refresh of the same process replaces its number
status_line(0.20, "run2")       # a second process (claude --continue) adds to it
open("work.txt", "w").write(f"{repo} {time.time()}\n")
subprocess.run(["git", "add", "work.txt"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"work in {repo}"], check=True)
brief = open(os.path.join(os.path.dirname(report), "brief.md")).read()
time.sleep(1)
open(report, "w").write(f"---\nstatus: done\n---\n## Summary\n{repo} finished. upstream-seen={'Upstream sessions' in brief} model={model} exercise-seen={'## Exercise' in brief} graph-seen={'## Code graph' in brief}\n")
time.sleep(600)
'''


@unittest.skipUnless(shutil.which("tmux") and shutil.which("git"), "needs tmux and git")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tmux-orch-"))
        self.repos, self.home = self.root / "repos", self.root / "home"
        self.repos.mkdir()
        for name in ("lib", "api", "web"):
            origin = self.root / "origins" / f"{name}.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
            seed = self.root / "seed" / name
            subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
            (seed / "README").write_text(name)
            g = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(g + ["add", "."], check=True)
            subprocess.run(g + ["commit", "-qm", "init"], check=True)
            subprocess.run(g + ["push", "-q", str(origin), "main"], check=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(self.repos / name)], check=True)
        fake = self.root / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        fake_graphify = self.root / "graphify"
        fake_graphify.write_text(FAKE_GRAPHIFY)
        fake_graphify.chmod(0o755)
        self.plan = self.root / "tasks" / "DEMO-1-x" / "plan.md"
        self.plan.parent.mkdir(parents=True)
        self.plan.write_text("## Implementation steps\n### 1. `lib`\n### 2. `api`\ndepends on step 1\n### 3. `web`\n")
        reading = {"sessions": [S("lib"), S("api", ["lib"]), S("web", ["api"])],
                   "unassigned": [{"title": "deploy", "why": "ops"}], "notes": ["keep it small"]}
        self.socket = f"tmux-orch-test-{os.getpid()}"
        self.env = {**os.environ, "TMUX_ORCH_REPOS": str(self.repos), "TMUX_ORCH_HOME": str(self.home),
                    "TMUX_ORCH_CLAUDE": str(fake), "TMUX_ORCH_GRAPHIFY": str(fake_graphify), "TMUX_ORCH_POLL": "0.5", "TMUX_ORCH_SOCKET": self.socket,
                    "FAKE_READING": json.dumps(reading), "FAKE_ORCH_LOG": str(self.root / "orch.log")}
        self.env["TMUX_ORCH_FAKE_READING"] = self.env["FAKE_READING"]  # reaches tmux windows
        self.env["TMUX_ORCH_FAKE_ORCH_LOG"] = self.env["FAKE_ORCH_LOG"]

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "run_plan.py"), *args], env=self.env,
                              capture_output=True, text=True)

    def test_read_start_and_dependency_order(self):
        r = self.cli("read", str(self.plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Wave 1", r.stdout)
        self.assertIn("deploy", r.stdout)
        self.assertIn("(not exercised; no graph yet)", r.stdout)

        # Pick a model: unknown names are refused, a repo gets its own, re-reading keeps it.
        self.assertNotEqual(self.cli("model", "DEMO-1", "api", "gpt-9").returncode, 0)
        self.assertNotEqual(self.cli("model", "DEMO-1", "ghost", "opus").returncode, 0)
        r = self.cli("model", "DEMO-1", "api", "opus")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api  [opus]", r.stdout)
        self.assertEqual(self.cli("read", str(self.plan)).returncode, 0)
        sessions = json.loads((self.home / "DEMO-1" / "sessions.json").read_text())["sessions"]
        self.assertEqual({s["repo"]: s["model"] for s in sessions}, {"lib": "sonnet", "api": "opus", "web": "sonnet"})

        # Exercise every repo in its worktree; a second run skips them; editing the plan makes them stale.
        r = self.cli("exercise", "DEMO-1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("✓ api  $0.25", r.stdout)
        self.assertIn("? which X for web?", r.stdout)
        self.assertIn("Exercise cost: $0.75", r.stdout)
        self.assertIn("Already exercised", self.cli("exercise", "DEMO-1").stdout)
        ex = json.loads((self.home / "DEMO-1" / "sessions" / "lib" / "exercise.json").read_text())
        self.assertEqual((ex["summary"], ex["model"], len(ex["base_sha"])), ("lib fits the plan", "opus", 40))
        self.assertIn("(exercised, 1 open question; graph 3 nodes @", self.cli("preview", "DEMO-1").stdout)

        r = self.cli("start", "DEMO-1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

        state_file = self.home / "DEMO-1" / "state.json"
        deadline = time.time() + 60
        while time.time() < deadline:
            st = json.loads(state_file.read_text())["repos"] if state_file.exists() else {}
            if st and all(v["status"] == "done" for v in st.values()):
                break
            time.sleep(0.5)
        self.assertTrue(all(v["status"] == "done" for v in st.values()), st)

        # Ordered: lib, then api, then web, each only after its parent reported.
        started = sorted(st, key=lambda r: st[r]["started_at"])
        self.assertEqual(started, ["lib", "api", "web"])
        rep = lambda r: (self.home / "DEMO-1" / "sessions" / r / "report.md")
        self.assertLessEqual(rep("lib").stat().st_mtime, st["api"]["started_at"])
        self.assertLessEqual(rep("api").stat().st_mtime, st["web"]["started_at"])
        self.assertIn("upstream-seen=False", rep("lib").read_text())
        self.assertIn("upstream-seen=True", rep("web").read_text())
        self.assertIn("model=opus", rep("api").read_text())
        self.assertIn("model=sonnet", rep("web").read_text())
        self.assertIn("exercise-seen=True", rep("lib").read_text())
        self.assertIn("graph-seen=True", rep("web").read_text())

        # Each worked in its own worktree on share/DEMO-1; the main checkout is untouched.
        for r in ("lib", "api", "web"):
            wt = self.home / "DEMO-1" / "worktrees" / r
            branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
            self.assertEqual(branch, "share/DEMO-1")
            self.assertFalse((self.repos / r / "work.txt").exists())
            self.assertEqual(subprocess.run(["git", "-C", str(self.repos / r), "status", "--porcelain"],
                                            capture_output=True, text=True).stdout, "")

        brief = (self.home / "DEMO-1" / "sessions" / "web" / "brief.md").read_text()
        self.assertIn("api (branch share/DEMO-1)", brief)
        self.assertIn("keep it small", brief)
        self.assertIn("## Exercise", brief)
        self.assertIn("`web.go:1`: add X", brief)

        # The orchestrator heard about it.
        time.sleep(1)
        log = (self.root / "orch.log").read_text()
        self.assertIn("[watcher] lib started", log)
        self.assertIn("[watcher] web reported done", log)
        self.assertIn("all repo sessions reported done", log)

        # A plan may start a repo from an unmerged upstream branch instead of the default one.
        seed = self.root / "seed" / "lib"
        g = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["checkout", "-qb", "feat/upstream"], check=True)
        (seed / "UP").write_text("up")
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "upstream"], check=True)
        subprocess.run(g + ["push", "-q", str(self.root / "origins" / "lib.git"), "feat/upstream"], check=True)
        os.environ.update(TMUX_ORCH_REPOS=str(self.repos), TMUX_ORCH_HOME=str(self.home))
        import importlib
        mod = importlib.reload(rp)
        info = mod.prepare_worktree("lib", "DEMO-2", "feat/upstream")
        self.assertEqual(info["base_ref"], "origin/feat/upstream")
        self.assertTrue((Path(info["worktree"]) / "UP").exists())
        with self.assertRaisesRegex(RuntimeError, "does not exist on origin"):
            mod.prepare_worktree("api", "DEMO-2", "gone/branch")

        r = self.cli("status", "DEMO-1")
        self.assertIn("web", r.stdout)
        self.assertIn("done", r.stdout)
        # read 0.20 (read twice) + exercise 3 x 0.25 + sessions 3 x (0.40 + 0.20) + orchestrator 0.05
        self.assertIn("read $0.20 · exercise $0.75 · repo sessions $1.80 · orchestrator $0.05 · total $2.80", r.stdout)
        self.assertRegex(r.stdout, r"api\s+opus\s+done\s+\$0\.25\s+\$0\.60\s+3 nodes")


if __name__ == "__main__":
    unittest.main()
