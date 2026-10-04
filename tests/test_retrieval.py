"""Guards retrieval quality: fails if a change makes the search noticeably worse."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from run_eval import evaluate, load_questions  # noqa: E402
from rag import KnowledgeBase  # noqa: E402


def test_hybrid_retrieval_accuracy(tmp_path):
    kb = KnowledgeBase(ROOT / "data", vector_path=tmp_path)
    questions = load_questions(ROOT / "eval" / "questions.jsonl")
    bm25 = evaluate(kb, questions, "bm25")
    assert bm25["hit3"] >= 0.75
    if kb.dense_enabled:
        hybrid = evaluate(kb, questions, "hybrid")
        assert hybrid["hit3"] >= 0.80
        assert hybrid["mrr"] >= bm25["mrr"]
