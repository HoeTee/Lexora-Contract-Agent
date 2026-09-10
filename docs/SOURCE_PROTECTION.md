# 源码保护方案（AES-256-GCM + VMP 虚拟化）

## 当前正式架构

生产镜像采用两层保护：

1. `protect/pack.py` 将 `protect/modules.txt` 选中的 Python 源码编译为 Python 3.12 字节码、打包为 ZIP，再用 AES-256-GCM 加密为 `/app/code.bin`。
2. 密钥重建和 AES 解密逻辑位于 `libloader.so`，其中两个关键函数由 `VMProtectBegin` / `VMProtectEnd` 标记；VMP 将该共享库虚拟化为 `/app/libloader.vmp.so`。

Linux 下不使用“直接加壳 ELF 可执行文件”的路径。当前 VMP 版本对 ELF executable 的处理存在结构性问题；正式方案只对共享库加壳，再由明文小型 `/app/host` 使用 `dlopen` 加载。

```text
/app/host
  └─ dlopen /app/libloader.vmp.so
       ├─ 虚拟化函数：重建 AES 密钥
       ├─ 虚拟化函数：AES-256-GCM 解密 /app/code.bin
       ├─ 初始化嵌入式 Python 3.12
       ├─ 从内存 ZIP 注册 Python importer
       └─ 按 /app/host -m <module> [arguments...] 执行模块
```

最终镜像中的三个保护产物：

```text
/app/host                 # 明文启动器，只负责定位 .so、dlopen 和调用导出入口
/app/libloader.vmp.so     # VMP 虚拟化后的敏感 loader
/app/code.bin             # AES-256-GCM 密文代码包
```

最终镜像不包含项目 `.py`、明文项目 `.pyc`、未加壳 `libloader.so`、`key_data.h`、AES 构建密钥、`vmprotect_con`、VMP SDK 库或许可证文件。

## AES 加密层

输入：

- `protect/modules.txt` 指定范围内的项目 Python 源码。
- 通过 BuildKit secret `source_key` 提供的原始 32 字节 AES 密钥。

处理：

```text
.py
→ compile(..., optimize=2)
→ Python 3.12 .pyc payload
→ ZIP（manifest.json + modules/*.pyc）
→ AES-256-GCM
```

`code.bin` 格式：

```text
LEXORA1\0（8 字节 AAD/文件头）
+ nonce（12 字节）
+ ciphertext
+ GCM tag（16 字节）
```

`protect/pack.py` 同时生成临时 `key_data.h`，把密钥拆为随机 mask 与 XOR 分片；该头文件只进入 `libloader.so` 编译过程，随后随临时构建目录删除。

## VMP 虚拟化层

VMP 保护对象是未加壳的 `libloader.so`，不是 `code.bin`。源码标记位于 `protect/loader/src/crypto.cpp`：

```text
lexora_key_reconstruct
lexora_aes_decrypt
```

共享库使用隐藏符号编译，只显式导出：

```text
lexora_loader_main
```

`vmprotect_con` 将标记函数转换为 VM 字节码并输出 `libloader.vmp.so`。加壳后的共享库包含自身运行时，不需要在最终镜像中携带 `libVMProtectSDK64.so`。

## Docker 三阶段构建

正式入口是根目录 `Dockerfile`：

```text
protect-builder（python:3.12-slim）
  ├─ pack.py → code.bin + key_data.h
  ├─ VMP SDK 以只读 BuildKit 挂载提供
  └─ CMake → libloader.so + host

vmp-pack（ubuntu:24.04）
  ├─ vmprotect_con + SDK由BuildKit临时挂载提供
  └─ libloader.so → libloader.vmp.so

runtime（python:3.12-slim）
  └─ host + libloader.vmp.so + code.bin + 运行依赖和可读资源
```

加壳阶段必须使用 glibc 2.38 或更高版本；`python:3.12-slim` 的 Debian bookworm glibc 版本不足，因此不能合并 `protect-builder` 和 `vmp-pack`。

构建命令：

```powershell
.\protect\build.ps1
```

默认工具位置是仓库根目录 `vmp-tools/`。该目录仅存在于构建机本地，并被 `.gitignore` 和 `.dockerignore` 忽略；`protect/build.ps1` 将它作为 BuildKit named context `vmp_tools` 单独传入。需要覆盖时可传 `-VmpToolsDir`，也可设置 `VMP_TOOLS_DIR`。

脚本默认构建 `lexora:latest`，与根目录及 `deploy/` 的 Compose 配置一致；构建成功后还会用 `--network none` 启动临时 Python 容器，确认 `cl100k_base` 能从镜像内缓存加载。

`protect-builder` 仅以只读方式挂载 SDK；`vmp-pack` 使用临时可写挂载规范化 Windows 文件系统传入的执行权限，挂载内修改会在该条 `RUN` 完成后丢弃。VMP 工具不会被 `COPY` 到构建层，也不会进入最终镜像。

Git 仓库不保留 VMP 程序、SDK、动态库或许可证；仓库根目录的 `vmp-tools/` 是被忽略的本地构建输入。仓库保留 `protect/vmp/gen_lang.py` 和 `protect/vmp/patch_vmp_sources.sh`，用于对外部 VMP 源码树生成语言文件和应用 Linux 工具链兼容补丁；脚本不会自动下载或提交 VMP 程序。缺少完整 `vmp-tools/` 时只允许构建失败，不允许静默输出未保护镜像。

## 运行时流程

API 容器：

```text
/app/host -m uvicorn app:app ...
→ dlopen libloader.vmp.so
→ 重建密钥并验证/解密 code.bin
→ 注册内存 importer
→ 从受保护代码包导入 app
```

Celery worker 容器：

```text
/app/host -m celery -A task_queue.celery_app:celery_app worker ...
→ 使用同一套 .so 和 code.bin
```

管理 CLI 有两种等价的命令形式：

| 环境 | 示例 |
| --- | --- |
| 本地 `.venv` | `.\.venv\Scripts\python.exe scripts/manage_tenants.py list` |
| VMP 受保护容器 | `/app/host -m scripts.manage_tenants list` |

容器内的三个管理入口都使用同一套受保护运行时：

```text
/app/host -m scripts.manage_api_clients ...
/app/host -m scripts.manage_tenants ...
/app/host -m scripts.manage_users ...
```

三个管理脚本由 `protect/modules.txt` 选中并加密进 `code.bin`。最终镜像不存在 `/app/scripts/*.py`，所以不能使用 `.venv` 脚本形式；本地源码环境则不需要 `/app/host`。容器运行方式见 [部署说明](DEPLOYMENT.md)，完整参数见 [管理 CLI](MANAGEMENT_CLI.md)。

解密不会生成磁盘 `.pyc`，但解密后的 ZIP 字节会在 Python importer 的内存对象中存在，直到进程退出。

## 证据验证

构建必须同时验证：

- 加壳日志出现 `lexora_key_reconstruct` 和 `lexora_aes_decrypt` 两个 marker。
- 加壳日志出现 `Compilation completed`。
- `libloader.vmp.so` 是可加载的 ELF shared object。
- host 能通过 `dlsym` 找到 `lexora_loader_main`。

最终镜像验证：

```powershell
$image = "lexora:latest"

docker run --rm --entrypoint sh $image -c "test -x /app/host && test -s /app/libloader.vmp.so && test -s /app/code.bin"
docker run --rm --entrypoint find $image /app -type f -name "*.py"
docker run --rm --entrypoint find $image /app -type f -name "*.pyc"
docker run --rm --entrypoint sh $image -c "od -An -tx1 -N8 /app/code.bin"
docker run --rm --entrypoint sh $image -c "test ! -e /run/vmp-tools && test ! -e /app/vmprotect_con && test ! -e /app/libVMProtectSDK64.so && test ! -e /app/VMProtectLicense.ini && test ! -e /app/libloader.so"

foreach ($term in @("reconstruct_key", "aes_decrypt", "LEXORA1", "zipfile")) {
  docker run --rm --entrypoint grep $image -a -q $term /app/libloader.vmp.so
  if ($LASTEXITCODE -eq 0) { throw "Sensitive string remains in packed library: $term" }
}

$dependencies = docker run --rm --entrypoint ldd $image /app/libloader.vmp.so
if ($dependencies -match "VMProtectSDK") { throw "Packed library still depends on VMProtect SDK." }
```

应用验证：

```powershell
docker run --rm -p 8000:8000 `
  --env-file .env `
  -v "${PWD}\config.yaml:/app/config.yaml:ro" `
  lexora:latest

curl.exe -I http://127.0.0.1:8000/docs
```

## 安全边界

- **静态边界**：拿到镜像层后无法像普通 `.py`/`.pyc` 那样直接解译，需要先分析 VMP 虚拟机和 loader，再恢复 AES 密钥与代码包。
- **动态边界**：能够执行 `docker exec`、附加调试器、使用 `ptrace`/`LD_PRELOAD` 或读取进程内存的攻击者，仍可提取解密后的归档。
- **部署边界**：生产环境应使用最小化容器权限、限制宿主机 Docker 权限、避免向非受信人员开放容器 shell，并对调试和容器管理操作进行审计。
- **授权边界**：当前 VMP 工具来源存在授权风险，仅限已获合法授权的内部研究或保护场景；分发和商用前必须完成法律审核。

## 相关实现

- `Dockerfile`：三阶段生产镜像和仅构建期可见的 VMP bind mount。
- `protect/build.ps1`：AES 密钥和 VMP 工具目录校验、BuildKit 构建入口。
- `protect/vmp/gen_lang.py`：为外部 VMP 源码树生成语言 include 文件。
- `protect/vmp/patch_vmp_sources.sh`：为外部 VMP 源码树应用 Linux 编译兼容补丁。
- `protect/pack.py`：字节码归档和 AES-256-GCM 加密。
- `protect/loader/CMakeLists.txt`：`loader_so` 与 `host` 编译目标。
- `protect/loader/src/crypto.cpp`：密钥重建和 AES 解密 marker。
- `protect/loader/src/loader_entry.cpp`：`lexora_loader_main` 导出入口。
- `protect/loader/src/host.c`：`dlopen`宿主程序。
