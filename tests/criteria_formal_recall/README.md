# criteria-formal 人工召回测试

这个目录用于对 `resources/criteria/criteria-formal.docx` 的 18 个审查要点进行人工评测。
当前只制作 D01 样本，确认 XML 局部修改方式正确后再扩展到 D02-D10。

## 第一阶段：制作测试集

在项目根目录执行：

```powershell
python tests/criteria_formal_recall/prepare_cases.py --overwrite
```

输入来自 `C:\Users\lenovo\Downloads\合同样例`。脚本不修改源文件，只在 `cases/` 生成
`D01.docx`，并生成18行 `expected_cases_D01.csv` 和逐文字节点的 `xml_changes_D01.csv`。

生成器只替换 `word/document.xml` 中现有 `<w:t>` 节点的文字，不使用 Word SaveAs 或
`python-docx` 保存，不增删 XML 节点，也不修改其他 DOCX 部件。

先人工打开 D01，并对照两个 CSV 检查改造和 XML 边界是否正确。D01 确认后再生成其余9份。

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
