"""用当前实际检索链路做基线／候选对照：基线 embedding_only，候选额外开启 rerank。

线上链路是 `embedding_rerank → embedding_only → keyword_2gram` 三级降级，
当前 `RERANKER_MODE=disabled`，实际走 `embedding_only`。本脚本直接复用生产
同一套 `CatalogSearchUseCase`，接入 EmbeddingClient + 向量索引 + 可选
reranker，跑的就是线上这条链路。

不改动生产配置：`.env`、`settings.py`、`composition.py` 均不动。候选模式通过
`dataclasses.replace` 在内存里把 `reranker_mode` 换成 http，进程结束即失效，
Harness 历史结果不受影响。本地 Qdrant 目录被后端进程锁定时，改用
`.eval-tmp/qdrant` 的独立副本（`data_dir` 只在本次进程内替换）。

用法（项目根目录）：
    python -m scripts.eval.retrieval_dense_rerank \
        --role baseline --rerank off --split all \
        --output eval/verification/retrieval-standard/dense-baseline-20261125

    python -m scripts.eval.retrieval_dense_rerank \
        --role candidate --rerank on --split all \
        --output eval/verification/retrieval-standard/dense-rerank-20261125
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
from app.infrastructure.rerank.factory import create_reranker
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
    # x 模式保护已有证据，避免先检查后覆盖的竞争窗口。
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _catalog_path() -> Path:
    packaged = ROOT / "catalog/catalog-v3.jsonl"
    return packaged if packaged.is_file() else ROOT / "data/catalog-v3.jsonl"


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _public_endpoint(url: str | None) -> str | None:
    """只保留 scheme+host，避免把路径或凭据写进报告。"""
    if not url:
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else None


def _source_fingerprint() -> dict:
    names = ("app/application/usecases/catalog_search.py", "app/application/tools/product_search_tool.py",
             "app/domain/catalog/product.py", "app/domain/catalog/product_search_spec.py",
             "app/domain/catalog/product_attributes.py", "app/infrastructure/settings.py", "app/composition.py",
             "app/infrastructure/retrieval/bm25.py", "app/infrastructure/vector/qdrant_product_index.py",
             "app/infrastructure/embedding/openai_embedding_client.py", "app/infrastructure/rerank/http_reranker.py",
             "app/infrastructure/rerank/factory.py", "app/infrastructure/persistence/seed_products.py", "uv.lock")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def _evaluator_fingerprint() -> dict:
    names = ("app/application/retrieval_evaluation.py", "scripts/eval/retrieval_dense_rerank.py",
             "eval/retrieval/standard-v1.json")
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return {"sha256": _json_hash(entries), "files": entries}


def freeze_definition(*, dataset: Path, split: str) -> dict:
    catalog = _catalog_path()
    records, all_cases = _rows(catalog), _rows(dataset)
    selected = [case for case in all_cases if split == "all" or case.get("split") == split]
    if not selected or len({case["id"] for case in selected}) != len(selected):
        raise ValueError("选集为空或用例 ID 重复")
    source, evaluator = _source_fingerprint(), _evaluator_fingerprint()
    now = datetime.now(timezone.utc).isoformat()
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
            "embedding_identity": _json_hash({k: getattr(load_settings(require_llm_key=False), k)
                                              for k in ("embedding_model", "embedding_dim", "embedding_version")}),
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
    selected = [c for c in all_cases if args.split == "all" or c.get("split") == args.split]
    if not selected:
        raise SystemExit("选集为空")
    products = [_product_from_record(row) for row in records]
    by_id = {p.product_id: p for p in products}
    repo = InMemoryProductRepository(products)

    settings = load_settings(require_llm_key=True)
    embedder = OpenAIEmbeddingClient(settings)
    # 生产后端可能正持有 data/qdrant 的进程锁，评测改用独立副本。
    index = build_product_index(replace(settings, data_dir=ROOT / ".eval-tmp"))
    bootstrap: dict = {}
    if not await bootstrap_product_index(repo, embedder, index, report=bootstrap):
        raise SystemExit(f"向量建库失败：{bootstrap}")

    rerank_requested = args.rerank == "on"
    reranker = None
    if rerank_requested:
        reranker = create_reranker(replace(settings, reranker_mode="http"))
        if reranker is None:
            raise SystemExit("已请求 rerank，但 RERANKER_BASE_URL 为空，无法构造精排客户端")

    usecase = CatalogSearchUseCase(
        repo, embedder=embedder, vector_index=index, reranker=reranker,
        hybrid_enabled=False,  # 混合检索始终关闭，不参与本轮对照
        recall_candidates=settings.recall_candidates,
    )

    config_safe = {
        "variant": "dense" + ("_rerank" if rerank_requested else "_only"),
        "hybrid_recall_enabled": False,
        "reranker_mode": "http" if rerank_requested else "disabled",
        "reranker_model": settings.reranker_model if rerank_requested else None,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "recall_candidates": settings.recall_candidates,
        "k": 5,
        "external_services": True,
        "embedding_base_url": _public_endpoint(settings.embedding_base_url),
        "reranker_base_url": _public_endpoint(settings.reranker_base_url) if rerank_requested else None,
    }
    provenance = {**frozen["provenance"], "config_sha256": _json_hash(config_safe)}

    report = {
        "id": args.output.name,
        "label": args.label or ("当前检索主链（走 Embedding，无精排）" if not rerank_requested
                                else "当前检索主链 + 精排"),
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
                      ("total_products", "reused_products", "embedded_products", "written_products", "complete")},
        "note": ("真实调用 Embedding 与向量索引，不经 Agent、不调语言模型。"
                 + ("开启 HTTP 精排，与基线仅差 reranker_mode。" if rerank_requested
                    else "精排关闭，与线上 RERANKER_MODE=disabled 一致。")),
    }

    strategies: list[str] = []
    rerank_errors = 0
    for case in selected:
        started = time.perf_counter()
        try:
            result = await usecase.execute(_build_spec(case))
            row = evaluate_case(case, result["hits"], by_id)
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            row["recall_strategy"] = result.get("recall_strategy")
            row["rerank_status"] = result.get("rerank_status")
            if str(result.get("rerank_status")) in {"failed", "error", "timeout"}:
                rerank_errors += 1
            strategies.append(str(result.get("recall_strategy")))
            report["cases"].append(row)
        except Exception as error:  # 保留原始失败，不静默吞掉
            report["status"] = "failed"
            report["failure"] = {"case_id": case["id"], "error_type": type(error).__name__,
                                 "error": str(error)[:300]}
            report["cases"].append({"id": case["id"], "error": type(error).__name__})
            break

    report["summary"] = aggregate_cases([c for c in report["cases"] if c.get("ndcg_at_5") is not None
                                         or c.get("empty_ok") is not None])
    report["observed_strategies"] = sorted(set(strategies))
    report["rerank_error_count"] = rerank_errors
    if rerank_requested and rerank_errors:
        # 静默降级会伪装成"精排没用"，必须显式记录。
        report["status"] = "degraded"
        report["degraded_reason"] = f"{rerank_errors} 条记录显示精排失败，结果等同未精排"

    _write_new(args.output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="当前检索链路基线／精排候选对照")
    parser.add_argument("--role", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--rerank", choices=("on", "off"), required=True)
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "summary": report.get("summary", {}),
                      "strategies": report.get("observed_strategies"),
                      "rerank_errors": report.get("rerank_error_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
