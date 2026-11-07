![Orchestrator Hero](/Orchestrator.png)

**Run AI agents 24/7 while you sleep** - The Tmux Orchestrator enables Claude agents to work autonomously, schedule their own check-ins, and coordinate across multiple projects without human intervention.

## 🤖 Key Capabilities & Autonomous Features

- **Self-trigger** - Agents schedule their own check-ins and continue work autonomously
- **Coordinate** - Project managers assign tasks to engineers across multiple codebases  
- **Persist** - Work continues even when you close your laptop
- **Scale** - Run multiple teams working on different projects simultaneously

## 🏗️ Architecture

The Tmux Orchestrator uses a three-tier hierarchy to overcome context window limitations:

```
┌─────────────┐
│ Orchestrator│ ← You interact here
└──────┬──────┘
       │ Monitors & coordinates
       ▼
┌─────────────┐     ┌─────────────┐
│  Project    │     │  Project    │
│  Manager 1  │     │  Manager 2  │ ← Assign tasks, enforce specs
└──────┬──────┘     └──────┬──────┘
       │                   │
       ▼                   ▼
┌─────────────┐     ┌─────────────┐
│ Engineer 1  │     │ Engineer 2  │ ← Write code, fix bugs
└─────────────┘     └─────────────┘
```

### Why Separate Agents?
- **Limited context windows** - Each agent stays focused on its role
- **Specialized expertise** - PMs manage, engineers code
- **Parallel work** - Multiple engineers can work simultaneously
- **Better memory** - Smaller contexts mean better recall

## 📸 Examples in Action

### Project Manager Coordination
![Initiate Project Manager](Examples/Initiate%20Project%20Manager.png)
*The orchestrator creating and briefing a new project manager agent*

### Status Reports & Monitoring
![Status Reports](Examples/Status%20reports.png)
*Real-time status updates from multiple agents working in parallel*

### Tmux Communication
![Reading TMUX Windows and Sending Messages](Examples/Reading%20TMUX%20Windows%20and%20Sending%20Messages.png)
*How agents communicate across tmux windows and sessions*

### Project Completion
![Project Completed](Examples/Project%20Completed.png)
*Successful project completion with all tasks verified and committed*

## 🎯 Quick Start

### Option 1: Basic Setup (Single Project)

```bash
# 1. Create a project spec
cat > project_spec.md << 'EOF'
PROJECT: My Web App
GOAL: Add user authentication system
CONSTRAINTS:
- Use existing database schema
- Follow current code patterns  
- Commit every 30 minutes
- Write tests for new features

DELIVERABLES:
1. Login/logout endpoints
2. User session management
3. Protected route middleware
EOF

# 2. Start tmux session
tmux new-session -s my-project

# 3. Start project manager in window 0
claude

# 4. Give PM the spec and let it create an engineer
"You are a Project Manager. Read project_spec.md and create an engineer 
in window 1 to implement it. Schedule check-ins every 30 minutes."

# 5. Schedule orchestrator check-in
./schedule_with_note.sh 30 "Check PM progress on auth system"
```

### Option 2: Full Orchestrator Setup

```bash
# Start the orchestrator
tmux new-session -s orchestrator
claude

# Give it your projects
"You are the Orchestrator. Set up project managers for:
1. Frontend (React app) - Add dashboard charts
2. Backend (FastAPI) - Optimize database queries
Schedule yourself to check in every hour."
```

## ✨ Key Features

### 🔄 Self-Scheduling Agents
Agents can schedule their own check-ins using:
```bash
./schedule_with_note.sh 30 "Continue dashboard implementation"
```

### 👥 Multi-Agent Coordination
- Project managers communicate with engineers
- Orchestrator monitors all project managers
- Cross-project knowledge sharing

### 💾 Automatic Git Backups
- Commits every 30 minutes of work
- Tags stable versions
- Creates feature branches for experiments

### 📊 Real-Time Monitoring
- See what every agent is doing
- Intervene when needed
- Review progress across all projects

## 📋 Best Practices

### Writing Effective Specifications

```markdown
PROJECT: E-commerce Checkout
GOAL: Implement multi-step checkout process

CONSTRAINTS:
- Use existing cart state management
- Follow current design system
- Maximum 3 API endpoints
- Commit after each step completion

DELIVERABLES:
1. Shipping address form with validation
2. Payment method selection (Stripe integration)
3. Order review and confirmation page
4. Success/failure handling

SUCCESS CRITERIA:
- All forms validate properly
- Payment processes without errors  
- Order data persists to database
- Emails send on completion
```

### Git Safety Rules

1. **Before Starting Any Task**
   ```bash
   git checkout -b feature/[task-name]
   git status  # Ensure clean state
   ```

2. **Every 30 Minutes**
   ```bash
   git add -A
   git commit -m "Progress: [what was accomplished]"
   ```

3. **When Task Completes**
   ```bash
   git tag stable-[feature]-[date]
   git checkout main
   git merge feature/[task-name]
   ```

## 🚨 Common Pitfalls & Solutions

| Pitfall | Consequence | Solution |
|---------|-------------|----------|
| Vague instructions | Agent drift, wasted compute | Write clear, specific specs |
| No git commits | Lost work, frustrated devs | Enforce 30-minute commit rule |
| Too many tasks | Context overload, confusion | One task per agent at a time |
| No specifications | Unpredictable results | Always start with written spec |
| Missing checkpoints | Agents stop working | Schedule regular check-ins |

## 🛠️ How It Works

### The Magic of Tmux
Tmux (terminal multiplexer) is the key enabler because:
- It persists terminal sessions even when disconnected
- Allows multiple windows/panes in one session
- Claude runs in the terminal, so it can control other Claude instances
- Commands can be sent programmatically to any window

### 💬 Simplified Agent Communication

We now use the `send-claude-message.sh` script for all agent communication:

```bash
# Send message to any Claude agent
./send-claude-message.sh session:window "Your message here"

# Examples:
./send-claude-message.sh frontend:0 "What's your progress on the login form?"
./send-claude-message.sh backend:1 "The API endpoint /api/users is returning 404"
./send-claude-message.sh project-manager:0 "Please coordinate with the QA team"
```

The script handles all timing complexities automatically, making agent communication reliable and consistent.

### Scheduling Check-ins
```bash
# Schedule with specific, actionable notes
./schedule_with_note.sh 30 "Review auth implementation, assign next task"
./schedule_with_note.sh 60 "Check test coverage, merge if passing"
./schedule_with_note.sh 120 "Full system check, rotate tasks if needed"
```

**Important**: The orchestrator needs to know which tmux window it's running in to schedule its own check-ins correctly. If scheduling isn't working, verify the orchestrator knows its current window with:
```bash
echo "Current window: $(tmux display-message -p "#{session_name}:#{window_index}")"
```

## 🗺️ Run One Plan Across Many Repos

`run_plan.py` turns a plan into **one Claude Code session per repo**, each in its own tmux
window and git worktree, started in dependency order.

```bash
python3 run_plan.py read     ~/code/my-context/docs/tasks/ABC-123-x/plan.md   # Opus reads plan.md + tasks.md → preview
python3 run_plan.py model    ABC-123 web-fe opus   # per repo (default sonnet), or `all`
python3 run_plan.py exercise ABC-123                       # Opus reads the plan against each repo's real code, read-only
python3 run_plan.py start    ABC-123                       # tmux session plan-ABC-123: orchestrator + watcher
tmux attach -t plan-ABC-123
python3 run_plan.py status   ABC-123                       # status, model and cost per repo, and the total
python3 run_plan.py stop     ABC-123                       # kills tmux only; worktrees and reports stay
```

- **model**: each repo session runs on its own model (`opus`, `sonnet`, `haiku` or a full
  `claude-…` id). Reading the plan again keeps what you picked.
- **exercise** (optional, as in Agents of Empire): one read-only `claude -p` per repo, in parallel,
  in the worktree the session will use. It returns a summary, the changes by `file:line`, tests,
  risks and open questions, printed for you and added to that session's brief as `## Exercise`.
  Writes are denied by `guard.py`. An exercise is kept until the plan, the repo's tasks or its
  base change. `TMUX_ORCH_EXERCISE_MODEL` (default `opus`), `TMUX_ORCH_EXERCISE_BUDGET_USD` caps
  each repo.
- **graphify**: when a worktree is made (by `exercise` or `start`), `graphify extract --code-only`
  builds a code graph of its exact commit in `<KEY>/graphs/<repo>/`: local AST extraction, no LLM,
  no cost, seconds (1–6 s on two Go services). It lives outside the worktree, so
  `git add -A` never picks it up, and is rebuilt only when the worktree's commit moves. The
  exercise and the session's brief get its path and how to use it (`explain` and `path` by
  symbol for callers and blast radius; `query` with exact identifiers). Sessions only read it:
  `guard.py` denies `graphify update/extract/watch/...` and writes to the graph. Without
  graphify (or with `TMUX_ORCH_GRAPH=0`) sessions read the code as before.
- **cost**: read and exercise record `total_cost_usd` from `claude -p`; each interactive session
  (and the orchestrator) records it through its status line, which shows `model · $cost`.
  `status` adds them up. On a Pro, Max or Team plan this is Claude Code's API-priced estimate,
  not what you are billed.

```
ABC-123  tmux: plan-ABC-123 (running)
          repo                         model    status       exercise   session
  wave 1  promo-api          sonnet   running         $0.84     $1.10
  wave 1  web-fe               opus     done            $0.62     $2.31

  read $0.38 · exercise $1.46 · repo sessions $3.41 · orchestrator $0.20 · total $5.45
```

1. **read**: Opus reads `plan.md` (and `tasks.md` beside it) and writes `sessions.json`: one
   session per repo, its tasks, `depends_on`, and `base` when the plan names a branch to start
   from. Work that is not code (deploys, Jira, manual checks) is listed as yours. Edit
   `sessions.json` if the split or order is wrong.
2. **start**: window `orchestrator` (Opus) keeps you posted; window `watcher` starts every repo
   whose dependencies have reported `done`, each in its own window.
3. Each repo session works in `<repos>/.tmux-orch/<KEY>/worktrees/<repo>` on branch
   `share/<KEY>`, cut from the freshly fetched remote default branch (or `base`). Your own
   checkouts are never touched.
4. A session finishes by writing `sessions/<repo>/report.md` (`status: done | partial | blocked`).
   Dependents start only after `done`, with the upstream summaries in their brief. A question
   goes in `question.md`; the watcher tells the orchestrator, which answers or asks you.
5. `git push`, `gh pr …`, kubectl, terraform, cloud CLIs and DB clients **ask for approval in the
   repo's window**; force-push is denied. The watcher tells the orchestrator when a window is
   waiting for approval, idle, or its Claude exited without a report.
6. **`guard.py`** runs as a PreToolUse hook in every repo session (the same idea as Agents of
   Empire's guard):
   - Edit/Write outside the session's worktree and `sessions/<repo>/` is denied, symlinks included.
   - Bash that writes into a main checkout or another session's worktree is denied
     (`echo > /…/go-lib/x`, `cd /…/go-lib && git commit`, `git -C /…/go-lib checkout`); reading
     them is fine.
   - Rebase and force-push are denied in any spelling; every push or PR change asks, including
     `git -C . push`, `env X=1 git push` and `make && git push`.

   It matches command text, so a script that writes through an interpreter can slip past it: a
   guard against mistakes, not a sandbox.

| Variable | Default |
|---|---|
| `TMUX_ORCH_REPOS` | `~/code` (folder of git checkouts) |
| `TMUX_ORCH_HOME` | `<repos>/.tmux-orch` (state, briefs, reports, worktrees) |
| `TMUX_ORCH_REPO_MODEL` / `TMUX_ORCH_ORCH_MODEL` / `TMUX_ORCH_READ_MODEL` | `sonnet` / `opus` / `opus` (per repo: `model` in `sessions.json`) |
| `TMUX_ORCH_IDLE_MINUTES` · `TMUX_ORCH_POLL` | `10` · `10` seconds |

Tests: `python3 -m unittest discover -s tests` (the end-to-end test uses real tmux and git with a
fake `claude`).

## 🎓 Advanced Usage

### Multi-Project Orchestration
```bash
# Start orchestrator
tmux new-session -s orchestrator

# Create project managers for each project
tmux new-window -n frontend-pm
tmux new-window -n backend-pm  
tmux new-window -n mobile-pm

# Each PM manages their own engineers
# Orchestrator coordinates between PMs
```

### Cross-Project Intelligence
The orchestrator can share insights between projects:
- "Frontend is using /api/v2/users, update backend accordingly"
- "Authentication is working in Project A, use same pattern in Project B"
- "Performance issue found in shared library, fix across all projects"

## 📚 Core Files

- `send-claude-message.sh` - Simplified agent communication script
- `schedule_with_note.sh` - Self-scheduling functionality
- `tmux_utils.py` - Tmux interaction utilities
- `CLAUDE.md` - Agent behavior instructions
- `LEARNINGS.md` - Accumulated knowledge base

## 🤝 Contributing & Optimization

The orchestrator evolves through community discoveries and optimizations. When contributing:

1. Document new tmux commands and patterns in CLAUDE.md
2. Share novel use cases and agent coordination strategies
3. Submit optimizations for claudes synchronization
4. Keep command reference up-to-date with latest findings
5. Test improvements across multiple sessions and scenarios

Key areas for enhancement:
- Agent communication patterns
- Cross-project coordination
- Novel automation workflows

## 📄 License

MIT License - Use freely but wisely. Remember: with great automation comes great responsibility.

---

*"The tools we build today will program themselves tomorrow"* - Alan Kay, 1971