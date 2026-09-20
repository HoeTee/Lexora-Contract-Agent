# 审查问题与批注执行流程

## 1. 核心数据关系

- `issue` 表示一个独立审查问题。
- 一个 `issue` 最多包含一个 `role="edit"` 的 anchor，也可以包含多个 `role="evidence"` 的 anchor。
- `edit` 是 Word 批注的实际落点；`evidence` 只为当前问题提供对照依据，不写批注。
- 合同中确实不存在可定位原文时，允许使用 `anchors=[]` 表示汇总问题。
- 一个问题需要修改多个位置时，SubAgent 必须拆成多个 issue。

`defect_type` 不属于输出契约。问题性质由 criterion、check_point、reasoning 和用户可见的批注意见表达。

## 2. 通用执行顺序

1. SubAgent 根据当前 criterion、check points 和检索证据生成完整 JSON。
2. JSON 和 Pydantic Schema 校验输出结构。
3. 锚点修复器处理空白、标点和唯一错位 ID 等可以确定性修复的问题。
4. 锚点校验器检查原文归属、唯一定位及 edit 范围冲突。
5. 启用完整覆盖审计的路由检查 coverage decisions 与 issue/edit 的对应关系。
6. 路由约束校验器检查正文与证据区域、附件对照等通用输出契约。
7. Reflector 复核事实、范围、角色、锚点语义和修改意见。
8. Reflector 确认的问题交给 Summarizer 生成总结并判断语义重复。
9. DOCX 报告层解析 edit 的真实字符范围并写入批注。

模型每次重新生成完整结果后，都重新执行第 2 至第 6 步，再进入 Reflector。

## 3. 代码直接修复的锚点问题

只有能够从检索证据唯一证明的调整才直接修改：

- 空白、换行及全半角标点差异；
- anchor ID 错误，但 quoted text 只存在于一个证据 anchor；
- quoted text 包含完整证据文字，并且只能确定一个目标 anchor。

代码不猜测合同内容，也不静默删除无法验证的 anchor。

## 4. 要求模型重新生成的情况

- JSON 或 Schema 不合法；
- 一个 issue 包含多个 edit；
- anchor ID 或 quoted text 不属于已提供的检索证据；
- quoted text 在同一证据中匹配多次，无法唯一定位；
- 不同 edit 出现非完全相同的范围重叠；
- coverage decisions 与 issue/edit 关系不一致；
- 输出违反当前路由声明的通用约束；
- Reflector 判断事实、审查范围、edit/evidence 角色、锚点范围或修改建议存在语义错误。

修订请求携带具体错误、原 issue ID 和可用证据。未改变的原文缺陷应保持 issue ID，新增缺陷使用新 ID。

## 5. 重复与冲突

- 完全相同锚点不在 SubAgent Schema 层判重，因为同一文字可能存在不同问题。
- Summarizer 只有在锚点、placement、事实、风险和修改动作均相同时才合并 issue。
- DOCX 层只删除落在同一字符范围且批注文本也相同的最终批注。
- 同一或相邻范围上的不同批注意见必须保留。
- 非完全相同的 edit 范围发生重叠时属于锚点冲突，返回 SubAgent 重新选择最小连续原文。

## 6. 脱敏内容

程序不扫描 `***`、`XX` 或其他固定字符形态，也不按字符数量强制生成批注。审查要点和提示词要求 SubAgent 根据字段语义判断脱敏、不完整或模板内容，Reflector 负责语义复核。

因此，脱敏召回取决于路由提供的证据和模型判断，不存在星号正则兜底。

## 7. 模型失败和任务终态

模型超时、鉴权失败或修订失败时，任务状态及原始模型错误按运行时错误处理流程记录。若无法形成可用审查结果，结果文件仍使用上传的源合同生成；`failed` 和 `cancelled` 不阻止 `/result` 返回源文件。
