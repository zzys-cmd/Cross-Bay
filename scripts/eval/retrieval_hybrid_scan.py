"""商品召回参数扫描：混合检索下 recall_candidates 与两路权重的组合对照。

线上默认是 `embedding_only`（hybrid 关闭）。本脚本把 hybrid 打开，扫描三组
候选数与三组融合权重，回答「混合检索能否超过纯向量的 0.910」。

扫描矩阵（共 5 组，其中 32+1:1 既是基线也是权重组的对照组）：

    A 组：权重 1:1，候选数 32 / 64 / 128
    B 组：候选数 32，权重 1:1 / 1.5:1 / 1:1.5

不改生产配置：`.env`、`settings.py`、`composition.py` 均不动，参数只在本进程内
传给 `CatalogSearchUseCase`。Qdrant 用 `.eval-tmp` 副本，避免与后端争锁。

用法（项目根目录）：
    python -m scripts.eval.retrieval_hybrid_scan --recall-candidates 32 \
        --lexical-weight 1.0 --vector-weight 1.0 --role baseline \
        --output eval/verification/retrieval-standard/hybrid-c32-w1-1-20261128
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
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.product_index_factory import build_product_index

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "eval/v2/product_retrieval.jsonl"
SPEC_KEYS = ("category", "ship_to", "target_currency", "price_max_major",
             "required_material_tags", "excluded_material_tags", "product_id", "sku_id")


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
             "app/infrastructure/vector/qdrant_product_index.py",
             "app/infrastructure/embedding/openai_embedding_client.py",
             "app/infrastructure/persistence/seed_products.py",
             "app/infrastructure/settings.py", "scripts/eval/retrieval_hybrid_scan.py")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def _evaluator_fingerprint() -> dict:
    names = ("app/application/retrieval_evaluation.py", "scripts/eval/retrieval_hybrid_scan.py",
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
    repo = InMemoryProductRepository(products)

    settings = load_settings(require_llm_key=True)
    embedder = OpenAIEmbeddingClient(settings)
    index = build_product_index(replace(settings, data_dir=ROOT / ".eval-tmp"))
    bootstrap: dict = {}
    if not await bootstrap_product_index(repo, embedder, index, report=bootstrap):
        raise SystemExit(f"向量建库失败：{bootstrap}")

    usecase = CatalogSearchUseCase(
        repo, embedder=embedder, vector_index=index, reranker=None,
        hybrid_enabled=True,                       # 本轮扫的就是混合检索
        hybrid_lexical_weight=args.lexical_weight,
        hybrid_vector_weight=args.vector_weight,
        recall_candidates=args.recall_candidates,
        bm25_tokenizer=None,                       # 用默认两字切分；jieba 已验证更差
    )

    config_safe = {
        "variant": f"hybrid_c{args.recall_candidates}_w{args.lexical_weight}_{args.vector_weight}",
        "hybrid_recall_enabled": True,
        "recall_candidates": args.recall_candidates,
        "hybrid_lexical_weight": args.lexical_weight,
        "hybrid_vector_weight": args.vector_weight,
        "reranker": None, "k": 5, "external_services": True,
    }
    provenance = {**frozen["provenance"], "config_sha256": _json_hash(config_safe)}

    report = {
        "id": args.output.name,
        "label": args.label or f"混合检索 candidates={args.recall_candidates} "
                                f"权重={args.lexical_weight}:{args.vector_weight}",
        "protocol": PROTOCOL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "scope": "diagnostic",
        "role": args.role,
        "standard_id": PROTOCOL,
        "variant": config_safe["variant"],
        "provenance": provenance,
        "summary": {},
        "cases": [],
        "evaluator": _evaluator_fingerprint(),
        "parameters_safe": config_safe,
        "source_files": _source_fingerprint()["files"],
        "bootstrap": {k: bootstrap.get(k) for k in
                      ("total_products", "reused_products", "embedded_products", "complete")},
        "note": "混合检索（向量 + BM25 本地 RRF 融合），不接精排、不调语言模型。",
    }

    strategies: list[str] = []
    mismatches = 0
    effective: dict = {}
    for case in selected:
        started = time.perf_counter()
        try:
            result = await usecase.execute(_build_spec(case))
            row = evaluate_case(case, result["hits"], by_id)
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            row["recall_strategy"] = result.get("recall_strategy")
            strategy = str(result.get("recall_strategy"))
            if strategy != "hybrid_only":
                mismatches += 1
            strategies.append(strategy)
            diagnostics = result.get("diagnostics") or {}
            if not effective and diagnostics:
                # 记录首条实际生效的参数，确认传参没被默认值覆盖。
                effective = {k: diagnostics.get(k) for k in
                             ("candidate_limit", "lexical_weight", "vector_weight")}
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
    report["effective_parameters"] = effective
    if mismatches:
        report["status"] = "degraded"
        report["degraded_reason"] = f"{mismatches} 条记录未走 hybrid_only（可能向量侧失败降级）"

    _write_new(args.output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="商品召回参数扫描（混合检索）")
    parser.add_argument("--recall-candidates", type=int, required=True, choices=range(8, 257))
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument("--vector-weight", type=float, default=1.0)
    parser.add_argument("--role", choices=("baseline", "candidate"), default="candidate")
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "summary": report.get("summary", {}),
                      "strategies": report.get("observed_strategies"),
                      "effective": report.get("effective_parameters")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
