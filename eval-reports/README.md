# 评测报告归档

本目录保存 CI 上 `release` 档位评测的报告快照。完整原始数据（198 条 `rows/`、`attempts.jsonl`）在本地 `eval/verification/ci-release-*`，不入库；GitHub Actions 的 artifact 默认保留 90 天。

## 三次运行

| 日期 | 运行 ID | current | candidate | 失败数 | 门禁 | 目录 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-21 | `44896754096` | 97/99 | 99/99 | 2 | `BENEFIT_VERIFIED` | [20261121-current97](20261121-current97/) |
| 2026-11-21 | `44908921819` | 99/99 | 97/99 | 2 | `BLOCKED` | [20261121-candidate97](20261121-candidate97/) |
| 2026-11-22 | `44989461349` | 99/99 | 99/99 | 0 | `BENEFIT_VERIFIED` | [20261122-pass198](20261122-pass198/) |

共同条件：`deepseek-v4.1-flash`、`profile=release`、`RERANKER_MODE=disabled`、48 场景 × 2 策略 × 3 次重复 = 198 次调用。

## 怎么看

- **`summary.json`**：汇总指标。重点看 `gates.candidate.status`（门禁结论）、`strategies.*.passed`（通过次数）、`failure_count`
- **`report.html`**：下载后用浏览器打开，含完整面板、逐题答案与工具诊断

## 三次的差异

**11-21 第一次**：`current` 挂 2 条——`agent-long` 摘要候选两次含原文没有的金额（`ValueError`）、`h1-hold-blue` 被「当前库存……74」规则误判（答案实为「当前库存未知，不能沿用历史 74」）。门禁本身是 `BENEFIT_VERIFIED`，但当时退出码还带「任一条失败即红」，job 判红。

**11-21 第二次**：`candidate` 挂 2 条——`h1-hold-first` 把「第 2 件」答成第 3 件（`P28223` vs 标准 `P28222`）、`agent-long` 第 9 轮强制压缩返回 `noop` 被当成失败。后者是检查过严：未到压缩水位时摘要不变属正常，且 `candidate` 压缩更彻底才更容易触发。

**11-22**：两条修复后零失败。`agent-long` 改为 `summary_changed` 或 `status=noop` 均通过；发布档位退出码改为只看门禁。

## 最终指标（2026-11-22）

| 指标 | current | candidate |
| --- | --- | --- |
| 通过 | 99/99 | 99/99 |
| 成本 | ¥5.705 | ¥4.233 |
| 缓存命中率 | 45.9% | 67.7% |
| 普通轮 P95 | 6398ms | 6157ms |

成本降幅 25.8%，CI95 [21.1%, 31.6%]；成功率差 0，CI95 [0, 0]；离线契约 859 例通过。
