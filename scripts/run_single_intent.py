from __future__ import annotations

import json
import sys
from pathlib import Path

# Permite rodar: python scripts/test_single_intent.py
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from userintent.llm import IntentExtractor


def main() -> None:
    user_text = "Quero um notebook até R$ 4.000 para programar e jogar casualmente"
    intent = IntentExtractor().extract(user_text)
    print(json.dumps(intent.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
