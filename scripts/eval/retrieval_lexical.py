"""词法对照：keyword_2gram / BM25(原切分) / BM25(jieba 切分) 三路对比。

三者同属字面匹配，差别在切分方式与打分函数：

    keyword            相邻两字机械切分，碎片平权，命中数量即分数
    bm25 + 默认切分     同样按两字切分，但套 BM25 的 IDF 与长度归一化
    bm25 + jieba 切分   用中文词典切出真实词，再套 BM25

第三组用于回答「BM25 表现差是不是因为切词退化成 2gram」。三组都不接
Embedding，向量路恒为 None、融合权重自动降为 (1.0, 0.0)，只剩字面一路。

用法（项目根目录）：
    python -m scripts.eval.retrieval_lexical --variant keyword --role baseline \
        --split all --output eval/verification/retrieval-standard/lex-keyword-20261128

    python -m scripts.eval.retrieval_lexical --variant bm25 --tokenizer default \
        --role candidate --output eval/verification/retrieval-standard/lex-bm25-2gram-20261128

    python -m scripts.eval.retrieval_lexical --variant bm25 --tokenizer jieba \
        --role candidate --output eval/verification/retrieval-standard/lex-bm25-jieba-20261128
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from app.application.retrieval_evaluation import (
    PROTOCOL,
    aggregate_cases,
    evaluate_case,
    load_standard,
    standard_fingerprint,
)
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "eval/v2/product_retrieval.jsonl"
SPEC_KEYS = ("category", "ship_to", "target_currency", "price_max_major",
             "required_material_tags", "excluded_material_tags", "product_id", "sku_id")
EXPECTED_STRATEGY = {"keyword": "keyword_2gram", "bm25": "bm25"}


def jieba_tokenizer(text: str) -> list[str]:
    """中文词典切词；标点与空白丢弃，保留英文与数字原样。"""
    import jieba
    return [w for w in jieba.cut(text.casefold()) if w.strip()]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _catalog_path() -> Path:
    packaged = ROOT / "catalog/catalog-v3.jsonl"
    return packaged if packaged.is_file() else ROOT / "data/catalog-v3.jsonl"


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _source_fingerprint() -> dict:
    names = ("app/application/usecases/catalog_search.py",
             "app/domain/catalog/product.py",
             "app/domain/catalog/product_search_spec.py",
             "app/domain/catalog/product_attributes.py",
             "app/infrastructure/retrieval/bm25.py",
             "app/infrastructure/persistence/seed_products.py",
             "app/infrastructure/settings.py", "scripts/eval/retrieval_lexical.py")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def _evaluator_fingerprint() -> dict:
    names = ("app/application/retrieval_evaluation.py", "scripts/eval/retrieval_lexical.py",
             "eval/retrieval/standard-v1.json")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def freeze_definition(*, dataset: Path, split: str) -> dict:
    catalog = _catalog_path()
    records, all_cases = _rows(catalog), _rows(dataset)
    selected = [c for c in all_cases if split == "all" or c.get("split") == split]
    if not selected or len({c["id"] for c in selected}) != len(selected):
        raise ValueError("选集为空或用例 ID 重复")
    source, evaluator = _source_fingerprint(), _evaluator_fingerprint()
    try:
        git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                  text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        git_head = None
    return {
        "git_head": git_head,
        "provenance": {
            "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
            "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "selection_sha256": _json_hash(selected),
            "source_sha256": source["sha256"],
            "standard_sha256": standard_fingerprint(load_standard()),
            "evaluator_sha256": evaluator["sha256"],
            "embedding_identity": "none:lexical",
            "query_mode": "structured_spec", "k": 5, "split": split, "repetitions": 1,
            "catalog_count": len(records), "case_count": len(selected),
            "sku_count": sum(len(row["skus"]) for row in records),
        },
        "selection": {"case_ids": [c["id"] for c in selected], "split": split, "complete_split": True,
                      "dataset": dataset.relative_to(ROOT).as_posix(),
                      "catalog": catalog.relative_to(ROOT).as_posix()},
    }


def _build_spec(case: dict) -> ProductSearchSpec:
    return ProductSearchSpec(normalized_query=case["query"], top_k=5,
                             **{k: case[k] for k in SPEC_KEYS if k in case})


async def run(args: argparse.Namespace) -> dict:
    args.output.mkdir(parents=True, exist_ok=False)
    frozen = freeze_definition(dataset=args.dataset, split=args.split)
    records, all_cases = _rows(_catalog_path()), _rows(args.dataset)
    selected = [c for c in all_cases if args.split == "all" or c.get("split") == split]
    if not selected:
        raise SystemExit("选集为空")
    products = [_product_from_record(row) for row in records]
    by_id = {p.product_id: p for p in products}

    tokenizer = jieba_tokenizer if args.tokenizer == "jieba" else None
    usecase = CatalogSearchUseCase(
        InMemoryProductRepository(products),
        embedder=None, vector_index=None, reranker=None,
        hybrid_enabled=(args.variant == "bm25"),
        recall_candidates=32,
        bm25_tokenizer=tokenizer,
    )

    variant_label = args.variant if args.variant == "keyword" else f"bm25_{args.tokenizer}"
    config_safe = {
        "variant": variant_label,
        "hybrid_enabled": args.variant == "bm25",
        "bm25_tokenizer": args.tokenizer if args.variant == "bm25" else None,
        "embedder": None, "vector_index": None, "reranker": None,
        "recall_candidates": 32, "k": 5, "external_services": False,
    }
    provenance = {**frozen["provenance"], "config_sha256": _json_hash(config_safe)}

    report = {
        "id": args.output.name,
        "label": args.label or {
            "keyword": "Keyword 2gram 单路",
            "default": "BM25 单路（两字切分）",
            "jieba": "BM25 单路（jieba 分词）",
        }[args.tokenizer if args.variant == "bm25" else "keyword"],
        "protocol": PROTOCOL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "scope": "diagnostic",
        "role": args.role,
        "standard_id": PROTOCOL,
        "variant": variant_label,
        "provenance": provenance,
        "summary": {},
        "cases": [],
        "evaluator": _evaluator_fingerprint(),
        "parameters_safe": config_safe,
        "source_files": _source_fingerprint()["files"],
        "note": "纯本地词法计算：不调 Embedding、不调精排、不调语言模型；不产生外部调用。",
    }

    strategies: list[str] = []
    mismatches = 0
    for case in selected:
        started = time.perf_counter()
        try:
            result = await usecase.execute(_build_spec(case))
            row = evaluate_case(case, result["hits"], by_id)
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            row["recall_strategy"] = result.get("recall_strategy")
            if result.get("recall_strategy") != EXPECTED_STRATEGY[args.variant]:
                mismatches += 1
            strategies.append(str(result.get("recall_strategy")))
            report["cases"].append(row)
        except Exception as error:
            report["status"] = "failed"
            report["failure"] = {"case_id": case["id"], "error_type": type(error).__name__,
                                 "error": str(error)[:300]}
            report["cases"].append({"id": case["id"], "error": type(error).__name__})
            break

    report["summary"] = aggregate_cases([c for c in report["cases"]
                                         if c.get("ndcg_at_5") is not None or c.get("empty_ok") is not None])
    report["observed_strategies"] = sorted(set(strategies))
    report["strategy_mismatch_count"] = mismatches
    if mismatches:
        report["status"] = "degraded"
        report["degraded_reason"] = f"{mismatches} 条记录的召回策略不是 {EXPECTED_STRATEGY[args.variant]}"

    _write_new(args.output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="词法对照：keyword 2gram / BM25 两种切分")
    parser.add_argument("--variant", choices=("keyword", "bm25"), required=True)
    parser.add_argument("--tokenizer", choices=("default", "jieba"), default="default",
                        help="仅 bm25 生效：default 用模块自带两字切分，jieba 用中文词典")
    parser.add_argument("--role", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "summary": report.get("summary", {}),
                      "strategies": report.get("observed_strategies"),
                      "mismatches": report.get("strategy_mismatch_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
