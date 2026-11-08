#!/usr/bin/env python3
"""Prearm or restore The Warriors' native world timestep through PINE."""

from __future__ import annotations

import argparse
import math

from freecam_probe import WorldTimeController
from pine_client import PineClient


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--scale",
        type=float,
        help="multiply the current native timestep by this value",
    )
    action.add_argument(
        "--restore-bits",
        type=lambda value: int(value, 0),
        help="restore this exact IEEE-754 timestep bit pattern",
    )
    parser.add_argument("--port", type=int, default=28011)
    args = parser.parse_args()

    client = PineClient(port=args.port)
    try:
        if args.restore_bits is not None:
            original = WorldTimeController.bits_to_float(
                args.restore_bits
            )
            if not math.isfinite(original) or original <= 0.0:
                raise ValueError(
                    "restored timestep must be finite and positive"
                )
            controller = WorldTimeController(client, 0.0)
            controller.original_bits = args.restore_bits
            restored = controller.disable()
            print(
                "WORLD_TIME_RESTORED="
                f"0x{args.restore_bits:08X} ({restored:.9g})"
            )
            return

        assert args.scale is not None
        controller = WorldTimeController(client, args.scale)
        original, target = controller.enable()
        assert controller.original_bits is not None
        assert controller.target_bits is not None
        print(f"WORLD_TIME_ORIGINAL_BITS=0x{controller.original_bits:08X}")
        print(f"WORLD_TIME_TARGET_BITS=0x{controller.target_bits:08X}")
        print(
            f"WORLD_TIME_VALUES={original:.9g}->{target:.9g}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
