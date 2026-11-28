"""算法消融：字段加权（7 组）与相关性阈值（13 档）。

两组性质不同，用 `--experiment` 切换：

    field      字段加权 BM25。不接 Embedding，field_lexical_enabled=True 会让
               usecase 自动走混合路径，但向量侧为 None，最终只剩字段加权 BM25
               一路（strategy 记为 bm25）。纯本地计算，无外部调用。
    threshold  相关性阈值。接 Embedding 走线上主链，用 min_relevance_score 过滤
               低分商品；必须同时给出 relevance_score_kind，否则过滤被静默跳过。

字段加权的 preset：

    equal        七个字段等权，等价于「不加权」的字段版
    default      FIELD_WEIGHTED_BM25_DEFAULT_WEIGHTS（凭经验设的起点）
    title-only   只看标题
    title2       标题 ×2，其余 ×1
    title5       标题 ×5，其余 ×1
    title-cat    标题 + 类目
    title-brand  标题 + 品牌

用法（项目根目录）：
    python -m scripts.eval.retrieval_ablation --experiment field --preset equal \
        --role baseline --output eval/verification/retrieval-standard/ablate-field-equal-20261128

    python -m scripts.eval.retrieval_ablation --experiment threshold --min-score 0.3 \
        --role candidate --output eval/verification/retrieval-standard/ablate-thr-030-20261128
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
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
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.retrieval.bm25 import FIELD_WEIGHTED_BM25_DEFAULT_WEIGHTS
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.product_index_factory import build_product_index

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "eval/v2/product_retrieval.jsonl"
SPEC_KEYS = ("category", "ship_to", "target_currency", "price_max_major",
             "required_material_tags", "excluded_material_tags", "product_id", "sku_id")
FIELDS = ("title", "brand", "category", "description", "highlights", "material")

PRESETS: dict[str, dict[str, float]] = {
    "equal": {name: 1.0 for name in FIELDS},
    "default": dict(FIELD_WEIGHTED_BM25_DEFAULT_WEIGHTS) | {"model": 0.0},
    "title-only": {"title": 1.0},
    "title2": {"title": 2.0, "brand": 1.0, "category": 1.0, "description": 1.0,
               "highlights": 1.0, "material": 1.0},
    "title5": {"title": 5.0, "brand": 1.0, "category": 1.0, "description": 1.0,
               "highlights": 1.0, "material": 1.0},
    "title-cat": {"title": 1.0, "category": 1.0},
    "title-brand": {"title": 1.0, "brand": 1.0},
}


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
             "app/infrastructure/retrieval/bm25.py",
             "app/infrastructure/persistence/seed_products.py",
             "app/infrastructure/settings.py", "scripts/eval/retrieval_ablation.py")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def _evaluator_fingerprint() -> dict:
    names = ("app/application/retrieval_evaluation.py", "scripts/eval/retrieval_ablation.py",
             "eval/retrieval/standard-v1.json")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def freeze_definition(*, dataset: Path, split: str, embedding_identity: str) -> dict:
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
            "embedding_identity": embedding_identity,
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


def _embedding_identity(settings) -> str:
    return _json_hash({key: getattr(settings, key) for key in
                       ("embedding_model", "embedding_dim", "embedding_version")})


async def run(args: argparse.Namespace) -> dict:
    args.output.mkdir(parents=True, exist_ok=False)
    records, all_cases = _rows(_catalog_path()), _rows(args.dataset)
    selected = [c for c in all_cases if args.split == "all" or c.get("split") == args.split]
    if not selected:
        raise SystemExit("选集为空")
    products = [_product_from_record(row) for row in records]
    by_id = {p.product_id: p for p in products}
    repo = InMemoryProductRepository(products)
    settings = load_settings(require_llm_key=False)

    if args.experiment == "field":
        weights = PRESETS[args.preset] if args.preset else None
        if weights is None and args.weights:
            weights = {k: float(v) for k, v in json.loads(args.weights).items()}
        if not weights:
            raise SystemExit("字段加权实验必须给出 --preset 或 --weights")
        # 不接 Embedding：field_lexical_enabled 让 usecase 走混合路径，向量侧为 None，
        # 最终只剩字段加权 BM25 一路，strategy 记为 bm25。
        usecase = CatalogSearchUseCase(
            repo, embedder=None, vector_index=None, reranker=None,
            field_lexical_enabled=True, field_weights=weights,
            recall_candidates=32, bm25_tokenizer=None,
        )
        expected_strategy = "bm25"
        embedding_identity = "none:field_bm25"
        variant = f"field_{args.preset or 'custom'}"
        config_safe = {"experiment": "field", "preset": args.preset, "field_weights": weights,
                       "embedder": None, "vector_index": None, "reranker": None,
                       "recall_candidates": 32, "k": 5, "external_services": False}
        bootstrap_info = None
    else:
        embedder = OpenAIEmbeddingClient(load_settings(require_llm_key=True))
        index = build_product_index(replace(settings, data_dir=ROOT / ".eval-tmp"))
        bootstrap: dict = {}
        if not await bootstrap_product_index(repo, embedder, index, report=bootstrap):
            raise SystemExit(f"向量建库失败：{bootstrap}")
        usecase = CatalogSearchUseCase(
            repo, embedder=embedder, vector_index=index, reranker=None,
            hybrid_enabled=False, recall_candidates=32,
            min_relevance_score=args.min_score,
            relevance_score_kind=args.relevance_kind,
        )
        expected_strategy = "embedding_only"
        embedding_identity = _embedding_identity(load_settings(require_llm_key=False))
        variant = f"threshold_{int(round(args.min_score * 100)):03d}"
        config_safe = {"experiment": "threshold", "min_relevance_score": args.min_score,
                       "relevance_score_kind": args.relevance_kind,
                       "hybrid_recall_enabled": False, "reranker": None, "k": 5,
                       "external_services": True}
        bootstrap_info = {k: bootstrap.get(k) for k in
                          ("total_products", "reused_products", "embedded_products", "complete")}

    frozen = freeze_definition(dataset=args.dataset, split=args.split,
                               embedding_identity=embedding_identity)
    provenance = {**frozen["provenance"], "config_sha256": _json_hash(config_safe)}

    report = {
        "id": args.output.name,
        "label": args.label or (f"字段加权 {args.preset}" if args.experiment == "field"
                                else f"阈值 {args.min_score:.2f}"),
        "protocol": PROTOCOL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "scope": "diagnostic",
        "role": args.role,
        "standard_id": PROTOCOL,
        "variant": variant,
        "provenance": provenance,
        "summary": {},
        "cases": [],
        "evaluator": _evaluator_fingerprint(),
        "parameters_safe": config_safe,
        "source_files": _source_fingerprint()["files"],
        "bootstrap": bootstrap_info,
        "note": ("纯本地字段加权 BM25：无向量、无精排、无模型调用。" if args.experiment == "field"
                 else "线上主链 embedding_only + 相关性阈值过滤。"),
    }

    strategies: list[str] = []
    mismatches = 0
    filter_mismatches = 0
    removed_total = 0
    for case in selected:
        started = time.perf_counter()
        try:
            result = await usecase.execute(_build_spec(case))
            row = evaluate_case(case, result["hits"], by_id)
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            row["recall_strategy"] = result.get("recall_strategy")
            strategy = str(result.get("recall_strategy"))
            if strategy != expected_strategy:
                mismatches += 1
            strategies.append(strategy)
            diagnostics = result.get("retrieval_diagnostics") or {}
            if args.experiment == "threshold":
                # 阈值必须真的生效；kind 不匹配时 usecase 会静默跳过过滤。
                if not diagnostics.get("relevance_filter_applied"):
                    filter_mismatches += 1
                removed_total += int(diagnostics.get("low_relevance_removed") or 0)
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
    if args.experiment == "threshold":
        report["filter_mismatch_count"] = filter_mismatches
        report["low_relevance_removed_total"] = removed_total
    reasons = []
    if mismatches:
        reasons.append(f"{mismatches} 条记录策略不是 {expected_strategy}")
    if filter_mismatches:
        reasons.append(f"{filter_mismatches} 条记录阈值过滤未生效（score_kind 不匹配）")
    if reasons:
        report["status"] = "degraded"
        report["degraded_reason"] = "；".join(reasons)

    _write_new(args.output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="算法消融：字段加权与阈值")
    parser.add_argument("--experiment", choices=("field", "threshold"), required=True)
    parser.add_argument("--preset", choices=tuple(PRESETS), help="字段加权的预设名")
    parser.add_argument("--weights", help='自定义字段权重，如 {"title":2,"brand":1}')
    parser.add_argument("--min-score", type=float, help="阈值实验的最小相关分")
    parser.add_argument("--relevance-kind", default="embedding",
                        choices=("embedding", "keyword", "bm25", "field_bm25", "reranker"))
    parser.add_argument("--role", choices=("baseline", "candidate"), default="candidate")
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.experiment == "threshold" and args.min_score is None:
        parser.error("--experiment threshold 需要 --min-score")
    report = asyncio.run(run(args))
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "ndcg_at_5": report["summary"].get("ndcg_at_5"),
                      "recall_at_5": report["summary"].get("recall_at_5"),
                      "precision": report["summary"].get("returned_precision"),
                      "strategies": report.get("observed_strategies"),
                      "filter_mismatches": report.get("filter_mismatch_count"),
                      "removed": report.get("low_relevance_removed_total")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
