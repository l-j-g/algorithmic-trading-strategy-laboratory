"""Run Hermes one-shot mode with the prompt supplied over stdin.

Hermes' ``--oneshot`` option takes its prompt as an argv value. ATS prompts
contain request data and must stay off the process command line, so this small
bridge imports Hermes in its own runtime and passes stdin directly to the
one-shot API.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--reasoning")
    parser.add_argument("--toolsets")
    parser.add_argument("--skills")
    parser.add_argument("--usage-file")
    return parser


def _profile_home(profile: str) -> Path:
    if profile == "default":
        return Path.home() / ".hermes"
    return Path.home() / ".hermes" / "profiles" / profile


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.profile:
        os.environ["HERMES_HOME"] = str(_profile_home(args.profile))
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Hermes stdin bridge requires a non-empty prompt", file=sys.stderr)
        return 2

    from hermes_cli.oneshot import run_oneshot

    return int(run_oneshot(
        prompt,
        model=args.model,
        provider=args.provider,
        toolsets=args.toolsets,
        skills=args.skills,
        usage_file=args.usage_file,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
