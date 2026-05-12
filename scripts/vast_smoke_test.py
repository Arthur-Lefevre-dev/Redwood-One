#!/usr/bin/env python3
"""
CLI smoke test for Vast.ai (search offers, create nvidia-smi test instance, destroy).

Run from repository root with VAST_API_KEY set (and optional .env):

  set VAST_API_KEY=your_key
  python scripts/vast_smoke_test.py search
  python scripts/vast_smoke_test.py create --offer-id 12345678
  python scripts/vast_smoke_test.py destroy --instance-id 4242

Comments in English; user-facing script messages can stay minimal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _bootstrap_env() -> None:
    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from dotenv import load_dotenv

        for name in (".env", os.path.join("docker", ".env")):
            p = os.path.join(root, name)
            if os.path.isfile(p):
                load_dotenv(p)
    except ImportError:
        pass


def main() -> int:
    _bootstrap_env()

    p = argparse.ArgumentParser(description="Vast.ai smoke test (Redwood)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search on-demand GPU offers")
    sp.add_argument(
        "--gpu",
        help="Comma-separated GPU names (default: from VAST_DEFAULT_GPU_NAMES or RTX 3060,RTX 4060); see VAST_USABLE_GPU_NAMES + /vast/offers?gpu_tier=usable",
        default=None,
    )
    sp.add_argument("--limit", type=int, default=8)

    sp2 = sub.add_parser("create", help="Create SSH test instance (runs nvidia-smi on start)")
    sp2.add_argument("--offer-id", type=int, required=True)
    sp2.add_argument("--disk", type=int, default=24)
    sp2.add_argument(
        "--image",
        default="nvidia/cuda:12.3.1-base-ubuntu22.04",
        help="Docker image on Vast",
    )

    sp3 = sub.add_parser("destroy", help="Destroy instance (same id as new_contract from create)")
    sp3.add_argument("--instance-id", type=int, required=True)

    args = p.parse_args()

    from core import vast_ai

    if not (os.environ.get("VAST_API_KEY") or "").strip():
        # pydantic-settings loads from .env
        from config import get_settings

        if not (get_settings().VAST_API_KEY or "").strip():
            print("Missing VAST_API_KEY (env or .env).", file=sys.stderr)
            return 2

    try:
        if args.cmd == "search":
            names = (
                [x.strip() for x in (args.gpu or "").split(",") if x.strip()]
                if (args.gpu or "").strip()
                else vast_ai.default_gpu_name_list()
            )
            rows = vast_ai.search_offers(names, limit=args.limit)
            print(json.dumps({"offers": rows, "count": len(rows)}, indent=2, ensure_ascii=False))
            return 0

        if args.cmd == "create":
            raw = vast_ai.create_instance(
                args.offer_id,
                image=args.image,
                disk_gb=args.disk,
                runtype="ssh_direct",
                label="redwood-vast-cli-test",
                onstart="nvidia-smi || true",
            )
            print(json.dumps(raw, indent=2, ensure_ascii=False))
            iid = raw.get("new_contract") if isinstance(raw, dict) else None
            if iid is not None:
                print(f"\nInstance id: {iid}\nDestroy: python scripts/vast_smoke_test.py destroy --instance-id {iid}", file=sys.stderr)
            return 0

        if args.cmd == "destroy":
            out = vast_ai.destroy_instance(args.instance_id)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
