---
name: dst-release-wrapup
description: >-
  End-of-task release wrap-up workflow for the jev_dst_agent DST mod repository.
  Automatically evaluates code changes to decide whether to bump the mod/server
  version (minor, patch, or skip), synchronizes all four version declarations,
  packages the Steam Workshop release via package_mods.py, runs packaging
  verification tests, audits git status and git diff --stat for unintended
  files, and commits and pushes to the remote repository. Use when the user asks
  to wrap up, package and push, release to Workshop, or finalize changes.
---

# DST Release Wrap-Up

## Overview

Automates the end-of-task release checklist for `jev_dst_agent` so Steam Workshop uploads never fail due to stale version numbers and repository pushes stay clean and verified. Runs end-to-end automatically unless a guardrail triggers.

## Dependencies

None. This is an instruction-only orchestration skill using the workspace's existing scripts (`python/tools/package_mods.py`, `python/tests/test_workshop_packaging.py`) and `git`.

## Quick Start

Trigger this skill after completing feature work, bug fixes, or skin updates with prompts such as:
- "收尾打包并提交推送"
- "Run release wrap-up and push"
- "更新版本号、打包工坊并 git push"

## Workflow

Execute the following 5 steps in order. On Windows PowerShell 5.1, separate commands with `;` rather than `&&`.

### 1. Inspect Changes & Guard Against Unintended Files

Run:
```powershell
git status --short; git diff --stat
```
Check both modified and untracked files:
- **Abort immediately** and report to the user if any sensitive or unignored artifact appears in `git status`, such as `.env`, API keys/tokens, `logs/`, `dist/`, `data/agent_memory/`, or large generated skin caches (`skins/*/work/`, `skins/*/player_data/`, `skins/*/review_data/`, `skins/*/tuner_data/`).

### 2. Evaluate & Synchronize Version Number

Check the current version in `mods/dst_buddy_agent/modinfo.lua` vs. `HEAD`:
```powershell
git diff HEAD -- mods/dst_buddy_agent/modinfo.lua
```
Decide the version bump automatically based on the scope of changes since `HEAD`:
- **Already bumped**: If `modinfo.lua` already has a higher version than `HEAD`, keep it and verify all 4 locations match.
- **Minor bump (`X.Y+1.0`)**: New user-facing gameplay features, new skins, new Lua actions/capabilities, new MCP tools, or protocol extensions.
- **Patch bump (`X.Y.Z+1`)**: Bug fixes, heuristic/tuning tweaks, performance fixes, or small Lua/Python runtime adjustments.
- **No bump**: Pure documentation, tests, or `.agents/skills/` changes with no runtime mod/Python behavior change.

When bumping (or fixing drift), keep all **4 locations** strictly synchronized:
1. `mods/dst_buddy_agent/modinfo.lua` (`version = "X.Y.Z"`)
2. `mods/dst_buddy_agent/modmain.lua` (`version="X.Y.Z"` in `log(...)`)
3. `python/mcp_server/companion_server.py` (`MCPServer("DSTCompanion", version="X.Y.Z", ...)`)
4. `python/tests/test_workshop_packaging.py` (all version assertions for `dst_buddy_agent`)

Verify zero stale version strings remain across those 4 files before proceeding.

### 3. Package Workshop Release & Verify

Run the packager and the 5-test workshop verification suite:
```powershell
python .\python\tools\package_mods.py
$env:PYTHONPATH="python"; python -m unittest discover -s python/tests -p test_workshop_packaging.py -v
```
- Confirm `dist/dst_buddy_agent.zip`, `dist/dst_agent_runner.zip`, and `dist/workshop/dst_buddy_agent/` are regenerated.
- **Abort immediately** if `package_mods.py` exits non-zero or any test in `test_workshop_packaging.py` fails. Never commit or push broken release artifacts.

### 4. Final Diff Audit

Re-run `git status --short; git diff --stat` after packaging (and any version edits) to confirm:
- Only intended source, config, doc, and test files are changed.
- `dist/` remains ignored by `.gitignore`.

### 5. Commit & Push

Stage, commit with a concise Conventional Commit message summarizing the changes (including the new version if bumped), and push:
```powershell
git add -A; git status --short; git commit -m "<type>: <summary>"
git push
```
- **Sandbox handling**: If `git add`/`git commit` fails with `Permission denied` on `.git/index.lock` or `git push` fails due to no network inside the standard sandbox, retry the single blocked git command with `BypassSandbox: true`.
- If `git push` fails due to non-fast-forward or authentication errors, **stop and report** the exact error to the user; never force-push.
- Report a concise final summary: version decision (old -> new or unchanged), packaged `dist/` outputs, test result (`5/5 PASS`), commit hash, and pushed branch.

## Common Mistakes

1. **Forgetting to bump `modinfo.lua` before uploading to Steam Workshop**: Klei's Workshop uploader rejects or skips updates when `version` is unchanged. Always run Step 2 before Step 3.
2. **Updating only `modinfo.lua` and breaking `test_workshop_packaging.py`**: All 4 version declarations (`modinfo.lua`, `modmain.lua`, `companion_server.py`, `test_workshop_packaging.py`) must match.
3. **Using `&&` in Windows PowerShell 5.1**: PowerShell 5.1 treats `&&` as a syntax error; always use `;` or separate statements.
4. **Running `unittest` without `PYTHONPATH=python`**: `test_workshop_packaging.py` imports `common.pairing` and requires `$env:PYTHONPATH="python"`.
