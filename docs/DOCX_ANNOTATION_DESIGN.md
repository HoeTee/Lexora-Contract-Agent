# DOCX 批注与修订处理说明

本文档说明合同审查中 DOCX 文本清理、`quoted_text` 定位、原合同批注保留和 AI 批注写入的实现逻辑。

## 直接结论

系统现在区分两份 DOCX 语义：

- 给 subagent 审查的文本：使用 `clean_docx()` 生成，接受修订并清除原批注。
- 最终输出的批注版 DOCX：以用户上传的原始 DOCX 为基础，保留原批注和原修订，再追加 AI 批注和黄色高亮。

保留范围包括所有已有人工批注、历史普通 AI 批注和历史 AI 审查总结，不按作者或内容删除、替换。重复上传已审查文件时，旧总结与本次新增总结会共存；“每次生成一条总结”不表示输出文件只能有一条历史总结。

这样做的目的，是让 subagent 看到的文本和程序定位 `quoted_text` 时使用的临时匹配文本一致，同时不破坏客户上传合同中已有的批注和修订痕迹。

## 审查结果从哪里来

批注生成阶段同时使用两类最终结构化结果：

- `presentation_results`：由 `build_review_presentation()` 对 Orchestrator 原始 `results` 做确定性去重、锚点选择和分流后生成，只保留写入原文的规范化问题。
- `final_review`：只包含 Summarizer 返回的 `overall_comment`、`priority_comments`，以及程序追加的 `summary_issue_comments`；三者共同生成文档开头总览批注。

因此，`results` 不是 SummaryAgent 的输出。真实数据流是：

```text
SubAgentOutput
  -> Orchestrator 汇总成 raw results
  -> build_review_presentation 生成 presentation_results、summarizer_results、summary_issue_comments
  -> SummaryAgent 读取 summarizer_results
  -> LLM 只输出 overall_comment、priority_comments
  -> 程序把 summary_issue_comments 追加到 final_review
  -> DOCX 批注生成器使用 presentation_results 和 final_review
```

其中：

- 每个原始 issue 都有稳定 `issue_key`，必须进入正文批注或 `summary_issue_comments`；程序检测到漏项会直接报错。
- 没有有效锚点、合同整体期限问题，以及条款断号或标题顺序问题进入总结；该判断不依赖 Reviewer 返回的锚点数量，不借正常标题或相邻段落强行放置批注。
- 合同框架缺失多项必备标题时，即使 Reviewer 返回了少量正常标题作为定位证据，也进入总结并一次列明全部缺失标题。
- 写入前按 `xml_anchor_type + xml_anchor_id + quoted_text + 修改动作` 去重；同一 XML 范围由多个审查要点返回同一修改时只生成一次批注，并在 `source_issue_keys` 保留来源。
- “错别字/语病/重复”等文字质量要点命中同一 XML 范围时合并；关系型问题中带有“作为修正基准/用于对比”等说明的正确附件标题锚点不写批注，只保留正文错误引用。
- 当前 `check_point` 给出“直接写明”的固定引号文字时，程序原样复制该文字，并只保留一个批注落点；不会从同一 criterion 的其他检查点串入错误文字。
- SummaryAgent 不再返回 `issue_decisions`，不能删除问题、改写锚点或决定展示位置。
- 整体仍只调用一次 SummaryAgent，不增加新的 LLM 调用。

## 审查文本如何生成

实现位置：

- `tools/document/file_cleaner.py`

`clean_docx()` 用于生成给解析器和 subagent 使用的临时 DOCX。它不会作为最终批注版 DOCX 的输出基底。

清理规则：

- 普通 `w:t` 文本保留。
- `w:ins` / `w:moveTo` 中的文本保留，相当于接受插入。
- `w:del` / `w:moveFrom` 中的文本删除，相当于接受删除。
- `commentRangeStart`、`commentRangeEnd`、`commentReference` 删除。
- `word/comments.xml` 和相关 comments part 删除。
- 修订跟踪设置从 settings 中移除，避免清理后的审查文本继续显示修订状态。

输出结果是“接受修订、移除批注后的合同文本”，供后续解析和审查使用。

## quoted_text 如何映射回原合同

实现位置：

- `tools/document/reporting/docx_report.py`

最终批注版 DOCX 不再使用 `clean_docx()` 的结果作为基底，而是直接复制用户上传的原始合同：

```python
shutil.copy2(contract_path, output_path)
```

程序随后在这个原始 DOCX 上构造临时匹配文本。

核心结构：

- `CharRef`：临时匹配文本中的一个字符，对应原 DOCX 中的一个 `Run` 和该 run 内的字符 offset。
- `ParagraphTextView`：一个段落的临时匹配文本，以及每个字符到原始 run 的 `char_map`。
- `TextMatch`：`quoted_text` 在 `ParagraphTextView.text` 中命中的起止字符位置和实际命中文本。
- `RunRange`：命中的 `quoted_text` 在某个原始 run 内的起止 offset。
- `TextAnchor`：一次 `quoted_text` 命中后的最终定位结果，包含段落、匹配文本和原始 run ranges。

效果链路：

```text
ParagraphTextView
  -> TextMatch
  -> RunRange
  -> TextAnchor
  -> Word 批注 XML
```

匹配流程：

1. 遍历原始 DOCX 的段落。
2. 对每个段落构造 `ParagraphTextView`。
3. 构造时使用和 `clean_docx()` 一致的接受修订语义：
   - 保留普通文本和 `w:ins` 文本。
   - 忽略 `w:del` / `w:moveFrom` 文本。
   - 不读取 `comments.xml` 中的批注正文。
4. 将段落内可审查文本拼接成一个临时字符串。
5. 用 subagent 返回的 `quoted_text` 在这个临时字符串里匹配。
6. 匹配成功后生成 `TextMatch`，记录命中的 `start_index`、`end_index` 和 `matched_text`。
7. 通过 `ParagraphTextView.char_map` 找回原始 DOCX 中对应的 run 和 offset。
8. 再把连续的 run + offset 折叠成一个或多个 `RunRange`。
9. 最终生成 `TextAnchor`，供后续高亮和插入批注范围使用。

因此，`quoted_text` 不是直接去原始 XML 中硬搜，而是先在“接受修订后的临时段落文本”中命中，再映射回原始合同的真实 run 位置。

## 跨段 quoted_text 匹配

`quoted_text` 可以跨相邻段落，但必须仍然是合同原文中连续出现的一段文本。

当前跨段匹配不是只支持两段，而是通用的连续段落匹配：

1. 先为每个可审查段落生成 `ParagraphTextView`。
2. 从任意一个段落开始，把该段和后续相邻段落的 normalized text 连续拼接成 `combined_text`。
3. 在 `combined_text` 中查找 normalized 后的 `quoted_text`。
4. 如果命中，再把命中范围映射回第一个实际覆盖到的 `ParagraphTextView`。
5. 生成 `TextAnchor` 后，只对这个可定位片段插入批注和高亮。

因此，只要 `quoted_text` 是连续跨相邻段落的文本，跨 2 段、3 段或更多段都使用同一套逻辑，不需要按段数新增代码。

不支持的情况：

- `quoted_text` 使用 `...`、`……` 等省略号把两个不相邻片段拼成一个引用。
- `quoted_text` 混合正文和附件，或混合不同附件中的不连续证据。
- `quoted_text` 是模型概括后的句子，而不是合同原文连续片段。

这类内容不应由 DOCX 锚定器强行猜测落点。正确处理方式是让 SubAgent 输出可连续匹配的主落点文本；如需多个不连续证据，应在结构上拆成多个证据字段，而不是塞进一个 `quoted_text`。

## AI 批注如何写入

实现位置：

- `tools/document/reporting/docx_report.py`

写入规则：

- 原始合同已有的 comments part 不删除。
- 新 AI 批注 id 从现有最大 comment id 后继续递增，避免覆盖原批注。
- 总览批注作者为 `AI 审查总结`，正文标题使用 `总体审查结论：` 和 `优先修改建议：`。
- 总览批注中 `总体审查结论` 和 `优先修改建议` 之间保留一个空白段落。
- 逐条问题批注作者为 `AI 条款审查`，正文先写入 `风险等级：高/中/低`，再写入修改建议。
- 当 `.env` 中 `DOCX_COMMENT_INCLUDE_CRITERION=True` 时，逐条问题批注末尾会追加该 issue 所属的 `审查要点`；关闭时不追加。这个开关不影响文档开头的总览批注。
- 命中的文本会被拆分到精确 run 边界。
- 命中 run 添加 `w:highlight w:val="yellow"`。
- 程序插入新的 `commentRangeStart`、`commentRangeEnd` 和 `commentReference`。
- 原合同已有修订结构保留，AI 批注和黄色高亮叠加到命中位置上。
- 索引阶段会把 `body_content_end` 的真实 XML 锚点传入写入器；“正文结尾”类问题只能使用该锚点，不能回退到附件或整个 DOCX 的最后一段。

高亮代码逻辑：

1. `_generate_docx_with_comments()` 读取每个 issue 的 `quoted_text`。
2. `_find_text_range_anchor_in_xml_anchor()` 先按 `xml_anchor_type/xml_anchor_id` 锁定 DOCX 范围，再把 `quoted_text` 首次出现位置定位成 `TextAnchor`。
3. 成功定位后，`comments_data` 中保存 `TextAnchor`、风险等级和批注正文。
4. `_add_comments_to_doc()` 写入 `comments.xml` 中的新 `w:comment`。
5. 如果 anchor 是 `TextAnchor`，调用 `_add_comment_markers_to_text_range()`。
6. `_add_comment_markers_to_text_range()` 根据 `RunRange` 拆分原始 run，并对命中的 run 调用 `_set_run_highlight()`。
7. `_set_run_highlight()` 在 run 的 `w:rPr` 下写入 `w:highlight w:val="yellow"`。
8. 最后再插入同 id 的 `commentRangeStart`、`commentRangeEnd` 和 `commentReference`。

如果 issue 没有生成 `TextAnchor`，它不会写入逐条 Word 批注，因此也不会出现没有黄色高亮的逐条 AI 问题批注。总览批注使用段落级 anchor，不属于逐条问题批注，不要求高亮。

批注时间使用北京时间：

```python
datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
```

生成的 `w:date` 会包含 `+08:00`，不再使用本地 naive time 加 `Z`。

## Word 批注 XML 完整性约束

DOCX 批注不是只在正文里插入一个标记。一个有效批注至少同时涉及：

- `word/document.xml` 中的 `commentRangeStart`、`commentRangeEnd`、`commentReference`。
- `word/comments.xml` 中对应的 `w:comment`。
- `word/_rels/document.xml.rels` 中指向标准 comments part 的 relationship。

必须满足以下约束：

- `commentRangeStart/@w:id`、`commentRangeEnd/@w:id`、`commentReference/@w:id` 与 `comments.xml` 中的 `w:comment/@w:id` 必须一致。
- `document.xml` 中出现的每个 `commentReference/@w:id`，都必须能在 `word/comments.xml` 中找到同 id 的 `w:comment`。
- 新增 AI 批注必须写入标准 comments part，relationship type 必须是 `http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments`。
- 不能用 `reltype` 字符串包含 `comments` 的方式查找 comments part，因为 Word 文档可能同时包含 `commentsExtended.xml`、`commentsIds.xml` 等扩展 part。
- 如果已有 `comments.xml`，新增批注后要同时更新 python-docx part 的 `_blob` 和 `_element`，避免保存时丢失新批注或破坏旧批注。

这几个约束来自 Open XML 的基本批注模型：Microsoft Open XML 文档说明 `commentReference` 会把 `Comments.xml` 中的 comment 连接到 `Document.xml` 的具体位置，且 comment、range start、range end、reference 的 id 需要一致；Open XML 标准资料也明确说明，如果 `commentReference` 找不到匹配 id 的 comment，则文档不符合规范。

参考资料：

- Microsoft Learn: Insert a comment into a word processing document  
  https://learn.microsoft.com/en-us/office/open-xml/word/how-to-insert-a-comment-into-a-word-processing-document
- Open XML `commentReference` 规范说明  
  https://c-rex.net/samples/ooxml/e1/part4/OOXML_P4_DOCX_commentReference_topic_ID0E4PNV.html
- ECMA-376 Office Open XML 标准入口  
  https://ecma-international.org/publications-and-standards/standards/ecma-376/

## fallback 批注

总览批注保留文档开头批注设计，不要求高亮。

缺少唯一问题原文的合同整体问题应由最终整理阶段标记为 `summary_only`。对于仍进入写入器的缺失类 issue：

- 如果 issue 的 `quoted_text` 成功命中，生成 `TextAnchor`，写入 Word 批注并高亮原文。
- “正文结尾”类问题只有在索引提供有效 `body_content_end` 锚点时才允许确定性落点；正文边界不可用时不回退到附件末段。
- 其他无 `quoted_text` 的历史兼容问题仍由现有缺失类落点逻辑处理；新输出应优先通过 `summary_only` 避免该路径。
- 未锚定 issue 仍会记录到同名 `*_annotation_events.json`，用于排查 SubAgent 输出和 quoted_text 质量。

因此，输出 DOCX 中除总览批注外，所有 AI 问题批注都必须有对应黄色高亮。

## 为什么有批注但没有高亮

是否高亮取决于批注锚点类型，而不是批注内容是否提到了具体条款。

| annotation event 状态 | 批注位置 | 是否高亮 | 含义 |
| --- | --- | --- | --- |
| `anchored` | `quoted_text` 命中的原文位置 | 是 | 已生成 `TextAnchor`，程序知道具体 run range。 |
| `missing_text_fallback` | 不写入 Word 批注 | 否 | issue 没有提供 `quoted_text`，没有可高亮文本。 |
| `unmatched` | 不写入 Word 批注 | 否 | `quoted_text` 非空但无法在合同原文中连续命中。 |

所以，看到批注内容里写了某个条款，并不等于程序已经定位到了这个条款。真正判断依据是同名 `*_annotation_events.json`：

- `status=anchored`：应当存在黄色高亮。
- `status=missing_text_fallback` 或 `status=unmatched`：不会生成逐条 Word 批注。
- `matched_text`：程序实际命中的原文。
- `paragraph_path`、`start_char`、`end_char`：程序插入批注和高亮的定位信息。

如果 Word 中出现“批注连接到了具体条款但没有黄色高亮”，应优先检查该批注是不是原合同已有批注，或者检查当前输出 DOCX 的 `document.xml` 中对应 run 是否存在 `w:highlight w:val="yellow"`。除总览外，AI 新增且成功锚定的逐条问题批注应同时具备批注范围和黄色高亮。

## 测试覆盖

实现位置：

- `tests/test_docx_report_annotations.py`

当前测试覆盖：

- 输入 DOCX 带已有 `comments.xml` 时，输出仍保留原批注。
- 即使文档同时存在 `commentsExtended.xml`，AI 批注也必须写入标准 `comments.xml`。
- 新 AI 批注从已有 comment id 后追加。
- 输入 DOCX 带 `w:ins` 和 `w:del` 时，最终输出仍保留这些原始修订结构。
- `quoted_text` 可以按接受修订后的文本命中。
- 连续跨相邻段落的 `quoted_text` 可以被锚定到实际原文片段。
- AI 命中文本被黄色高亮，批注正文包含风险等级。
- 未锚定 issue 不写入 Word 批注，只记录到 annotation events。
- 新批注时间包含 `+08:00`。
- `clean_docx()` 输出接受修订后的审查文本，并移除原批注 part。

## 审查输出质量契约更新

见[审查质量与批注规则](REVIEW_QUALITY.md)：新增 edit/evidence 锚点用途、逐字段批注、以附件为基准修改正文、反思待复核状态及标准版本摘要。docxindex 路由和召回算法不变。新代码须随镜像部署，不能只更新提示词。
