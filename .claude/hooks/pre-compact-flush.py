"""PreCompact hook — spawn the background memory flush. Body lives in _flush_common."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _flush_common import spawn_flush  # noqa: E402


def main():
    spawn_flush(json.loads(sys.stdin.read()), hook_name="pre-compact-flush")


if __name__ == "__main__":
    main()
