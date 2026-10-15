#!/usr/bin/env python3
"""等 API 配额恢复后，把 run #3 的 job 与 annotation 抓下来。

为什么要费这个劲：CI 的原始日志要登录才能下载（`gh` 本机没装，也没有 token），
而 **annotation 是匿名可读的**。构建脚本现在每进入一个阶段就发一条 `::notice::`、
失败时发 `::error::`，所以 annotation 本身就足以定位死在哪一步。

用法：python3 packaging/watch_ci.py [--once]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

REPO = "zhangjinluuu/bili-claw"
API = f"https://api.github.com/repos/{REPO}"


def get(path: str) -> tuple[int, dict | list | None]:
    """返回 (状态码, JSON)。403/404 不抛异常——配额耗尽时这就是正常结果。"""
    request = urllib.request.Request(
        path if path.startswith("http") else f"{API}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "bclaw-ci-watch"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except (OSError, ValueError) as error:
        # 网络抖动或响应不是 JSON——两种情况都不该让这个脚本死掉，它只是来读状态的。
        print(f"  （请求失败：{error}）", flush=True)
        return 0, None


def wait_for_quota() -> None:
    """配额没了就睡到重置。`/rate_limit` 本身不计入配额，所以这步永远安全。"""
    while True:
        status, payload = get("https://api.github.com/rate_limit")
        if status != 200 or not isinstance(payload, dict):
            print("  查不到配额，等 60 秒再试", flush=True)
            time.sleep(60)
            continue
        core = payload["resources"]["core"]
        if core["remaining"] > 3:
            print(f"  配额 {core['remaining']}/{core['limit']}，继续", flush=True)
            return
        wait = max(core["reset"] - int(time.time()), 0) + 15
        print(
            f"  配额 {core['remaining']}/{core['limit']}，睡 {wait // 60} 分 {wait % 60} 秒",
            flush=True,
        )
        time.sleep(wait)


def tag_runs(limit: int = 3) -> list[dict]:
    """最近几次 tag 触发的 run，新的在前。

    刻意取多次而不是只取最新的一次：修复前那次 run 的 annotation 正是"CRLF 假设"
    的独立佐证，修复后那次才是结论。两个都看到，才算真的说清楚。
    """
    status, payload = get("/actions/runs?event=push&per_page=20")
    if status != 200 or not isinstance(payload, dict):
        return []
    runs = [r for r in payload.get("workflow_runs", []) if r.get("head_branch", "").startswith("v")]
    return runs[:limit]


def report(run: dict) -> None:
    print(f"\n=== run #{run['run_number']}  {run['head_branch']}  {run['head_sha'][:7]}", flush=True)
    print(f"    状态 {run['status']}  结论 {run['conclusion']}", flush=True)
    print(f"    {run['html_url']}", flush=True)

    status, payload = get(f"/actions/runs/{run['id']}/jobs")
    if status != 200 or not isinstance(payload, dict):
        print(f"  jobs 读不到（HTTP {status}）", flush=True)
        return

    for job in payload.get("jobs", []):
        print(f"\n  ▸ {job['name']:<16} {job['status']:<12} {job.get('conclusion')}", flush=True)
        for step in job.get("steps", []):
            mark = {"success": "✓", "failure": "✗", "skipped": "-"}.get(step.get("conclusion") or "", "…")
            print(f"      {mark} {step['name']}", flush=True)

        # job id 就是 check run id —— 这是拿 annotation 的唯一入口，而它匿名可读。
        astatus, annotations = get(f"/check-runs/{job['id']}/annotations")
        if astatus != 200 or not isinstance(annotations, list):
            print(f"      （annotation 读不到：HTTP {astatus}）", flush=True)
            continue
        for note in annotations:
            level = note.get("annotation_level", "?")
            title = (note.get("title") or "").strip()
            message = (note.get("message") or "").strip()
            print(f"      [{level}] {title}: {message}", flush=True)


def main() -> int:
    once = "--once" in sys.argv
    while True:
        wait_for_quota()
        runs = tag_runs()
        if not runs:
            print("  还没有 tag 触发的 run，等 60 秒", flush=True)
        else:
            for run in runs:
                report(run)
            if once or all(run["status"] == "completed" for run in runs):
                return 0
        if once:
            return 1
        time.sleep(180)


if __name__ == "__main__":
    raise SystemExit(main())
