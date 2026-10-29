#!/usr/bin/env python3
"""pr_overview - one-screen map of a pull request, signal separated from noise.

  pr_overview.py https://github.com/OWNER/REPO/pull/N [--save DIR] [--depth 2]

Prints metadata, the description, commits, churn per area (first path
segments) and the files most worth reading. Generated code, lock files, tests
and docs are counted but kept out of the "read these" list, so a 300-file PR
shows the 15 files that carry the idea. With --save it also writes pr.json,
files.json and pr.diff (generated files left out) into DIR. Needs an
authenticated `gh`.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys

GENERATED = re.compile(
    r"(\.pb\.go$|_grpc\.pb\.go$|\.pb\.gw\.go$|zz_generated|\.gen\.[a-z]+$|_gen\.go$|(^|/)generated/|"
    r"(^|/)\.openapi-generator/|/openapi/(model_|api_|docs/|api/|README)|(^|/)mock_[^/]+\.go$|"
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|go\.sum|Cargo\.lock|poetry\.lock|uv\.lock)$|"
    r"(^|/)(vendor|dist|node_modules|__snapshots__)/|\.min\.(js|css)$|\.snap$|\.svg$|\.png$)")
TESTS = re.compile(r"(_test\.go$|\.(test|spec)\.[jt]sx?$|(^|/)(tests?|e2e|testdata|__tests__|fixtures)/|_test\.py$|(^|/)test_[^/]+\.py$)")
DOCS = re.compile(r"(\.mdx?$|\.rst$|\.adoc$|(^|/)docs?/|(^|/)LICENSE|(^|/)CHANGELOG)")


def gh(*args):
    r = subprocess.run(["gh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode:
        sys.exit(f"ERROR: gh {' '.join(args[:3])}...: {r.stderr.strip()}")
    return r.stdout


def kind(path):
    if GENERATED.search(path):
        return "generated"
    if TESTS.search(path):
        return "test"
    if DOCS.search(path):
        return "docs"
    return "signal"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", help="pull request URL")
    ap.add_argument("--save", metavar="DIR", help="also write pr.json, files.json, pr.diff here")
    ap.add_argument("--depth", type=int, default=2, help="path segments that make an 'area' (default 2)")
    ap.add_argument("--top", type=int, default=25, help="how many signal files to list")
    a = ap.parse_args()
    m = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)", a.pr)
    if not m:
        sys.exit("ERROR: expected a URL like https://github.com/OWNER/REPO/pull/123")
    owner, repo, num = m.groups()

    pr = json.loads(gh("pr", "view", a.pr, "--json",
                       "title,body,author,state,isDraft,baseRefName,headRefName,headRefOid,additions,deletions,"
                       "changedFiles,commits,labels,closingIssuesReferences,url,mergedAt"))
    files = json.loads(gh("api", "--paginate", "--slurp", f"repos/{owner}/{repo}/pulls/{num}/files?per_page=100"))
    files = [f for page in files for f in page]
    for f in files:
        f["kind"] = kind(f["filename"])

    if a.save:
        os.makedirs(a.save, exist_ok=True)
        with open(os.path.join(a.save, "pr.json"), "w") as fh:
            json.dump(pr, fh, indent=2)
        with open(os.path.join(a.save, "files.json"), "w") as fh:
            json.dump([{k: f.get(k) for k in ("filename", "status", "additions", "deletions", "kind", "previous_filename")}
                       for f in files], fh, indent=1)
        # `gh pr diff` refuses diffs over 20k lines, which is exactly when the noise filter matters:
        # write the per-file patches instead, generated files left out.
        with open(os.path.join(a.save, "pr.diff"), "w") as fh:
            for f in files:
                if f["kind"] == "generated":
                    continue
                old = f.get("previous_filename") or f["filename"]
                fh.write(f"diff --git a/{old} b/{f['filename']}\n# status: {f['status']}  kind: {f['kind']}\n")
                fh.write((f.get("patch") or "# (no patch from the API: binary or too large; read it in a checkout)") + "\n")

    print(f"# {pr['title']}")
    print(f"{owner}/{repo}#{num}  {pr['state']}{' (draft)' if pr.get('isDraft') else ''}  by {pr['author']['login']}  "
          f"{pr['headRefName']} -> {pr['baseRefName']}  +{pr['additions']} -{pr['deletions']}  {pr['changedFiles']} files")
    issues = [f"#{i['number']} {i.get('title', '')}" for i in pr.get("closingIssuesReferences") or []]
    if issues:
        print("closes: " + "; ".join(issues))
    print("\n## Description\n" + (pr.get("body") or "(empty)").strip())
    print(f"\n## Commits ({len(pr['commits'])})")
    for c in pr["commits"][:40]:
        print(f"  {c['oid'][:8]} {c['messageHeadline']}")

    by_kind = collections.Counter()
    for f in files:
        by_kind[f["kind"]] += f["additions"] + f["deletions"]
    total = sum(by_kind.values()) or 1
    print("\n## Where the churn is")
    print("  " + "   ".join(f"{k}: {v} lines ({v * 100 // total}%)" for k, v in by_kind.most_common()))

    areas = collections.defaultdict(lambda: collections.Counter())
    for f in files:
        area = "/".join(f["filename"].split("/")[:a.depth]) if "/" in f["filename"] else "(root)"
        c = areas[area]
        c["files"] += 1
        c["add"] += f["additions"]
        c["del"] += f["deletions"]
        c["added"] += f["status"] == "added"
        c["removed"] += f["status"] == "removed"
        if f["kind"] == "signal":
            c["sig"] += f["additions"] + f["deletions"]
    print(f"\n## Areas (depth {a.depth}; 'signal' excludes generated, tests, docs)")
    print(f"  {'area':<44} {'files':>5} {'+':>6} {'-':>6} {'signal':>7}  new/deleted files")
    for area, c in sorted(areas.items(), key=lambda kv: -kv[1]["sig"]):
        print(f"  {area[:44]:<44} {c['files']:>5} {c['add']:>6} {c['del']:>6} {c['sig']:>7}  +{c['added']}/-{c['removed']}")

    signal = sorted((f for f in files if f["kind"] == "signal"), key=lambda f: -(f["additions"] + f["deletions"]))
    print(f"\n## Read these first ({min(a.top, len(signal))} of {len(signal)} signal files, by churn)")
    for f in signal[:a.top]:
        print(f"  {f['status']:<9} +{f['additions']:<5} -{f['deletions']:<5} {f['filename']}")
    added = [f["filename"] for f in files if f["status"] == "added" and f["kind"] == "signal"]
    removed = [f["filename"] for f in files if f["status"] == "removed" and f["kind"] == "signal"]
    print(f"\n## New signal files ({len(added)})")
    for p in added[:40]:
        print(f"  {p}")
    print(f"\n## Deleted signal files ({len(removed)})")
    for p in removed[:40]:
        print(f"  {p}")
    if a.save:
        print(f"\nsaved pr.json, files.json, pr.diff in {a.save}")


if __name__ == "__main__":
    main()
