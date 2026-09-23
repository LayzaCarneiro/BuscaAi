from __future__ import annotations

import argparse
import json

from userintent.llm import IntentExtractor


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrai UserIntent via LLM")
    parser.add_argument("text", help="Mensagem do usuário")
    args = parser.parse_args()

    intent = IntentExtractor().extract(args.text)
    print(json.dumps(intent.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
