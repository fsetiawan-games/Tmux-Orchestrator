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

    def test_parse_reading_structured_and_fallback(self):
        body = {"sessions": [], "unassigned": [], "notes": []}
        self.assertEqual(rp.parse_reading(json.dumps({"structured_output": body})), body)
        self.assertEqual(rp.parse_reading(json.dumps({"result": "here:\n" + json.dumps(body)})), body)


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys, time
args = sys.argv[1:]
if "-p" in args:
    sys.stdin.read()
    print(json.dumps({"structured_output": json.loads(os.environ["FAKE_READING"])}))
    sys.exit(0)
prompt = args[-1]
m = re.search(r"Finish by writing (\S+report\.md)", prompt)
if not m:                     # the orchestrator: just stay up and echo what it is sent
    while True:
        line = sys.stdin.readline()
        if line:
            open(os.environ["TMUX_ORCH_FAKE_ORCH_LOG"], "a").write(line)
        else:
            time.sleep(0.2)
report = m.group(1)
repo = os.path.basename(os.getcwd())
open("work.txt", "w").write(f"{repo} {time.time()}\n")
subprocess.run(["git", "add", "work.txt"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"work in {repo}"], check=True)
brief = open(os.path.join(os.path.dirname(report), "brief.md")).read()
time.sleep(1)
open(report, "w").write(f"---\nstatus: done\n---\n## Summary\n{repo} finished. upstream-seen={'Upstream sessions' in brief}\n")
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
        self.plan = self.root / "tasks" / "DEMO-1-x" / "plan.md"
        self.plan.parent.mkdir(parents=True)
        self.plan.write_text("## Implementation steps\n### 1. `lib`\n### 2. `api`\ndepends on step 1\n### 3. `web`\n")
        reading = {"sessions": [S("lib"), S("api", ["lib"]), S("web", ["api"])],
                   "unassigned": [{"title": "deploy", "why": "ops"}], "notes": ["keep it small"]}
        self.socket = f"tmux-orch-test-{os.getpid()}"
        self.env = {**os.environ, "TMUX_ORCH_REPOS": str(self.repos), "TMUX_ORCH_HOME": str(self.home),
                    "TMUX_ORCH_CLAUDE": str(fake), "TMUX_ORCH_POLL": "0.5", "TMUX_ORCH_SOCKET": self.socket,
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


if __name__ == "__main__":
    unittest.main()
