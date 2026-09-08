# 审查质量回归

代码契约与测试范围见 [审查质量设计](../../docs/REVIEW_QUALITY.md)。

```powershell
python -m unittest discover -s tests/review_quality -v
python -m unittest discover -s tests/criteria_formal_recall -p "test_*.py"
```

本目录保存回归测试、验收清单及对照报告。正式 API 任务继续使用 data 用户分区，不放入测试目录；DOCX 与完整调用日志从任务目录获取。临时 DOCX 写入测试通过标准临时目录执行，不改动合同原件。
