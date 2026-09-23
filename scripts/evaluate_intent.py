from __future__ import annotations

import argparse
import json
from pathlib import Path

from userintent.evaluator import aggregate, evaluate_case
from userintent.llm import IntentExtractor
from userintent.schema import UserIntent


def load_cases(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def save_predictions(predictions_path: Path, predictions: list[dict]) -> None:
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with predictions_path.open("w", encoding="utf-8") as f:
        for row in predictions:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_predictions(path: Path) -> dict[int, UserIntent]:
    out: dict[int, UserIntent] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            out[int(row["case_id"])] = UserIntent.model_validate(row["prediction"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Avalia UserIntent em dataset JSONL")
    parser.add_argument("--dataset", default="data/intent_eval_50.jsonl")
    parser.add_argument(
        "--predictions",
        help="Arquivo de predições já salvo; evita chamadas à API",
    )
    parser.add_argument(
        "--save-predictions",
        default="data/predictions_gemini.jsonl",
        help="Onde salvar as predições geradas pela API",
    )
    args = parser.parse_args()

    dataset = list(load_cases(Path(args.dataset)))

    if args.predictions:
        predictions = load_predictions(Path(args.predictions))
    else:
        extractor = IntentExtractor()
        predictions: dict[int, UserIntent] = {}
        serialized: list[dict] = []

        total = len(dataset)
        for index, case in enumerate(dataset, start=1):
            print(f"[{index:02d}/{total:02d}] {case['input']}")
            try:
                pred = extractor.extract(case["input"])
            except Exception as exc:
                print(f"  ERRO: {exc}")
                raise

            predictions[case["case_id"]] = pred
            serialized.append(
                {
                    "case_id": case["case_id"],
                    "input": case["input"],
                    "prediction": pred.model_dump(mode="json"),
                }
            )

        if args.save_predictions:
            save_predictions(Path(args.save_predictions), serialized)
            print(f"\nPredições salvas em: {args.save_predictions}")

    results = []
    for case in dataset:
        pred = predictions[case["case_id"]]
        results.append(evaluate_case(pred, case["expected"], case["case_id"]))

    metrics = aggregate(results)
    print("\n=== MÉTRICAS ===")
    for name, value in metrics.items():
        if name == "cases":
            print(f"{name}: {int(value)}")
        else:
            print(f"{name}: {value:.4f}")

    failures = [r for r in results if not r.core_pass]
    print(f"\nCasos que falharam no core_pass: {len(failures)}/{len(results)}")
    for failure in failures:
        print(json.dumps(failure.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
