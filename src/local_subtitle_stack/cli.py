from __future__ import annotations

import argparse
from pathlib import Path

from .app import build_service
from .queue import QueueError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subtitle-stack")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="Queue one or more source videos.")
    enqueue.add_argument("sources", nargs="+")
    enqueue.add_argument("--profile", default="conservative")
    enqueue.add_argument("--glossary")
    enqueue.add_argument("--series")
    enqueue.add_argument("--context")
    enqueue.add_argument("--recursive", action="store_true")

    subparsers.add_parser("worker", help="Process queued jobs until the queue is empty.")
    subparsers.add_parser("status", help="Show queue status.")

    resume = subparsers.add_parser("resume", help="Resume a queued or failed job.")
    resume.add_argument("job_id")

    review = subparsers.add_parser("open-review", help="Open completed subtitle outputs in Subtitle Edit.")
    review.add_argument("job_id", nargs="?")

    output = subparsers.add_parser("open-output", help="Open the exported subtitle folder in Explorer.")
    output.add_argument("job_id", nargs="?")

    subparsers.add_parser("pause", help="Pause the queue after the current safe checkpoint.")
    subparsers.add_parser("unpause", help="Clear the pause flag.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = build_service()

    try:
        if args.command == "enqueue":
            glossary = Path(args.glossary) if args.glossary else None
            for source in args.sources:
                source_path = Path(source)
                if source_path.is_dir():
                    manifests, skipped = service.enqueue_folder(
                        folder=source_path,
                        profile=args.profile,
                        glossary=glossary,
                        series=args.series,
                        context=args.context,
                        recursive=args.recursive,
                    )
                    print(f"Queued {len(manifests)} videos from {source_path}")
                    if skipped:
                        print(f"Skipped {len(skipped)} already queued videos.")
                    for manifest in manifests:
                        print(f"Queued {manifest.source_name} as {manifest.job_id}")
                    continue
                manifest = service.enqueue(
                    source=source_path,
                    profile=args.profile,
                    glossary=glossary,
                    series=args.series,
                    context=args.context,
                )
                print(f"Queued {manifest.source_name} as {manifest.job_id}")
            return 0

        if args.command == "worker":
            service.run_until_empty()
            print("Queue complete.")
            return 0

        if args.command == "status":
            rows = service.status_rows()
            if not rows:
                print("No jobs found.")
                return 0
            for row in rows:
                print(
                    f"{row['job_id']}  {row['status']:<9}  {row['stage']:<18}  "
                    f"{row['state_dir']:<7}  {row['source']}"
                )
            return 0

        if args.command == "resume":
            manifest = service.resume(args.job_id)
            print(f"Resumed {manifest.job_id}")
            return 0

        if args.command == "open-review":
            paths = service.open_review(args.job_id)
            for path in paths:
                print(path)
            return 0

        if args.command == "open-output":
            print(service.open_output_folder(args.job_id))
            return 0

        if args.command == "pause":
            service.store.set_pause(True)
            print("Pause requested.")
            return 0

        if args.command == "unpause":
            service.store.set_pause(False)
            print("Pause cleared.")
            return 0
    except QueueError as exc:
        print(str(exc))
        return 1

    raise QueueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
