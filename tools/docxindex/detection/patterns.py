import re

CN_NUM = "一二三四五六七八九十百零〇两"
MAIN_SECTION_RE = re.compile(rf"^第[{CN_NUM}0-9]+[条章]")
ATTACHMENT_PARENT_RE = re.compile(rf"^第[{CN_NUM}0-9]+[条章]\s*附件\s*$")
ATTACHMENT_RE = re.compile(rf"^附件\s*([{CN_NUM}0-9]+)(?:\s*$|[：:、\s])")
NAMED_ATTACHMENT_RE = re.compile(r"^附件\s*[：:]\s*《?.+")
ATTACHMENT_LIST_ITEM_RE = re.compile(r"^\d+[.、]\s*附件")
LEVEL2_RE = re.compile(rf"^[{CN_NUM}]+、")
LEVEL3_RE = re.compile(rf"^（[{CN_NUM}]+）")
TITLE_KEYWORD_RE = re.compile(r"(细则|标准|协议|说明书|承诺书|清单|要求|任务书|方案|需求|报告|函)$")
LABEL_KEYWORD_RE = re.compile(
    r"(目的|对象|分工|内容|方式|结果|其他|要求|标准|范围|期限|责任|义务|说明|证明|来源|包装|请假|考核|罚则|承诺|服务|电话)"
)
PLAIN_LABEL_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9（）()]{2,24}[。；;：:]$")
TAIL_MARKER_RE = re.compile(r"^（?(?:以下无正文|以下为合同签署栏)")
SIGNATURE_TABLE_RE = re.compile(r"甲方[\s\S]{0,500}(?:盖章|签字)[\s\S]{0,500}乙方|乙方[\s\S]{0,500}(?:盖章|签字)[\s\S]{0,500}甲方")
SIGNATURE_LINE_RE = re.compile(
    rf"^(?:第[{CN_NUM}0-9]+条\s*|[{CN_NUM}]+、\s*|\d+[.、]\s*)?"
    r"(?:(?:甲方|乙方)(?:（[^）]{0,20}）)?[：:].{0,80}(?:盖章|签字|法定代表人|授权代表)"
    r"|甲方[：:].{0,120}乙方[：:])"
)
