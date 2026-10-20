# 核心代码学习页

打开 [index.html](index.html)，即可离线浏览组件和代码。也可在仓库根目录启动只服务本目录的本地 HTTP 服务：

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory docs/code-learning
```

浏览器访问 `http://127.0.0.1:8765/`。其他电脑可直接打开复制过去的 `index.html`；页面不依赖项目运行环境、CDN 或模型服务。

## 页面内容

- 架构地图：按入口、执行、Agent、工具、模型控制和部署定位组件。
- 组件与源码：职责、输入输出、边界说明、函数片段和完整文件快照。
- 调用链：逐步查看合同提交、入队、审查、生成报告和状态读取。
- 并发实验：修改教学参数，观察合同槽、要点槽和共享模型池的区别。
- 模型请求：切换 BGE / Qwen / embedding / LLM 参数示意，查看相关源码。
- 检查理解：通过选择题核对模型容量、重试、流式与镜像部署边界。

本页以 `contract_agent` 为准。所有真实代码均来自生成时的文件；每段代码附原行号、相对路径、绝对路径及文件摘要。绝对路径用于定位原工作区，复制到其他电脑后应结合相对路径寻找文件。

本页不读取真实 `.env`、`config.yaml`、身份资料、合同或运行日志。示例配置值不代表生产运行值。参数实验与请求示意不会修改配置或调用服务。

## 更新源码快照

`catalog.json` 维护讲解内容与源码引用；`explorer.html` 提供交互组件。生成脚本只解析文件，不导入项目业务模块。

先校验所有引用：

```powershell
python scripts/build_code_learning_page.py --check
```

重新生成需要 Python、Node.js，以及 `answer-me-with-html` 技能中的 `scripts/am.mjs`：

```powershell
python scripts/build_code_learning_page.py
```

如果渲染器不在默认的用户目录 `.agents/skills/answer-me-with-html/`，显式传入 `--renderer <am.mjs的完整路径>`。

修改源码导致行号变化时，应调整引用范围并重新生成。页面中的源码副本是学习资料，不会自动更新，也不应在其副本中修改业务实现。
