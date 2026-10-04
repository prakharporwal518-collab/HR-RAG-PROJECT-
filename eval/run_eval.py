"""Measure retrieval accuracy on a labelled set of question -> expected page(s).

    python eval/run_eval.py                 # compare bm25 / dense / hybrid
    python eval/run_eval.py --mode hybrid --show-misses
    python eval/run_eval.py --min-hit3 0.85 # exit 1 if accuracy drops (useful in CI)

Metrics (a hit = any retrieved chunk is on one of the expected pages):
    Hit@1  the very first passage is from the right page
    Hit@3  a right page is in the top 3 passages
    MRR    mean of 1/rank of the first right passage (1.0 = always first)
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from rag import KnowledgeBase  # noqa: E402

K = 5


def load_questions(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(kb: KnowledgeBase, questions: list[dict], mode: str) -> dict:
    hit1 = hit3 = rr = 0.0
    misses = []
    for q in questions:
        pages = [c.page for c in kb.search(q["question"], k=K, mode=mode)]
        rank = next((i for i, p in enumerate(pages, 1) if p in q["pages"]), None)
        hit1 += rank == 1
        hit3 += rank is not None and rank <= 3
        rr += 1 / rank if rank else 0
        if not rank or rank > 3:
            misses.append({"question": q["question"], "expected": q["pages"], "got": pages})
    n = len(questions)
    return {"mode": mode, "hit1": hit1 / n, "hit3": hit3 / n, "mrr": rr / n, "misses": misses}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.jsonl")
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--mode", choices=["bm25", "dense", "hybrid", "all"], default="all")
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--min-hit3", type=float, help="fail if hybrid (or --mode) Hit@3 is below this")
    args = ap.parse_args()

    questions = load_questions(args.questions)
    with tempfile.TemporaryDirectory() as tmp:  # fresh vector index so results are reproducible
        kb = KnowledgeBase(args.data, vector_path=Path(tmp))
        modes = ["bm25", "dense", "hybrid"] if args.mode == "all" else [args.mode]
        if not kb.dense_enabled:
            print("! Embeddings unavailable: only BM25 can be evaluated.")
            modes = ["bm25"]
        results = [evaluate(kb, questions, m) for m in modes]

    print(f"\n{len(questions)} questions, top-{K} retrieval\n")
    print(f"{'mode':<8} {'Hit@1':>7} {'Hit@3':>7} {'MRR':>7}")
    for r in results:
        print(f"{r['mode']:<8} {r['hit1']:>7.1%} {r['hit3']:>7.1%} {r['mrr']:>7.3f}")
    if args.show_misses:
        for r in results:
            print(f"\nMisses for {r['mode']} (right page not in top 3):")
            for m in r["misses"]:
                print(f"  - {m['question']}  expected {m['expected']}, got {m['got']}")

    if args.min_hit3 is not None:
        score = results[-1]["hit3"]
        if score < args.min_hit3:
            print(f"\nFAIL: {results[-1]['mode']} Hit@3 {score:.1%} < {args.min_hit3:.0%}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
