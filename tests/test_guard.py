"""python3 -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import guard as g  # noqa: E402

R = "/r"                                   # the repos folder (main checkouts)
WT = "/r/.tmux-orch/K/worktrees/api"       # this session's worktree
SD = "/r/.tmux-orch/K/sessions/api"        # this session's folder
W, P = [WT, SD], [R, "/r/.tmux-orch"]


def bash(cmd, cwd=WT):
    v = g.decide("Bash", {"command": cmd}, cwd, W, P)
    return v and v[0]


def write(path, tool="Write", cwd=WT):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    v = g.decide(tool, {key: path}, cwd, W, P)
    return v and v[0]


class WriteTools(unittest.TestCase):
    def test_inside_the_worktree_and_session_folder_is_left_alone(self):
        self.assertIsNone(write(f"{WT}/internal/x.go"))
        self.assertIsNone(write("internal/x.go"))               # relative to cwd
        self.assertIsNone(write(f"{SD}/report.md"))
        self.assertIsNone(write(f"{WT}/n.ipynb", "NotebookEdit"))

    def test_outside_is_denied(self):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            self.assertEqual(write("/r/go-lib/x.go", tool), "deny", tool)
        self.assertEqual(write("/r/.tmux-orch/K/worktrees/web/x"), "deny")   # a sibling session
        self.assertEqual(write("/r/.tmux-orch/K/worktrees/api-old/x"), "deny")  # prefix is not inside
        self.assertEqual(write("../../sessions/web/report.md"), "deny")
        self.assertEqual(write("/tmp/scratch.txt"), "deny")
        self.assertEqual(write("~/.zshrc"), "deny")

    def test_a_symlink_out_of_the_worktree_is_outside(self):
        root = Path(tempfile.mkdtemp())
        wt, outside = root / "wt", root / "main"
        wt.mkdir(), outside.mkdir()
        (wt / "escape").symlink_to(outside)
        v = g.decide("Write", {"file_path": str(wt / "escape" / "x.go")}, str(wt), [str(wt)], [str(root)])
        self.assertEqual(v[0], "deny")
        self.assertIsNone(g.decide("Write", {"file_path": str(wt / "ok.go")}, str(wt), [str(wt)], [str(root)]))

    def test_read_tools_are_not_judged(self):
        self.assertIsNone(g.decide("Read", {"file_path": "/r/go-lib/x.go"}, WT, W, P))
        self.assertIsNone(g.decide("Grep", {"path": "/r"}, WT, W, P))


class BashWrites(unittest.TestCase):
    def test_ordinary_work_in_the_worktree_passes(self):
        for cmd in ["go test ./... 2>&1 | tail -20", "git add -A && git commit -m 'x'", "make build > /dev/null",
                    "npm run lint", "git status", "rm -rf ./tmp", "sed -i '' s/a/b/ x.go", "echo x > out.txt",
                    f"echo done > {SD}/report.md", "mkdir -p internal/foo"]:
            self.assertIsNone(bash(cmd), cmd)

    def test_reading_a_main_checkout_passes(self):
        for cmd in ["cat /r/go-lib/x.go", "ls /r/go-lib", "git -C /r/go-lib log --oneline -5",
                    "cat /r/go-lib/x.go > notes.txt", "cp /r/go-lib/x.go ./vendor/x.go", "grep -rn Foo /r/go-lib | head"]:
            self.assertIsNone(bash(cmd), cmd)

    def test_writing_a_main_checkout_or_a_sibling_is_denied(self):
        for cmd in ["echo hi > /r/go-lib/x", "echo hi >> '/r/go-lib/x'", "cd /r/go-lib && git commit -am x",
                    "cd /r/go-lib; rm -rf vendor", "git -C /r/go-lib checkout main", "git -C /r/go-lib pull",
                    "rm -rf /r/.tmux-orch/K/worktrees/web/src", "cp x.go /r/go-lib/x.go", "touch ../web/x",
                    "sed -i '' s/a/b/ /r/go-lib/x.go", "tee /r/go-lib/x < in", "mv x /r/go-lib/",
                    "rm -f /r/monolith/config.php"]:
            self.assertEqual(bash(cmd), "deny", cmd)

    def test_a_session_whose_cwd_drifted_into_a_main_checkout_cannot_write(self):
        self.assertEqual(bash("git commit -am x", cwd="/r/go-lib"), "deny")
        self.assertEqual(bash("rm -rf vendor", cwd="/r/go-lib"), "deny")
        self.assertIsNone(bash("git log -3", cwd="/r/go-lib"))


class BashGit(unittest.TestCase):
    def test_rebase_and_force_push_are_denied_in_any_spelling(self):
        for cmd in ["git rebase main", "git pull --rebase", "git -c x=y rebase -i HEAD~3", "git push --force",
                    "git push -f origin share/K", "git push -uf origin x", "git -C . push --force-with-lease",
                    "git push origin +share/K", "cd x && git push --force"]:
            self.assertEqual(bash(cmd), "deny", cmd)

    def test_every_push_and_pr_change_asks(self):
        for cmd in ["git push", "git push -u origin share/K", "git -C . push origin share/K",
                    "env GIT_TRACE=1 git push", "make test && git push", "gh pr create --fill",
                    "gh pr merge 12 --squash", "sh -c 'git push'", "gh pr comment 3 -b hi"]:
            self.assertEqual(bash(cmd), "ask", cmd)
        for cmd in ["gh pr view 12", "gh pr list", "git fetch origin", "git log origin/main..HEAD"]:
            self.assertIsNone(bash(cmd), cmd)

    def test_commands_that_reach_past_the_machine_ask(self):
        for cmd in ["kubectl apply -f x.yaml", "env KUBECONFIG=x kubectl delete pod y", "terraform apply",
                    "gcloud sql connect x", "mysql -h db -e 'select 1'", "aws s3 ls", "npm publish",
                    "curl -X POST https://x", "curl -d a=b https://x", "gh api repos/x -X PATCH",
                    "helm upgrade x y", "go test && redis-cli FLUSHALL"]:
            self.assertEqual(bash(cmd), "ask", cmd)
        for cmd in ["kubectl get pods", "curl https://example.com", "helm list", "gh api repos/x"]:
            self.assertIsNone(bash(cmd), cmd)


class HookProtocol(unittest.TestCase):
    def run_hook(self, event):
        return subprocess.run([sys.executable, str(HERE / "guard.py"), "--writable", WT, "--writable", SD,
                               "--protect", R], input=json.dumps(event), capture_output=True, text=True)

    def test_deny_and_ask_come_back_as_hook_json(self):
        r = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": "/r/go-lib/x"}, "cwd": WT,
                           "hook_event_name": "PreToolUse"})
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("/r/go-lib/x", out["permissionDecisionReason"])
        r = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git -C . push"}, "cwd": WT})
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_an_allowed_call_prints_nothing_so_permission_rules_decide(self):
        r = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "go test ./..."}, "cwd": WT})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = subprocess.run([sys.executable, str(HERE / "guard.py")], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class Wiring(unittest.TestCase):
    def test_repo_sessions_get_the_guard_for_their_own_dirs(self):
        os.environ.update(TMUX_ORCH_REPOS="/r", TMUX_ORCH_HOME="/r/.tmux-orch")
        import importlib
        import run_plan
        rp = importlib.reload(run_plan)
        s = rp.repo_settings(WT, SD)
        hook = s["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Bash|Edit|Write|MultiEdit|NotebookEdit")
        cmd = hook["hooks"][0]["command"]
        for part in ("guard.py", f"--writable {WT}", f"--writable {SD}", "--protect /r", "--protect /r/.tmux-orch"):
            self.assertIn(part, cmd)
        self.assertIn("Bash(git push:*)", s["permissions"]["ask"])


if __name__ == "__main__":
    unittest.main()
