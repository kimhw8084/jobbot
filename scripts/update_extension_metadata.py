from __future__ import annotations

import json
from pathlib import Path

from jobbot.extension_identity import expected_identity
from jobbot.version import PRODUCT_VERSION


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    value = expected_identity(ROOT)
    value.update({"product_version": PRODUCT_VERSION, "schema": 1})
    target = ROOT / "extension" / "build_meta.json"
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(target)
    print(value["runtime_digest"])


if __name__ == "__main__":
    main()
