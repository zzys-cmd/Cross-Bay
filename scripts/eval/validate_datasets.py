# -*- coding: utf-8 -*-
"""标注集自检。

标注集是评测的基准。基准本身错了，后面所有指标都不可信，而且错法很隐蔽：
把一个「会被硬约束挡掉」的商品标成 relevant，不会报错，只会让 Recall 上限
悄悄低于 1，然后你对着一个永远达不到满分的指标反复调检索。

所以标注集在拿去评测前必须先过这一关。跑测脚本不强制依赖它，但改完标注就该跑：

    uv run python scripts/eval/validate_datasets.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.infrastructure.persistence.in_memory_repositories import (  # noqa: E402
    InMemoryProductRepository,
)
from scripts.eval.data_quality import (  # noqa: E402
    validate_catalog_distribution,
    validate_catalog_products,
    validate_catalog_raw_records,
)
from scripts.eval.knowledge_quality import (  # noqa: E402
    count_knowledge_chunks,
    load_knowledge_manifest,
    validate_knowledge_content,
    validate_knowledge_manifest,
)

_KNOWLEDGE_DIR = Path("knowledge")
_CATALOG_FIXTURE = Path("data") / "catalog-v2.jsonl"


def _load(path: Path) -> list[tuple[int, dict]]:
    cases = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.strip():
            cases.append((lineno, json.loads(raw)))
    return cases


async def validate_catalog_fixture() -> list[str]:
    """校验运行时商品数据，而不只校验 query 金标引用是否存在。"""
    products = await InMemoryProductRepository().list_all()
    problems = validate_catalog_products(products) + validate_catalog_distribution(products)
    try:
        records = _load(_CATALOG_FIXTURE)
        problems.extend(validate_catalog_raw_records([record for _, record in records]))
    except (OSError, json.JSONDecodeError) as err:
        problems.append(f"无法读取版本化商品数据：{err}")
    return problems


async def validate_knowledge_fixture() -> list[str]:
    """把来源/时效元数据与 chunk 规模纳入统一发版前校验。"""
    try:
        manifest = load_knowledge_manifest(_KNOWLEDGE_DIR)
    except ValueError as err:
        return [str(err)]
    problems = validate_knowledge_manifest(_KNOWLEDGE_DIR, manifest)
    problems.extend(validate_knowledge_content(_KNOWLEDGE_DIR))
    chunk_count = await count_knowledge_chunks(_KNOWLEDGE_DIR)
    if not 150 <= chunk_count <= 250:
        problems.append(f"知识 chunk 数 {chunk_count} 不在 [150, 250] 范围内")
    return problems



async def main() -> None:
    catalog_problems = await validate_catalog_fixture()
    knowledge_problems = await validate_knowledge_fixture()
    problems = catalog_problems + knowledge_problems
    if problems:
        print(f"发现 {len(problems)} 处问题：")
        for message in problems:
            print("  -", message)
        sys.exit(1)
    print("商品目录与品类知识自检通过")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
