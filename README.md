# 飞牛 Docker Telegram 远程管理机器人

建议把机器人做成远程镜像。飞牛端只保留 `docker-compose.yml`，直接拉镜像运行，不需要在飞牛上构建，也不需要重启飞牛整机。

## 1. GitHub 自动发布镜像

项目内置了 GitHub Actions 工作流：

```text
.github/workflows/docker-publish.yml
```

推送到 `main` 分支，或推送 `v*.*.*` tag 时，会自动构建并推送镜像。

默认一定推送 GitHub Container Registry：

```text
ghcr.io/yanganan/fn-docker-tg-bot:latest
ghcr.io/yanganan/fn-docker-tg-bot:sha-xxxxxxx
ghcr.io/yanganan/fn-docker-tg-bot:v1.0.0
```

如果仓库配置了 Docker Hub secrets，还会同步推送 Docker Hub：

```text
yanganan/fn-docker-tg-bot:latest
yanganan/fn-docker-tg-bot:sha-xxxxxxx
yanganan/fn-docker-tg-bot:v1.0.0
```

支持的平台：

```text
linux/amd64
linux/arm64
```

飞牛一般使用 `linux/amd64`，如果是 ARM 设备也能直接拉同一个镜像。

## 2. Docker Hub 配置

如果只用 GHCR，可以不配置 Docker Hub。需要同步 Docker Hub 时，在 GitHub 仓库里配置两个 Secrets：

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

`DOCKERHUB_TOKEN` 建议在 Docker Hub 创建 Access Token，不要直接使用 Docker Hub 登录密码。

配置位置：

```text
GitHub 仓库 -> Settings -> Secrets and variables -> Actions -> New repository secret
```

配置完成后，只要代码 push 到 `main`，GitHub Actions 就会自动构建并推送镜像。

## 3. 手动构建推送

如果暂时不用 GitHub Actions，也可以本地手动推送：

```bash
docker login
docker build -t yanganan/fn-docker-tg-bot:latest .
docker push yanganan/fn-docker-tg-bot:latest
```

## 4. 飞牛端部署

飞牛上只需要放一个 [docker-compose.yml](/Users/xavier/Documents/Codex/fn-contrl-tg/docker-compose.yml)，不需要放完整源码。

需要修改这些配置：

```yaml
image: ghcr.io/yanganan/fn-docker-tg-bot:latest

environment:
  TELEGRAM_TOKEN: "你的机器人token"
  ALLOWED_USER_ID: "你的Telegram用户ID"
  DOCKER_COMPOSE_DIR: "/vol1/1000/docker-services"

volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - /vol1/1000/docker-services:/vol1/1000/docker-services
```

启动：

```bash
docker compose pull
docker compose up -d
```

查看日志：

```bash
docker compose logs -f docker-tg-bot
```

## 5. 后续更新

后续只需要把代码推到 GitHub `main` 分支，GitHub Actions 会自动发布镜像。

飞牛端更新机器人镜像：

```bash
docker compose pull
docker compose up -d
```

这样只会重启 `docker-tg-bot` 容器，不会重启飞牛整机。

## 6. Telegram 常用入口

- `/start`：打开底部菜单和按钮菜单
- `/menu`：打开功能菜单
- `/help`：查看完整命令
- `/bot_info`：查看机器人运行信息
- `/bot_restart`：重启机器人进程

如果使用远程镜像，`/bot_restart` 只会重启当前镜像里的代码。代码有更新时，仍然需要先在飞牛执行 `docker compose pull && docker compose up -d`。

## 7. 注意事项

- 机器人通过 `/var/run/docker.sock` 操作宿主机 Docker，权限很高，只允许自己的 Telegram 用户 ID。
- 删除容器、删除卷、清理镜像等危险操作，脚本里已经加了二次确认。
- `DOCKER_COMPOSE_DIR` 必须和 volumes 挂载路径一致，否则机器人容器内找不到 compose 服务目录。
- 如果你的 compose 服务目录不是 `/vol1/1000/docker-services`，同时改 `DOCKER_COMPOSE_DIR` 和 volumes 第二行。
