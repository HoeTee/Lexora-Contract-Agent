# criteria-formal 人工召回测试

这个目录用于对 `resources/criteria/criteria-formal.docx` 的 18 个审查要点进行人工评测。

## 第一阶段：制作测试集

在项目根目录执行：

```powershell
python tests/criteria_formal_recall/prepare_cases.py --overwrite
```

输入来自 `C:\Users\lenovo\Downloads\合同样例`。脚本不修改源文件，只在 `cases/` 生成
`D01.docx` 至 `D10.docx`，并生成 `expected_cases.csv`。每份合同包含 18 个一级预期问题，
总计 180 条。

先人工打开 `cases/` 中的合同，并对照 `expected_cases.csv` 检查改造是否正确。

## 第二阶段：运行审查

确认测试集后，先启动本地 API，再执行：

```powershell
python tests/criteria_formal_recall/run_cases.py
```

脚本明确上传 `criteria-formal.docx`，将带批注 DOCX 写入 `results/<run_id>/annotated/`，
并生成 `results/<run_id>/recall.csv`。

`recall.csv` 的自动召回只表示对应合同、对应审查要点是否输出了至少一个问题。
批注内容是否准确由人工打开输出 DOCX 判断，并可填写 CSV 中的 `manual_correct` 和
`manual_note`。
