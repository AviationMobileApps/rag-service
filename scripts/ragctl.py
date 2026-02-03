#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or not float(seconds) == float(seconds):
        return "?"
    seconds = float(seconds)
    if seconds < 1:
        return "<1s"
    mins, sec = divmod(int(seconds + 0.5), 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h{mins:02d}m{sec:02d}s"
    if mins:
        return f"{mins}m{sec:02d}s"
    return f"{sec}s"


def _headers(api_key: str, workspace_id: Optional[str], principal_id: Optional[str]) -> dict[str, str]:
    h = {"Authorization": f"Bearer {api_key}"}
    if workspace_id:
        h["X-Workspace-Id"] = workspace_id
    if principal_id:
        h["X-Principal-Id"] = principal_id
    return h


def _guess_content_type(path: Path) -> str:
    ct, _ = mimetypes.guess_type(str(path))
    return ct or "application/octet-stream"


def ingest_one(
    *,
    api_url: str,
    api_key: str,
    scope: str,
    file_path: Path,
    workspace_id: Optional[str],
    principal_id: Optional[str],
    timeout_s: float,
) -> str:
    url = api_url.rstrip("/") + "/v1/ingest/document"
    headers = _headers(api_key, workspace_id, principal_id)

    content_type = _guess_content_type(file_path)
    cmd: list[str] = [
        "curl",
        "-sS",
        "--fail-with-body",
        "--max-time",
        str(timeout_s),
        "-X",
        "POST",
        url,
        "-F",
        f"scope={scope}",
        "-F",
        f"file=@{str(file_path)};type={content_type}",
    ]
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"curl failed with exit {proc.returncode}")

    payload = json.loads(proc.stdout)
    return str(payload.get("doc_id") or "")


def _iter_matching_files(root: Path, pattern: str):
    try:
        for p in root.glob(pattern):
            try:
                if p.is_file():
                    yield p
            except OSError:
                continue
    except Exception as e:
        raise RuntimeError(f"glob failed for root={root} pattern={pattern}: {e}") from e


def cmd_ingest_dir(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"ERROR: root path does not exist: {root}", file=sys.stderr, flush=True)
        return 2

    pattern = args.glob or "**/*.md"
    limit = int(args.limit or 0)
    prescan = bool(getattr(args, "prescan", False))

    print(f"Scanning {root} for {pattern} …", file=sys.stderr, flush=True)

    total: int | None = None
    if prescan:
        # Count first so we can show accurate totals/ETA, at the cost of slower startup.
        matched = 0
        for _ in _iter_matching_files(root, pattern):
            matched += 1
            if limit > 0 and matched >= limit:
                break
        if limit > 0:
            total = min(matched, limit)
        else:
            total = matched

    if limit > 0 and total is None:
        total = limit

    if limit > 0:
        print(f"Enqueuing up to {limit} file(s) from {root} …", flush=True)
    else:
        print(f"Enqueuing file(s) from {root} …", flush=True)

    failures: list[tuple[Path, str]] = []
    t0 = time.time()
    completed = 0
    ok = 0
    submitted = 0
    last_progress_ts = 0.0
    progress_is_tty = sys.stderr.isatty()
    progress_min_interval_s = 0.25 if progress_is_tty else 2.0

    def _render_progress(*, final: bool = False) -> None:
        nonlocal last_progress_ts
        now = time.time()
        if not final and (now - last_progress_ts) < progress_min_interval_s:
            return
        last_progress_ts = now

        elapsed = max(0.001, now - t0)
        rate = completed / elapsed
        if total is not None:
            remaining = max(0, int(total) - completed)
            eta = remaining / rate if rate > 0 else None
            line = f"[{completed}/{total}] ok={ok} failed={len(failures)} remaining={remaining} rate={rate:.2f}/s eta={_fmt_eta(eta)}"
        else:
            line = f"[{completed}] ok={ok} failed={len(failures)} submitted={submitted} rate={rate:.2f}/s"
        if final:
            print(line, file=sys.stderr)
        else:
            if progress_is_tty:
                print(line.ljust(140), end="\r", file=sys.stderr, flush=True)
            else:
                print(line, file=sys.stderr, flush=True)

    def _run(p: Path) -> tuple[Path, str, float]:
        start = time.time()
        try:
            doc_id = ingest_one(
                api_url=args.api_url,
                api_key=args.api_key,
                scope=args.scope,
                file_path=p,
                workspace_id=args.workspace_id,
                principal_id=args.principal_id,
                timeout_s=args.timeout_s,
            )
            if not doc_id:
                raise RuntimeError("missing doc_id in response")
            return p, doc_id, time.time() - start
        except Exception as e:
            return p, f"ERROR: {e}", time.time() - start

    with ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as ex:
        pending = set()
        path_iter = _iter_matching_files(root, pattern)
        done_scanning = False

        def _submit_next() -> bool:
            nonlocal submitted, done_scanning, total
            if limit > 0 and submitted >= limit:
                done_scanning = True
                return False
            try:
                p = next(path_iter)
            except StopIteration:
                done_scanning = True
                if total is None:
                    total = submitted
                elif submitted < int(total):
                    total = submitted
                return False
            pending.add(ex.submit(_run, p))
            submitted += 1
            return True

        # Prime the queue.
        while len(pending) < int(args.concurrency) and _submit_next():
            pass
        if done_scanning and not pending and submitted == 0:
            print("No files matched.", flush=True)
            return 0

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                p, result, dt_s = fut.result()
                completed += 1
                if result.startswith("ERROR:"):
                    failures.append((p, result))
                    print(file=sys.stderr, flush=True)
                    print(f"{p}: {result} ({dt_s:.2f}s)", file=sys.stderr, flush=True)
                else:
                    ok += 1
                    print(f"{p}: {result} ({dt_s:.2f}s)", flush=True)
                _render_progress()

            while len(pending) < int(args.concurrency) and _submit_next():
                pass

    dt = time.time() - t0
    print(file=sys.stderr, flush=True)
    if total is None:
        total = completed
    _render_progress(final=True)
    print(f"Done in {dt:.1f}s. ok={ok} failed={len(failures)} total={completed}", flush=True)
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ragctl", description="rag-service helper CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    ingest = sub.add_parser("ingest-dir", help="Upload + enqueue all files in a directory (e.g. 10k .md files)")
    ingest.add_argument("--api-url", default=os.getenv("RAG_API_URL", "http://localhost:8021"))
    ingest.add_argument("--api-key", default=os.getenv("RAG_API_KEY"))
    ingest.add_argument("--root", required=True, help="Root directory to scan")
    ingest.add_argument("--glob", default="**/*.md", help="Glob relative to root (default: **/*.md)")
    ingest.add_argument("--scope", default="tenant", choices=["tenant", "workspace", "user"])
    ingest.add_argument("--workspace-id", default=os.getenv("RAG_WORKSPACE_ID"))
    ingest.add_argument("--principal-id", default=os.getenv("RAG_PRINCIPAL_ID"))
    ingest.add_argument("--concurrency", type=int, default=4)
    ingest.add_argument("--timeout-s", type=float, default=30.0)
    ingest.add_argument("--limit", type=int, default=0, help="Optional cap for testing (0 = no cap)")
    ingest.add_argument("--prescan", action="store_true", help="Count matches before uploading (slower start, accurate totals/ETA)")
    ingest.set_defaults(func=cmd_ingest_dir)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "api_key", None) in (None, ""):
        print("ERROR: missing --api-key (or set RAG_API_KEY).", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
