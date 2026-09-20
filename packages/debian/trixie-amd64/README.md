# Debian 离线软件包

本目录保存 `Dockerfile` 在无网络构建期间安装的 Debian 13（Trixie）amd64 软件包。

- `protect-builder/`：`build-essential`、`cmake`、`libssl-dev`、`binutils` 及其依赖闭包。
- `runtime/`：`vim`、`curl`、`dnsutils`、`iputils-ping`、`iproute2`、`netcat-openbsd`、`procps`、`less` 及其依赖闭包。
- `Packages`：由 `dpkg-scanpackages` 生成的软件包索引。
- `SHA256SUMS`：目录内所有 `.deb` 文件的完整性校验值，Docker 构建会在安装前验证。

软件包依赖闭包基于以下基础镜像生成：

```text
python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17
```

更新基础镜像摘要时，必须重新解析、下载并断网验证两个目录，不能继续复用旧依赖闭包。
