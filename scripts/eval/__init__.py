# -*- coding: utf-8 -*-
"""eval —— 离线检索与数据集校验脚手架

评测是离线工具，不属于运行时应用，因此放在 `scripts/eval/` 而非 `app/`：

    metrics.py                  Recall@K / MRR / NDCG@K 纯函数 + 聚合 + 发版门禁
    validate_datasets.py        商品目录与品类知识自检
    retrieval_dense_rerank.py   线上检索链路的基线／精排对照

`retrieval_dense_rerank.py` 复用生产 `CatalogSearchUseCase`，接 Embedding 与
向量索引，可用 `--rerank on|off` 做单变量对照；不改 `.env`、`settings.py`、
`composition.py`。端到端质量由 `scripts/eval_regression.py` 的 Rubric 评测负责，
两者互补。
"""
