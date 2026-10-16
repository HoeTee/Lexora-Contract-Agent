# Agent 提示词统一管理与字段契约修复（2026-10-16）

已将所有 agent 的系统提示、任务模板、格式示例、修复提示集中到 `agents/prompts.yaml`，由 `agents/prompts.py` 加载。旧 `agents/prompts/` 目录及其中 `cn_prompts.py`、`cn_prompts.yaml` 已移除。Planner、SubAgent、Reflector、Summarizer 不再各自维护这些静态文本；DocxIndex 工具提示词仍归工具模块管理。

首次模型请求现在携带完整输出格式。H01 明确要求 coverage_decisions 每条包含 xml_anchor_type、xml_anchor_id、decision、reason，不允许 anchor_id 替代。H18 明确成对证据字段、真正 JSON null 以及四种比较状态。汇总的系统提示与三字段输出示例对齐。公共修复提示按字段合并重复错误；29个记录产生的87项格式错误可归纳为三类，原始完整错误仍留在日志。

并发、模型60秒总预算、四次发送上限、要点总尝试2次、三个 worker 和 Redis DB0/DB2 均未改。Dockerfile、示例 Compose 和本地被忽略的 Compose 已迁移到 `/app/agents/prompts.yaml`；首次升级需要重建镜像，之后改 YAML 正文需要重启 API 和全部三个 worker。使用说明见 `docs/AGENT_PROMPTS.md`，部署文件来源与挂载步骤见 `docs/DEPLOYMENT.md`。

离线验证：最终版本 criteria_formal_recall 套件275项，265项通过、10项按既有条件跳过；新增8项覆盖真实schema示例、首轮携带格式、变量一次替换、无效配置和87项错误合并。Ruff检查通过。三发送语义测试显式固定测试额度，层级与路由额度测试按有效配置验证，避免本地四发送实验影响测试预期。

完整合同实测：

| 指标 | 结果 |
|---|---|
| 任务 | 20261016-202006-98b4 |
| 输入 | deploy/test_contract.docx，与此前合同一致 |
| 结果 | 18/18要点完成，无最终失败 |
| 总用时 | 367.766秒，约6分8秒 |
| H01 | attempt1，ISSUES_FOUND；首响应29条覆盖记录全部使用正确四字段 |
| H01模型发送 | 1次；无schema重发、无整要点重跑 |
| H01模型操作耗时 | 24.094534秒；其中请求23.669091秒，剩余35.905466秒 |
| H18 | attempt1，COMPLIANT；5个逻辑操作均首发送通过schema |
| 实际模型发送 | 124次，123次HTTP200，1次超时未取得HTTP状态 |
| 发生超时的要点 | H04初次请求超过60秒，整要点重跑后恢复 |
| Redis | 结束后三类模型占用、等待、截止记录均为0，无采样越限 |

H18中的业务修订是补齐p135/p158比较结论，随后经历反思；它们是新增业务操作，不是格式重发。正文末尾p163—p166附件目录仍未出现在H18初始上下文，本轮没有修改检索范围，不能宣称附件核查覆盖完整。

测试使用本地隔离Docker环境，依赖镜像为现有lexora:latest，工作区源码只读挂载；未构建或验证新的生产加密镜像。模型地址沿用本机配置中的阿里云域名端点，不是公司内网部署验证。测试项目 `contract-agent-prompts-20261016` 已清理，宿主机数据和结果保留。

完整实测结束后，将公共“未要求审计则空数组”说明明确限定为当前schema实际定义的字段，防止Planner/Reflector/Summarizer新增不属于自己的数组；H01四字段规则与示例保持不变。最终离线275项已在这条措辞澄清后重新通过，未再次提交完整模型任务。`metadata.json`分别记录实测与最终YAML哈希，保留验证边界。

证据目录：`C:\Users\lenovo\Desktop\contract_agent\deploy\prompt-verify-20261016-201959`。`summary.json`、`prompt-audit.json`、`public-status.json`、`metadata.json`、`unit-tests.txt`、`redis-final.json`、`service-logs.txt`提供汇总与原始证据；`report.docx`为已下载的审查报告。

修改前检查点提交：d5c10661。修改后提交保存源码、部署示例、测试与文档；本地config.yaml、docker-compose.yaml和deploy实验目录按原仓库忽略规则留在本机。未修改用户原有的其他工作区变更。本轮使用终端和并行代理，未调用skill或MCP工具。
