# criteria-formal 人工召回测试

该目录使用10份不同合同测试 `resources/criteria/criteria-formal.docx` 的18个审查要点，
共180个审查用例。

## 生成测试合同

```powershell
python tests/criteria_formal_recall/prepare_cases.py --overwrite
```

32份合同的完整E2E基准使用“合同样例”25份和“模板合同”7份；保留原有D01-D10映射，
其余文件按目录和文件名稳定排序为D11-D32：

```powershell
python tests/criteria_formal_recall/prepare_cases.py --case-set 32 --overwrite
```

输出为 `cases_32/D01.docx` 至 `cases_32/D32.docx`，审查基准和XML改动审计分别为
`expected_cases_32.csv`（32×18=576条）与 `xml_changes_32.csv`。生成器要求两个源目录
合计恰好32份DOCX，数量变化时直接失败，避免样本集合静默漂移。

输入来自 `C:\Users\lenovo\Downloads\合同样例`，输出为 `cases/D01.docx` 至
`cases/D10.docx`。生成器只替换 `word/document.xml` 中已有 `<w:t>` 节点的文字，
不使用 Word SaveAs 或 `python-docx` 保存，不增删 XML 节点，其他 DOCX 部件必须逐字节一致。
生成后还会重新建立 DocxIndex，确认正文对比证据位于 `body`、附件期限和附件名称位于
`attachments`；任何一项不满足都会终止生成，不能写入期望表冒充阳性样本。

生成的审查基准和XML改动审计分别为：

- `expected_cases_10.csv`：10份合同 × 18个审查要点。
- `xml_changes_10.csv`：每个被修改文字节点的原文、改后文字和修改原因。

## 运行真实API审查

在 Windows 上，`run_cases.py` 会在评测进程存活期间请求系统保持唤醒，并在退出时恢复默认电源行为，避免休眠时间被错误计入端到端耗时。

小于 5 分钟的性能门禁使用 `config.example.yaml` 的工作流预算：审查要点并发 5、全局 LLM 并发 5、最多 3 轮反思、单次模型调用 30 秒、SDK 最多重试 1 次，且审查要点不做整段重跑。并发限制用于避免模型服务的突发限流；外部模型持续不可用时任务应快速失败，不能用超时后的长重试换取表面成功。

```powershell
python tests/criteria_formal_recall/run_cases.py --concurrency 1 --task-timeout 300
```

需要只补跑某份合同时使用：

```powershell
python tests/criteria_formal_recall/run_cases.py --contract-id D03
```

脚本明确上传 `criteria-formal.docx`，带批注合同和运行数据写入
`results/<run_id>/`。运行时 `data/api/<task_id>/` 只用于读取正常API任务状态和结构化结果，
测试集和测试报告不会写入项目 `data/`。

`tasks.json` 对每份合同记录 `started_at`、`finished_at`、`duration_seconds` 和
`within_5_minutes`。端到端时间从提交 API 前开始，到下载带批注 DOCX 并读取结构化输出后结束；
默认 300 秒硬超时。合同层建议顺序运行，合同内部18个审查要点仍按服务配置并发，避免跨合同争抢
模型配额污染单合同耗时。

在运行完整 API 前，可验证真实 stdio MCP 建索引链路：

```powershell
python tests/criteria_formal_recall/smoke_mcp_docxindex.py `
  tests/criteria_formal_recall/cases/D01.docx
```

## 评测口径

- 最终召回以带批注DOCX中实际可见的正确批注为准，后端结构化结果只用于定位丢失阶段。
- 批注准确度先核对对应审查要点，再核对合同事实和批注锚点。
- 同一问题的重复批注不算识别错误，也不会增加召回数量；需要时单独统计冗余率。
- 原合同已有批注和 `AI 审查总结` 不进入批注准确度分母。

多个运行批次可通过 `evaluate_results.py` 合并。评估器会先扣除输入合同已有批注，
再把最终可见的 `AI 条款审查` 批注映射回后端审查要点，并输出：
有原文锚点的批注使用 `anchors[].comment_text` 映射；条款缺失类的文首/文尾确定性批注使用
`issue_comment` 映射，因此两种真实可见批注采用同一准确率分母。

- `recall_failures.csv`：最终没有准确可见批注的审查用例及失败阶段。
  同时保留后端 issue 数量、后端引用原文、后端批注，以及错误可见批注的原文、内容和判断理由；
  `backend_evidence_status` 区分“未识别无原文”“已识别且有 anchor 原文”和
  “已识别但无 anchor 原文”；`review` 阶段未识别的用例对应证据字段为空数组。
- `incorrect_comments.csv`：违反“审查要点、合同事实、锚点”任一条件的批注。
- `annotated/`：按 `D01_已审查.docx` 至 `D10_已审查.docx` 汇总最终带批注合同。
- `metrics_by_contract.csv`：每份合同的端到端时间、召回率和准确率。
- `metrics_by_criterion.csv`：每个审查要点跨10份合同的召回率和准确率。
- `metrics_summary.csv` 和 `metrics_report.md`：总体阈值结论；默认要求每份合同小于300秒、
  总体召回率不低于85%、总体准确率不低于90%。

示例：

```powershell
python tests/criteria_formal_recall/evaluate_results.py `
  --run-dir tests/criteria_formal_recall/results/<run_id> `
  --output-dir tests/criteria_formal_recall/results/<evaluation> `
  --fail-on-threshold
```

## 按日志诊断失败

`diagnose_failures.py` 联合读取每个任务的 `trace.json`、SubAgent 完整 conversation、
`review_outputs.json` 和输入 DOCX，区分“模型首轮漏判”“Reflector 覆盖问题”以及
“锚点为空、锚点范围内原文不匹配、写回后不可见”等原因。例如：

```powershell
python tests/criteria_formal_recall/diagnose_failures.py `
  --run-dir tests/criteria_formal_recall/results/<run_1> `
  --run-dir tests/criteria_formal_recall/results/<run_2> `
  --output-dir tests/criteria_formal_recall/results/<evaluation>
```

输出为 `review_failure_diagnosis.csv` 和 `docx_write_failure_diagnosis.csv`。
新任务的逐条写回详情另存于任务自己的日志目录：API 为
`data/api/<task_id>/logs/annotation_events.json`，Web 租户为
`data/web/<tenant_id>/<task_id>/logs/annotation_events.json`。测试脚本只读取，不向 `data/` 写测试报告。
