"""CLI entrypoint: ``python -m merchantbench_adapter``."""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merchantbench_adapter",
        description=(
            "Reconstructed Hermes adapter for MerchantBench. Talks to the env "
            "via the public SDK and drives a persistent Hermes AIAgent."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=os.environ.get("MERCHANTBENCH_RUN_ID"),
        required=not bool(os.environ.get("MERCHANTBENCH_RUN_ID")),
        help="MerchantBench run id (or MERCHANTBENCH_RUN_ID)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MERCHANTBENCH_BASE_URL", "http://127.0.0.1:5050"),
        help="MerchantBench env base URL",
    )
    parser.add_argument(
        "--agent-id",
        default=os.environ.get("MERCHANTBENCH_AGENT_ID", "agent_0"),
        help="Agent id inside the run (default: agent_0)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MODEL_NAME"),
        help="OpenRouter / OpenAI-compatible model id",
    )
    parser.add_argument(
        "--max-hops-per-step",
        type=int,
        default=int(os.environ.get("MERCHANTBENCH_MAX_HOPS", "30")),
        help="Max Hermes tool/LLM iterations per decision window",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Stop once simulation step reaches this horizon (hours)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce adapter stderr logging (MerchantBench launcher passes this)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    if args.quiet and not args.verbose:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [merchantbench_adapter] %(message)s",
    )

    # Ensure the Hermes checkout root is importable when launched as a module.
    hermes_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if hermes_root not in sys.path:
        sys.path.insert(0, hermes_root)

    from merchantbench_adapter.runtime import run_adapter

    return run_adapter(
        base_url=args.base_url,
        run_id=args.run_id,
        agent_id=args.agent_id,
        model=args.model,
        max_hops_per_step=args.max_hops_per_step,
        max_steps=args.max_steps,
        quiet=bool(args.quiet and not args.verbose),
    )


if __name__ == "__main__":
    raise SystemExit(main())
