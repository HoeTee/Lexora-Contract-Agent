# criteria-formal 人工召回测试

该目录使用10份不同合同测试 `resources/criteria/criteria-formal.docx` 的18个审查要点，
共180个审查用例。

## 生成测试合同

```powershell
python tests/criteria_formal_recall/prepare_cases.py --overwrite
```

输入来自 `C:\Users\lenovo\Downloads\合同样例`，输出为 `cases/D01.docx` 至
`cases/D10.docx`。生成器只替换 `word/document.xml` 中已有 `<w:t>` 节点的文字，
不使用 Word SaveAs 或 `python-docx` 保存，不增删 XML 节点，其他 DOCX 部件必须逐字节一致。

生成的审查基准和XML改动审计分别为：

- `expected_cases_10.csv`：10份合同 × 18个审查要点。
- `xml_changes_10.csv`：每个被修改文字节点的原文、改后文字和修改原因。

## 运行真实API审查

```powershell
python tests/criteria_formal_recall/run_cases.py --concurrency 1
```

需要只补跑某份合同时使用：

```powershell
python tests/criteria_formal_recall/run_cases.py --contract-id D03
```

脚本明确上传 `criteria-formal.docx`，带批注合同和运行数据写入
`results/<run_id>/`。运行时 `data/api/<task_id>/` 只用于读取正常API任务状态和结构化结果，
测试集和测试报告不会写入项目 `data/`。

## 评测口径

- 最终召回以带批注DOCX中实际可见的正确批注为准，后端结构化结果只用于定位丢失阶段。
- 批注准确度先核对对应审查要点，再核对合同事实和批注锚点。
- 同一问题的重复批注不算识别错误，也不会增加召回数量；需要时单独统计冗余率。
- 原合同已有批注和 `AI 审查总结` 不进入批注准确度分母。

多个运行批次可通过 `evaluate_results.py` 合并。评估器会先扣除输入合同已有批注，
再把最终可见的 `AI 条款审查` 批注映射回后端审查要点，并输出：

- `recall_failures.csv`：最终没有准确可见批注的审查用例及失败阶段。
  同时保留后端 issue 数量、后端引用原文、后端批注，以及错误可见批注的原文、内容和判断理由；
  `backend_evidence_status` 区分“未识别无原文”“已识别且有 anchor 原文”和
  “已识别但无 anchor 原文”；`review` 阶段未识别的用例对应证据字段为空数组。
- `incorrect_comments.csv`：违反“审查要点、合同事实、锚点”任一条件的批注。
- `annotated/`：按 `D01_已审查.docx` 至 `D10_已审查.docx` 汇总最终带批注合同。
