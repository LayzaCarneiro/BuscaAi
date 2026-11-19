from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

from userintent.llm import IntentExtractor


def main() -> None:
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="Extrai UserIntent via LLM")
    parser.add_argument("text", help="Mensagem do usuário")
    args = parser.parse_args()

    intent = IntentExtractor().extract(args.text)
    print(json.dumps(intent.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
