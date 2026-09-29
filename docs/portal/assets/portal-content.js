// 面向合同审查服务使用者与部署人员的公开说明。
// 块类型: heading | para | list | table | code | callout
window.DOCS_PORTAL_CONTENT = {
  "overview": [
    {
      type: "callout",
      title: "服务用途",
      text: "Lexora 接收 DOCX 合同，创建异步审查任务，并输出带有审查批注的 DOCX 文件。",
    },
    { type: "heading", text: "可以完成的操作" },
    {
      type: "list",
      items: [
        "通过浏览器工作台上传合同并下载结果。",
        "通过异步 API 提交合同、查询状态、获取结果或取消尚未开始的任务。",
        "在状态响应中查看任务进度、审查项总览和全部审查项明细。",
        "使用任务 ID 关联一次提交、状态查询、结果下载和问题排查。",
      ],
    },
    { type: "heading", text: "输入与输出" },
    {
      type: "table",
      headers: ["类型", "说明"],
      rows: [
        ["合同文件", "必须是可正常打开的 DOCX 文件。"],
        ["附加审查要点", "可选 DOCX；只对本次任务生效。"],
        ["任务状态", "包含任务阶段、时间、quality 汇总和全部 criteria。"],
        ["审查结果", "完成后提供带有批注的 DOCX 文件。"],
      ],
    },
    { type: "heading", text: "一次任务的流程" },
    {
      type: "code",
      text: "准备 DOCX\n  → 提交任务并保存 task_id\n  → 查询任务状态\n  → 查看 quality 与 criteria\n  → 任务完成后下载结果",
    },
    {
      type: "callout",
      title: "使用边界",
      text: "审查结果用于辅助发现和定位合同问题，使用者仍需结合业务背景和正式审核要求确认最终意见。",
    },
  ],

  "quick-start": [
    { type: "para", text: "以下步骤适用于已经启动并完成访问配置的服务。" },
    { type: "heading", text: "使用 Web" },
    {
      type: "list",
      ordered: true,
      items: [
        "打开服务地址并登录。",
        "进入工作台，选择需要审查的 DOCX 合同。",
        "如需增加本次专用审查要点，再选择对应 DOCX。",
        "提交后等待页面显示任务进度。",
        "任务完成后下载批注版 DOCX，并在历史记录中保留任务关联。",
      ],
    },
    { type: "heading", text: "使用 API" },
    {
      type: "code",
      text: "curl.exe -X POST \"http://localhost:5000/api/review/jobs\" `\n  -H \"Authorization: <api_key>\" `\n  -F \"file=@C:\\contracts\\contract.docx\"",
    },
    { type: "para", text: "提交成功返回 HTTP 202。请立即保存响应中的 `task_id`。" },
    {
      type: "code",
      text: "curl.exe -X POST \"http://localhost:5000/api/review/jobs/status\" `\n  -H \"Authorization: <api_key>\" `\n  -H \"Content-Type: application/json\" `\n  -d '{ \"task_id\": \"<task_id>\" }'",
    },
    { type: "para", text: "当 `status` 进入终态后，根据 `output.ready` 判断结果是否可以取得。" },
    { type: "heading", text: "快速验收" },
    {
      type: "list",
      items: [
        "提交响应包含非空 task_id。",
        "状态响应包含 created_at、input、output、quality 和 criteria。",
        "criteria 展示本次任务的全部审查项。",
        "任务成功后能够下载并正常打开 DOCX。",
      ],
    },
  ],

  "web-guide": [
    { type: "heading", text: "登录与工作台" },
    { type: "para", text: "访问服务根地址后，使用管理员分配的账号登录。普通用户进入工作台完成合同上传和结果下载。" },
    { type: "heading", text: "提交合同" },
    {
      type: "list",
      items: [
        "合同文件必须为 `.docx`。",
        "附加审查要点为可选文件，不上传时仍会执行系统提供的固定审查项。",
        "上传完成后不要重复点击提交；页面会显示当前任务状态。",
        "合同内容较多时运行时间会增加，可以离开工作页后从历史记录重新进入。",
      ],
    },
    { type: "heading", text: "查看任务" },
    {
      type: "table",
      headers: ["页面信息", "用途"],
      rows: [
        ["任务状态", "判断任务正在等待、执行还是已经结束。"],
        ["创建和完成时间", "确认任务提交时间与实际完成时间。"],
        ["输入文件名", "确认当前记录对应的原始合同。"],
        ["下载入口", "取得本次任务可用的结果文件。"],
      ],
    },
    { type: "heading", text: "历史记录" },
    { type: "para", text: "历史页面用于重新定位已经提交的任务和下载入口。排查问题时应同时记录合同文件名、提交时间和 task_id。" },
    {
      type: "callout",
      title: "浏览器操作提示",
      text: "如果页面长时间没有更新，可以刷新页面重新读取任务状态。刷新不会重新提交合同。",
    },
  ],

  "status-guide": [
    { type: "heading", text: "任务级 status" },
    {
      type: "table",
      headers: ["状态", "含义"],
      rows: [
        ["pending", "任务已经建立，正在等待进入执行流程。"],
        ["queued", "任务正在等待可用执行位置。"],
        ["running", "任务正在执行。"],
        ["succeeded", "任务已经生成可交付结果。"],
        ["failed", "任务未能正常完成；读取 error 获取原因。"],
        ["cancelled", "任务在允许取消的阶段被取消。"],
      ],
    },
    { type: "heading", text: "审查项 execution_status" },
    {
      type: "table",
      headers: ["状态", "含义", "是否有有效 review_result"],
      rows: [
        ["not_started", "该审查项尚未开始。", "否"],
        ["running", "该审查项正在执行。", "否"],
        ["completed", "该审查项已经形成完整结果。", "是"],
        ["incomplete", "执行结束，但未形成完整确认结果。", "可能有部分结果"],
        ["error", "该审查项执行失败。", "否"],
      ],
    },
    { type: "heading", text: "quality 如何阅读" },
    {
      type: "list",
      items: [
        "`total_count`：本次任务的审查项总数。",
        "`completed_count`：已经完整完成的数量。",
        "`incomplete_count`：结果未完整确认的数量。",
        "`error_count`：执行失败的数量。",
        "`not_started_count` 与 `running_count`：用于观察任务当前进度。",
        "`incomplete_criteria` 与 `error_criteria`：对应审查项 ID 列表。",
      ],
    },
    {
      type: "callout",
      title: "不要只看 succeeded",
      text: "任务级 succeeded 表示结果文件已经生成。仍需同时查看 quality 和 criteria，确认是否存在 incomplete 或 error。",
    },
    { type: "heading", text: "示例" },
    {
      type: "code",
      text: "{\n  \"status\": \"succeeded\",\n  \"quality\": {\n    \"total_count\": 18,\n    \"completed_count\": 17,\n    \"incomplete_count\": 1,\n    \"error_count\": 0,\n    \"incomplete_criteria\": [\"H11\"],\n    \"error_criteria\": []\n  }\n}",
    },
  ],

  "result-guide": [
    { type: "heading", text: "结果文件" },
    { type: "para", text: "正常完成的任务会提供批注版 DOCX。文件名在状态响应的 `output.result_filename` 中展示。" },
    {
      type: "list",
      items: [
        "`output.ready=true` 表示当前终态任务已有可取得文件。",
        "`status=succeeded` 时取得的是审查结果文件。",
        "任务失败或取消时，可取得文件可能只是原始合同的保留副本，不能视为已完成审查。",
        "下载后应确认文件可以正常打开，并核对批注是否与本次任务对应。",
      ],
    },
    { type: "heading", text: "批注阅读" },
    {
      type: "list",
      items: [
        "先阅读文档中的总体审查结论，再逐条查看具体批注。",
        "结合 quality 确认是否有未完整或失败的审查项。",
        "批注是辅助审查意见，不替代业务人员或法律人员的最终判断。",
      ],
    },
    { type: "heading", text: "结果留存" },
    { type: "para", text: "建议将原合同、task_id、状态响应和下载结果作为同一次任务留存，避免后续无法判断结果对应的输入版本。" },
  ],

  "api-guide": [
    { type: "callout", title: "认证方式", text: "所有公开任务接口都使用 `Authorization` 请求头。密钥由部署管理员提供，不要写入 URL、日志或提交到代码仓库。" },
    { type: "heading", text: "1. 提交任务" },
    { type: "code", text: "POST /api/review/jobs\nAuthorization: <api_key>\nContent-Type: multipart/form-data\n\nfile=<contract.docx>\ncriteria_file=<optional.docx>" },
    { type: "para", text: "成功时返回 HTTP 202，以及 `task_id`、`status` 和 `message`。`criteria_file` 为可选的本次附加审查要点。" },
    { type: "heading", text: "2. 查询状态" },
    { type: "code", text: "POST /api/review/jobs/status\nAuthorization: <api_key>\nContent-Type: application/json\n\n{ \"task_id\": \"20260928-220442-d471\" }" },
    {
      type: "para",
      text: "响应保留 task_id、status、created_at、started_at、finished_at、message、error、meta_fields、input、output、quality 和 criteria。",
    },
    { type: "heading", text: "3. 获取结果" },
    { type: "code", text: "POST /api/review/jobs/result\nAuthorization: <api_key>\nContent-Type: application/json\n\n{\n  \"task_id\": \"20260928-220442-d471\",\n  \"output_type\": \"file\"\n}" },
    { type: "para", text: "`output_type` 支持 `file` 和 `url`。省略时使用服务端配置的默认方式。" },
    { type: "heading", text: "4. 取消任务" },
    { type: "code", text: "POST /api/review/jobs/<task_id>/cancel\nAuthorization: <api_key>" },
    { type: "para", text: "只有尚未开始执行的任务可以取消。已经 running 或进入终态的任务会返回冲突响应。" },
    { type: "heading", text: "调用建议" },
    {
      type: "list",
      items: [
        "提交后持久化保存 task_id，不要依赖进程内临时变量。",
        "状态查询使用合理间隔，避免高频轮询。",
        "只在 output.ready 为 true 时请求结果。",
        "HTTP 成功只代表接口调用成功；业务完成情况以 status、quality 和 criteria 为准。",
      ],
    },
  ],

  "deployment": [
    { type: "heading", text: "环境边界" },
    {
      type: "table",
      headers: ["位置", "需要具备的内容"],
      rows: [
        ["镜像构建机", "项目源码、Docker/BuildKit 和项目要求的本地构建工具。"],
        ["远端部署宿主机", "生产镜像或镜像 TAR、Compose 文件、`.env`、`config.yaml`、`profiles/` 和 `data/`。"],
        ["运行容器", "由 Compose 启动；长期数据写入宿主机挂载目录。"],
      ],
    },
    { type: "heading", text: "构建镜像" },
    { type: "para", text: "在具备完整构建环境的项目根目录执行官方构建脚本：" },
    { type: "code", text: ".\\protect\\build.ps1" },
    { type: "para", text: "构建成功后生成与 Compose 配置一致的 `lexora:latest`。构建脚本失败时不要继续发布旧镜像。" },
    { type: "heading", text: "通过 TAR 交付" },
    { type: "code", text: "docker save -o lexora.tar lexora:latest" },
    { type: "para", text: "将 TAR 和部署配置复制到远端宿主机，在部署目录执行：" },
    { type: "code", text: "docker load -i lexora.tar\ndocker compose up -d --force-recreate" },
    { type: "heading", text: "通过镜像仓库部署" },
    { type: "code", text: "docker compose pull\ndocker compose up -d --force-recreate" },
    { type: "heading", text: "启动后检查" },
    {
      type: "code",
      text: "docker compose ps\ndocker compose logs --tail=100 lexora\ncurl.exe http://127.0.0.1:5000/health",
    },
    {
      type: "list",
      items: [
        "确认 API 服务和任务执行服务均处于运行状态。",
        "确认健康检查成功。",
        "确认 `.env` 与 `config.yaml` 来自本次部署目录。",
        "确认 `profiles/` 和 `data/` 是宿主机持久化目录。",
        "代码变更后必须重新构建并替换镜像，仅重启旧镜像不会生效。",
      ],
    },
  ],

  "troubleshooting": [
    { type: "callout", title: "先记录 task_id", text: "没有 task_id 时很难把 API 响应、任务状态和容器日志关联到同一次审查。" },
    { type: "heading", text: "任务一直 pending" },
    {
      type: "list",
      items: [
        "查询状态，确认 created_at 是否持续不变且 started_at 仍为空。",
        "在部署宿主机执行 `docker compose ps`，确认 API、队列服务和任务执行服务均在运行。",
        "查看任务执行服务日志，确认是否收到该 task_id。",
        "如果所有任务都停留在 pending，优先排查任务执行服务和队列连接。",
      ],
    },
    { type: "heading", text: "任务 running 很久" },
    {
      type: "list",
      items: [
        "读取 criteria，确认哪些审查项处于 running。",
        "比较 started_at 和当前时间，确认是否只是合同较大或任务较多。",
        "按 task_id 查看容器日志，确认是否仍有持续进度。",
        "不要仅因为耗时较长就重复提交同一合同。",
      ],
    },
    { type: "heading", text: "出现 incomplete" },
    { type: "para", text: "`incomplete` 表示该审查项没有形成完整确认结果。任务仍可能生成文件，但使用者需要根据 `incomplete_criteria` 识别审查缺口。" },
    { type: "heading", text: "出现 error" },
    { type: "para", text: "查看对应 criteria 项的 `error`，再根据 task_id 查找该任务的服务日志。部分审查项 error 时，任务仍可能提供其他已完成审查项的结果。" },
    { type: "heading", text: "任务 failed" },
    {
      type: "list",
      items: [
        "读取任务级 error.code、error.message 和 error.component。",
        "确认输入 DOCX 可以正常打开。",
        "确认部署配置和外部服务连接正常。",
        "保留 task_id、提交时间和相关容器日志后再重试。",
      ],
    },
    { type: "heading", text: "无法下载结果" },
    {
      type: "list",
      items: [
        "确认任务已经进入终态。",
        "确认 `output.ready=true`。",
        "确认请求使用了与提交任务相同的 Authorization 身份。",
        "如果 output_type 为 file，客户端需要把响应体保存为 DOCX 文件。",
      ],
    },
    { type: "heading", text: "部署环境日志命令" },
    { type: "code", text: "docker compose logs --since=30m lexora\ndocker compose logs --since=30m review-worker-1 review-worker-2 review-worker-3" },
    { type: "para", text: "这些命令应在远端部署宿主机的 Compose 目录执行，不是在本地开发机或未挂载数据的临时容器中执行。" },
  ],
};
