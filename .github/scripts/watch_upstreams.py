#!/usr/bin/env python3
"""Daily: for every watched (repo, branch, path) find the newest commit touching it. Anything newer than the
recorded state opens ONE issue per feature, listing the new upstream commits and our files that implement the
same feature. The state file is committed back so each change is reported once. Needs GH_TOKEN + gh CLI."""
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

CFG = ".github/upstream-watch.json"
STATE = ".github/upstream-watch-state.json"
REPORT = ".github/UPSTREAM-CHANGES.md"
MAX_COMMITS = 15


def gh(*args):
  r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
  if r.returncode != 0:
    raise RuntimeError(r.stderr.strip()[:300])
  return json.loads(r.stdout or "null")


def commits(repo, branch, path, since=None):
  q = f"repos/{repo}/commits?sha={branch}&path={path}&per_page={MAX_COMMITS}"
  if since:
    q += f"&since={since}"
  return gh(q) or []


def main():
  cfg = json.load(open(CFG))
  try:
    state = json.load(open(STATE))
  except (OSError, ValueError):
    state = {}
  first_run = not state
  issues, errors = [], []
  for w in cfg["watches"]:
    new_lines = []
    for path in w["paths"]:
      key = f"{w['repo']}@{w['branch']}:{path}"
      try:
        latest = commits(w["repo"], w["branch"], path)
      except RuntimeError as e:
        errors.append(f"{key}: {e}")
        continue
      if not latest:
        continue
      prev = state.get(key)
      head = latest[0]
      if prev and prev["sha"] != head["sha"]:
        try:
          since_list = commits(w["repo"], w["branch"], path, since=prev["date"])
        except RuntimeError:
          since_list = latest
        for c in since_list:
          if c["sha"] == prev["sha"]:
            break
          msg = c["commit"]["message"].split("\n")[0][:110]
          new_lines.append(f"- [`{c['sha'][:8]}`]({c['html_url']}) {c['commit']['author']['date'][:10]} " +
                           f"`{path}`: {msg}")
      state[key] = {"sha": head["sha"], "date": head["commit"]["committer"]["date"]}
    if new_lines and not first_run:
      body = (f"Upstream **{w['repo']}** (`{w['branch']}`) changed code behind a feature this fork also has: " +
              f"**{w['feature']}**.\n\nNew upstream commits:\n" + "\n".join(new_lines) +
              "\n\nOur implementation to compare: " + ", ".join(f"`{p}`" for p in w["ours"]) +
              "\n\n_Opened by watch-upstreams.yaml. Close it once reviewed (port, ignore, or note why)._")
      issues.append((f"Upstream change: {w['feature']} ({w['repo']})", body))
  fallback = []
  for title, body in issues:
    r = subprocess.run(["gh", "issue", "create", "--title", title, "--body", body, "--label", "upstream-watch"],
                       capture_output=True, text=True)
    if r.returncode != 0:                     # issues disabled on the fork -> report file + failed run (email)
      fallback.append(f"## {datetime.now(UTC):%Y-%m-%d} {title}\n\n{body}\n")
  if fallback:
    old = open(REPORT).read() if os.path.exists(REPORT) else "# Upstream changes to review\n\nNewest first. Delete an entry once reviewed.\n\n"
    head, _, rest = old.partition("\n\n## ")
    head = old if not rest else head
    open(REPORT, "w").write(head.rstrip("\n") + "\n\n" + "\n".join(fallback) + ("\n## " + rest if rest else ""))
    open("/tmp/upstream_alert", "w").write(str(len(fallback)))
  json.dump(state, open(STATE, "w"), indent=1, sort_keys=True)
  print(f"{datetime.now(UTC):%Y-%m-%d %H:%M} watched={sum(len(w['paths']) for w in cfg['watches'])} " +
        f"issues={len(issues)} first_run={first_run} errors={len(errors)}")
  for e in errors:
    print("WARN", e)
  return 0


if __name__ == "__main__":
  sys.exit(main())
