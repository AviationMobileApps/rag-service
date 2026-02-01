#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


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


def cmd_ingest_dir(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"ERROR: root path does not exist: {root}", file=sys.stderr)
        return 2

    patterns = args.glob or "**/*.md"
    paths = sorted({p for p in root.glob(patterns) if p.is_file()})
    if args.limit and args.limit > 0:
        paths = paths[: args.limit]

    if not paths:
        print("No files matched.")
        return 0

    print(f"Enqueuing {len(paths)} files from {root} …")

    failures: list[tuple[Path, str]] = []
    t0 = time.time()

    def _run(p: Path) -> tuple[Path, str]:
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
            return p, doc_id
        except Exception as e:
            return p, f"ERROR: {e}"

    with ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as ex:
        futures = [ex.submit(_run, p) for p in paths]
        for fut in as_completed(futures):
            p, result = fut.result()
            if result.startswith("ERROR:"):
                failures.append((p, result))
                print(f"{p}: {result}", file=sys.stderr)
            else:
                print(f"{p}: {result}")

    dt = time.time() - t0
    print(f"Done in {dt:.1f}s. ok={len(paths) - len(failures)} failed={len(failures)}")
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
