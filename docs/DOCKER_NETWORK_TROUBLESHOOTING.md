# Docker 网络排障

本文记录“宿主机可以访问，容器或 API 服务不能访问”的排查方法。重点是区分浏览器、远端宿主机、服务容器和代码请求，不要用一个环境的成功结论覆盖另一个环境。

## 适用场景

典型现象：

- 浏览器粘贴 URL 可以下载文件。
- 部署宿主机执行 `curl -o` 可以下载文件。
- `/api/review/jobs` 使用 `file_url` 下载失败。
- `events.log` 记录 `contract_url_download_failed`。
- 容器内请求返回 `404`、`403`、`502`，或 DNS 解析失败。

## 先看错误层级

先看任务目录下的 API 事件日志：

```json
{"event": "contract_url_download_failed", "http_status": 404, "error": "..."}
```

判断口径：

- `http_status: null`：请求没有到 HTTP 层，优先排查 DNS、连接、超时、代理。
- `http_status: 404`：请求已经到达某个 HTTP 服务，但当前入口、路径、签名参数或请求上下文没有命中资源。
- `http_status: 401` / `403`：优先排查鉴权、Cookie、Referer、User-Agent 或签名参数。
- `http_status: 502`：优先排查网关、上游服务、代理或容器网络出口。
- API 返回仍是 `{ "detail": "..." }`：先确认服务容器是否已经运行最新代码。

## 区分三个环境

浏览器、宿主机和容器是三个不同环境：

```text
浏览器：本地电脑 + 浏览器 DNS/代理/Cookie/VPN
宿主机：远端服务器 + 宿主机 DNS/hosts/网络出口
容器：Docker 网络命名空间 + 容器 /etc/hosts + Docker DNS
```

浏览器能下载，只能证明浏览器所在环境能访问该 URL。API 下载发生在服务容器内，必须在容器内验证。

## 宿主机验证

在服务部署宿主机执行：

```bash
curl -v -L -o /tmp/test.docx '完整URL'
```

重点看输出中的实际连接 IP：

```text
Trying <宿主机成功解析的IP>:443...
```

这个 IP 是宿主机成功下载时的入口。后续要确认容器是否也解析到同一个入口。

## 容器 DNS 验证

当前镜像预装了常用容器排障工具，可以直接使用 `nslookup`、`dig`、`curl`、`ping`、`ip`、`nc`、`ps`、`less` 和 `vim`。如果正在排查的是旧镜像，工具可能不存在；此时可以先用 Python 验证 DNS：

```bash
docker exec -it <container> python -c 'import socket; print({x[4][0] for x in socket.getaddrinfo("域名",80)})'
```

判断：

- 输出是宿主机成功 IP：DNS 入口一致，继续测 HTTP 请求。
- 输出是其他 IP：容器 DNS/hosts 和宿主机不一致。
- 输出多个 IP：容器 hosts 或 DNS 结果不稳定，不要继续追加 hosts。
- 报 `Name or service not known`：容器 DNS 无法解析该域名。

容器的 DNS 常见是 Docker 内置 DNS：

```text
nameserver 127.0.0.11
```

这说明容器并不是直接使用宿主机 `/etc/resolv.conf`。

也可以使用镜像内预装命令：

```bash
docker exec -it <container> nslookup 域名
docker exec -it <container> dig 域名
```

## 容器内排障工具

镜像基于 `python:3.12-slim`，使用 Debian `apt` 安装以下工具：

| 命令 | apt 包 | 用途 |
| --- | --- | --- |
| `vim` | `vim` | 临时查看或编辑容器内文件。 |
| `curl` | `curl` | 验证 URL、HTTP 状态、响应头和实际连接入口。 |
| `nslookup` / `dig` | `dnsutils` | 检查 DNS 解析结果。 |
| `ping` | `iputils-ping` | 基础网络连通性验证。 |
| `ip` | `iproute2` | 查看网卡、地址和路由。 |
| `nc` | `netcat-openbsd` | 验证 TCP 端口连通性。 |
| `ps` | `procps` | 查看容器内进程。 |
| `less` | `less` | 查看较长日志或文本输出。 |

这些工具只解决容器内检查命令不可用的问题，不改变容器 DNS、路由或应用下载逻辑。若容器仍访问失败，应继续按宿主机、容器 DNS、容器 HTTP 三层分别验证。

## 容器 HTTP 验证

在容器内用代码路径相同的 HTTP 客户端验证：

```bash
docker exec -it <container> python -c 'import httpx; u="完整URL"; r=httpx.get(u,follow_redirects=True,timeout=60); print(r.status_code,r.headers.get("content-type"),len(r.content)); print(r.text[:120] if "text" in (r.headers.get("content-type") or "") else "")'
```

判断：

- 容器 `httpx` 返回 `200`，但 API 返回失败：优先排查 API 入参是否被截断、转义错误，或服务是否不是最新版本。
- 容器 `httpx` 返回 `404`，宿主机 `curl` 返回 `200`：优先排查容器 DNS/hosts/网络入口。
- 容器 `httpx` 返回 `401` / `403`：优先排查请求头、Cookie、Referer、签名参数。

如果怀疑缺少浏览器请求头，可临时加 `User-Agent` 验证：

```bash
docker exec -it <container> python -c 'import httpx; u="完整URL"; r=httpx.get(u,follow_redirects=True,timeout=60,headers={"User-Agent":"Mozilla/5.0"}); print(r.status_code,r.headers.get("content-type"),len(r.content)); print(r.text[:120] if "text" in (r.headers.get("content-type") or "") else "")'
```

如果加 `User-Agent` 后从失败变成 `200`，再考虑是否需要在代码下载逻辑中补请求头。

## 修复容器 DNS/hosts

如果宿主机成功下载时连接的是某个固定 IP，而容器解析到其他 IP，可以在 `docker-compose.yaml` 中使用 `extra_hosts`：

```yaml
services:
  lexora:
    extra_hosts:
      - "service.example.internal:<目标IP>"
```

然后重建容器：

```bash
docker compose down
docker compose up -d
```

重建后验证：

```bash
docker exec -it <container> python -c 'import socket; print({x[4][0] for x in socket.getaddrinfo("service.example.internal",443)})'
```

必须只输出目标 IP。

## extra_hosts 的作用

`extra_hosts` 会在容器启动时向容器 `/etc/hosts` 注入记录，效果类似：

```text
<目标IP> service.example.internal
```

它用于让容器把指定域名解析到指定 IP。它是短期稳定修复，适合内网 DNS 暂时不可用或域名解析入口不一致的场景。

不需要 hosts 映射时，不要写 `extra_hosts`。写成空列表通常不会出错，但没有意义：

```yaml
services:
  lexora:
    extra_hosts: []
```

不要写空字符串或不完整映射：

```yaml
services:
  lexora:
    extra_hosts:
      - ""
      - "service.example.internal:"
```

这类占位值可能导致 Compose 解析失败，或向容器 `/etc/hosts` 写入异常记录。

长期更好的方式是配置正确内网 DNS：

```yaml
services:
  lexora:
    dns:
      - "<内网DNS_IP>"
```

固定 IP 的风险是后端入口、负载均衡或内网解析调整后会失效。

## 不要直接改容器 /etc/hosts

Docker 会为每个容器生成并管理这些运行时文件：

```text
/etc/hosts
/etc/hostname
/etc/resolv.conf
```

它们不是镜像里的普通静态文件，而是 Docker 启动容器时挂载进去的配置文件。直接执行：

```bash
sed -i '/domain/d' /etc/hosts
```

可能报错：

```text
Device or resource busy
```

因为 `sed -i` 会创建临时文件再替换原文件，而 `/etc/hosts` 是 Docker 管理的挂载文件。

也不要反复执行：

```bash
echo 'IP domain' >> /etc/hosts
```

重复记录会导致解析结果混乱，容器可能仍然解析到错误入口。
