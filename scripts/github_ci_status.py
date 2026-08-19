"""Did THIS EXACT COMMIT pass CI? One command, one answer.

WHY THIS EXISTS. "CI is green" has been claimed for a neighbouring commit, a
branch's usual state, and a local test run. None of those are the same
statement. A run is evidence for a commit only if its `head_sha` is that
commit, and the only way to be sure is to ask GitHub for that SHA and check
the field.

WHAT IT REFUSES TO DO. It never infers success from a nearby run, never
treats "no run found" as passing, and never prints a token. An unfinished
run is reported as unfinished rather than rounded to either outcome.

PUSH AND PULL_REQUEST ARE DIFFERENT EVENTS, and `gh run list --commit` does
not always surface both. So the Actions API is queried directly with
`head_sha=`, which returns every run for the commit regardless of event.
An empty answer from one view is not proof there was no CI.

THIS IS DEVELOPER TOOLING. Nothing in the trading runtime imports it. If
GitHub is down, JARVIS keeps collecting, deciding and settling — the only
thing that stops is a developer's ability to ask about CI.

Exit codes, so a script can branch on the answer:

    0   SUCCESS       every run for this SHA concluded successfully
    1   FAILURE       at least one concluded failure/timeout/cancelled
    2   IN_PROGRESS   queued or running; no verdict yet
    3   NO_RUN        GitHub has no run for this exact SHA
    4   UNAVAILABLE   gh missing, not authenticated, or the API failed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

SUCCESS, FAILURE, IN_PROGRESS, NO_RUN, UNAVAILABLE = (
    "SUCCESS", "FAILURE", "IN_PROGRESS", "NO_RUN", "UNAVAILABLE")

EXIT = {SUCCESS: 0, FAILURE: 1, IN_PROGRESS: 2, NO_RUN: 3, UNAVAILABLE: 4}

# A conclusion that is not "success" and not empty means the run finished
# badly. Listed explicitly so a new GitHub conclusion string is not silently
# treated as passing.
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required",
                   "startup_failure", "stale", "neutral"}


def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=120)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def repo_root() -> str | None:
    code, out, _ = _run(["git", "rev-parse", "--show-toplevel"])
    return out if code == 0 and out else None


def collect(root: str) -> dict:
    """Everything needed to answer the question, or the reason we cannot."""
    info: dict = {"repo_root": root}

    if not shutil.which("gh"):
        return {**info, "overall": UNAVAILABLE,
                "reason": "gh is not installed on PATH"}

    code, out, err = _run(["gh", "auth", "status"], cwd=root)
    info["authenticated"] = code == 0
    if code != 0:
        return {**info, "overall": UNAVAILABLE,
                "reason": f"gh is not authenticated: {err or out}"[:200]}
    for line in (out or "").splitlines():
        if "Logged in to" in line and "account" in line:
            info["account"] = line.split("account", 1)[1].strip().split()[0]

    code, sha, _ = _run(["git", "rev-parse", "HEAD"], cwd=root)
    if code != 0:
        return {**info, "overall": UNAVAILABLE, "reason": "no git HEAD"}
    info["sha"] = sha

    _, branch, _ = _run(["git", "branch", "--show-current"], cwd=root)
    info["branch"] = branch or "(detached)"
    _, dirty, _ = _run(["git", "status", "--porcelain"], cwd=root)
    info["dirty"] = bool(dirty)

    code, nwo, _ = _run(["gh", "repo", "view", "--json", "nameWithOwner",
                         "--jq", ".nameWithOwner"], cwd=root)
    if code != 0 or not nwo:
        return {**info, "overall": UNAVAILABLE,
                "reason": "could not resolve the GitHub repository"}
    info["repo"] = nwo

    # Remote tip, so "in sync" is a fact rather than an assumption.
    _, remote_sha, _ = _run(
        ["git", "rev-parse", f"origin/{branch}"], cwd=root) if branch else (0, "", "")
    info["remote_sha"] = remote_sha or None
    info["in_sync"] = bool(remote_sha) and remote_sha == sha

    # THE QUERY THAT MATTERS. head_sha= returns runs for this commit across
    # every event, which `gh run list --commit` does not reliably do.
    code, raw, err = _run(
        ["gh", "api", f"repos/{nwo}/actions/runs?head_sha={sha}&per_page=100",
         "--jq", ".workflow_runs"], cwd=root)
    if code != 0:
        return {**info, "overall": UNAVAILABLE,
                "reason": f"Actions query failed: {err[:160]}"}
    try:
        runs = json.loads(raw) if raw else []
    except ValueError:
        return {**info, "overall": UNAVAILABLE,
                "reason": "unparseable Actions response"}

    # Belt and braces: never trust a run whose head_sha is not this commit.
    runs = [r for r in runs if r.get("head_sha") == sha]
    info["runs"] = [{
        "id": r.get("id"), "name": r.get("name"), "event": r.get("event"),
        "status": r.get("status"), "conclusion": r.get("conclusion"),
        "head_sha": r.get("head_sha"), "url": r.get("html_url"),
    } for r in runs]

    if not runs:
        return {**info, "overall": NO_RUN,
                "reason": "GitHub has no workflow run for this exact SHA"}
    if any(r.get("status") != "completed" for r in runs):
        return {**info, "overall": IN_PROGRESS,
                "reason": "at least one run has not finished"}
    bad = [r for r in runs
           if (r.get("conclusion") or "").lower() in BAD_CONCLUSIONS]
    if bad:
        return {**info, "overall": FAILURE,
                "reason": ", ".join(f"{r.get('name')}={r.get('conclusion')}"
                                    for r in bad)}
    unknown = [r for r in runs if (r.get("conclusion") or "").lower()
               not in {"success", "skipped"}]
    if unknown:
        return {**info, "overall": FAILURE,
                "reason": "unrecognised conclusion: " + ", ".join(
                    str(r.get("conclusion")) for r in unknown)}
    return {**info, "overall": SUCCESS,
            "reason": f"{len(runs)} run(s) succeeded for this SHA"}


def render(info: dict) -> str:
    lines = [
        f"GITHUB AUTH       {'OK' if info.get('authenticated') else 'NO'}"
        + (f"  ({info['account']})" if info.get("account") else ""),
        f"REPOSITORY        {info.get('repo', '?')}",
        f"BRANCH            {info.get('branch', '?')}"
        + ("  [dirty]" if info.get("dirty") else ""),
        f"LOCAL SHA         {(info.get('sha') or '?')[:12]}",
        f"REMOTE SHA        {(info.get('remote_sha') or '(unknown)')[:12]}",
        f"SYNC              {'IN_SYNC' if info.get('in_sync') else 'DIVERGED/UNKNOWN'}",
        f"ACTIONS           {info.get('overall', '?')}",
    ]
    for r in info.get("runs", []):
        lines.append(
            f"  - {str(r.get('name'))[:34]:<34} {str(r.get('event')):<13}"
            f" {str(r.get('status')):<11} {str(r.get('conclusion'))}")
    if info.get("reason"):
        lines.append(f"WHY               {info['reason']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Report whether THIS EXACT commit passed GitHub Actions.")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    args = ap.parse_args(argv)

    root = repo_root()
    if root is None:
        info = {"overall": UNAVAILABLE, "reason": "not inside a git repository"}
    else:
        info = collect(root)

    print(json.dumps(info, indent=2) if args.json else render(info))
    return EXIT.get(info.get("overall", UNAVAILABLE), EXIT[UNAVAILABLE])


if __name__ == "__main__":
    raise SystemExit(main())
