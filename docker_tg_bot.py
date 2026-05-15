import os
import shutil
import signal
import shlex
import subprocess
import time
import uuid
from functools import wraps

import docker
import psutil
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater


load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ALLOWED_USER_IDS", os.getenv("ALLOWED_USER_ID", "")).split(",")
    if user_id.strip()
}
DOCKER_COMPOSE_DIR = os.getenv("DOCKER_COMPOSE_DIR", "/opt/docker-services")
DOCKER_COMPOSE_FILE = os.getenv("DOCKER_COMPOSE_FILE", "")
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "120"))
LOG_TAIL = int(os.getenv("LOG_TAIL", "80"))
MESSAGE_LIMIT = 3200
PENDING_ACTIONS = {}
CALLBACK_ACTIONS = {}
COMPOSE_BIN = None

client = docker.from_env()


def restricted(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user
        if not user or user.id not in ALLOWED_USER_IDS:
            reply(update, "无操作权限。")
            return
        try:
            return func(update, context)
        except ValueError as e:
            reply(update, str(e))
        except Exception as e:
            reply(update, f"操作失败：{e}")

    return wrapper


def reply(update: Update, text: str, markup=None, parse_mode=None):
    if update.callback_query:
        update.callback_query.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True, parse_mode=parse_mode)
    elif update.message:
        update.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True, parse_mode=parse_mode)


def edit_or_reply(update: Update, text: str, markup=None):
    if update.callback_query:
        update.callback_query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        reply(update, text, markup)


def persistent_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["主菜单", "服务管理", "容器管理"],
            ["镜像管理", "存储管理", "服务器状态"],
            ["清理工具", "帮助"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def callback_token(action: str, target: str = "") -> str:
    token = uuid.uuid4().hex[:10]
    CALLBACK_ACTIONS[token] = {"action": action, "target": target, "created": time.time()}
    return f"cb:{token}"


def resolve_callback_token(data: str):
    token = data.split(":", 1)[1]
    item = CALLBACK_ACTIONS.pop(token, None)
    if not item or time.time() - item["created"] > 600:
        raise ValueError("按钮已过期，请重新打开菜单。")
    return item["action"], item["target"]


def send_block(update: Update, title: str, body: str):
    body = body or "(无输出)"
    chunks = [body[i : i + MESSAGE_LIMIT] for i in range(0, len(body), MESSAGE_LIMIT)]
    for index, chunk in enumerate(chunks):
        suffix = f" ({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        reply(update, f"{title}{suffix}\n{chunk}")


def run_cmd(cmd, cwd=None, timeout=COMMAND_TIMEOUT):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(output.strip() or f"命令失败，退出码：{result.returncode}")
    return output.strip()


def compose_cmd(*args):
    global COMPOSE_BIN
    base = os.getenv("DOCKER_COMPOSE_CMD", "").strip()
    if base:
        cmd = shlex.split(base)
    elif COMPOSE_BIN:
        cmd = list(COMPOSE_BIN)
    elif shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        COMPOSE_BIN = ["docker", "compose"]
        cmd = list(COMPOSE_BIN)
    elif shutil.which("docker-compose"):
        COMPOSE_BIN = ["docker-compose"]
        cmd = list(COMPOSE_BIN)
    else:
        raise RuntimeError("未找到 docker compose 或 docker-compose，请安装后再使用 compose 服务操作。")
    if DOCKER_COMPOSE_FILE:
        cmd.extend(["-f", DOCKER_COMPOSE_FILE])
    cmd.extend(args)
    return cmd

def run_compose(*args):
    return run_cmd(compose_cmd(*args), cwd=DOCKER_COMPOSE_DIR)


def safe_arg(value: str) -> str:
    if not value or any(ch in value for ch in "\n\r;&|`$<>"):
        raise ValueError("参数包含不安全字符，请只使用服务名、容器名、镜像名或普通参数。")
    return value


def require_arg(context: CallbackContext, usage: str):
    if not context.args:
        raise ValueError(f"缺少参数。用法：{usage}")
    return safe_arg(context.args[0])


def get_container_rows(all_containers=True):
    containers = client.containers.list(all=all_containers)
    rows = []
    for c in containers:
        image = c.image.tags[0] if c.image.tags else c.image.short_id
        ports = []
        for container_port, mappings in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
            if mappings:
                for item in mappings:
                    ports.append(f"{item.get('HostIp', '')}:{item.get('HostPort', '')}->{container_port}")
        rows.append(f"{c.name:28} {c.status:12} {image:38} {' '.join(ports) or '-'}")
    return rows


def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("服务管理", callback_data="menu:services"),
                InlineKeyboardButton("容器管理", callback_data="menu:containers"),
            ],
            [
                InlineKeyboardButton("镜像管理", callback_data="menu:images"),
                InlineKeyboardButton("存储卷", callback_data="menu:volumes"),
            ],
            [
                InlineKeyboardButton("服务器状态", callback_data="menu:server"),
                InlineKeyboardButton("清理资源", callback_data="menu:cleanup"),
            ],
            [InlineKeyboardButton("命令帮助", callback_data="menu:help")],
        ]
    )


def services_menu():
    services = list_compose_services()
    rows = []
    for service in services[:20]:
        rows.append(
            [
                InlineKeyboardButton(f"{service}", callback_data=callback_token("svc_view", service)),
                InlineKeyboardButton("启", callback_data=callback_token("confirm:svc_start", service)),
                InlineKeyboardButton("停", callback_data=callback_token("confirm:svc_stop", service)),
                InlineKeyboardButton("重启", callback_data=callback_token("confirm:svc_restart", service)),
                InlineKeyboardButton("更新", callback_data=callback_token("confirm:svc_update", service)),
            ]
        )
    rows.append([InlineKeyboardButton("全部更新", callback_data="confirm:svc_update_all:-")])
    rows.append([InlineKeyboardButton("返回", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def container_menu():
    rows = []
    containers = client.containers.list(all=True)
    for c in containers[:20]:
        rows.append(
            [
                InlineKeyboardButton(c.name[:18], callback_data=callback_token("ctr_view", c.name)),
                InlineKeyboardButton("启", callback_data=callback_token("confirm:ctr_start", c.name)),
                InlineKeyboardButton("停", callback_data=callback_token("confirm:ctr_stop", c.name)),
                InlineKeyboardButton("重启", callback_data=callback_token("confirm:ctr_restart", c.name)),
                InlineKeyboardButton("更新", callback_data=callback_token("confirm:ctr_update", c.name)),
            ]
        )
    rows.append([InlineKeyboardButton("刷新容器列表", callback_data="menu:containers")])
    rows.append([InlineKeyboardButton("返回", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def image_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("查看镜像列表", callback_data="menu:image_list")],
            [InlineKeyboardButton("清理未使用镜像", callback_data="confirm:image_prune:-")],
            [InlineKeyboardButton("Docker 磁盘占用", callback_data="menu:system_df")],
            [InlineKeyboardButton("返回", callback_data="menu:main")],
        ]
    )


def volume_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("查看存储卷", callback_data="menu:volume_list")],
            [InlineKeyboardButton("查看 Docker 网络", callback_data="menu:network_list")],
            [InlineKeyboardButton("清理未使用卷", callback_data="confirm:volume_prune:-")],
            [InlineKeyboardButton("清理未使用网络", callback_data="confirm:network_prune:-")],
            [InlineKeyboardButton("返回", callback_data="menu:main")],
        ]
    )


def server_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("资源", callback_data="menu:server_info"),
                InlineKeyboardButton("负载", callback_data="menu:server_load"),
            ],
            [
                InlineKeyboardButton("网络", callback_data="menu:server_network"),
                InlineKeyboardButton("进程", callback_data="menu:server_top"),
            ],
            [InlineKeyboardButton("返回", callback_data="menu:main")],
        ]
    )


def cleanup_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Docker 综合清理", callback_data="confirm:docker_prune:-")],
            [InlineKeyboardButton("清理未使用卷", callback_data="confirm:volume_prune:-")],
            [InlineKeyboardButton("清理未使用镜像", callback_data="confirm:image_prune:-")],
            [InlineKeyboardButton("清理未使用网络", callback_data="confirm:network_prune:-")],
            [InlineKeyboardButton("返回", callback_data="menu:main")],
        ]
    )


def list_compose_services():
    try:
        output = run_compose("config", "--services")
        return [line.strip() for line in output.splitlines() if line.strip()]
    except Exception:
        containers = client.containers.list(all=True)
        services = sorted(
            {
                c.labels.get("com.docker.compose.service")
                for c in containers
                if c.labels.get("com.docker.compose.project")
            }
        )
        return [s for s in services if s]


def confirmation_keyboard(action, target, label):
    token = uuid.uuid4().hex[:10]
    PENDING_ACTIONS[token] = {"action": action, "target": target, "created": time.time()}
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"确认{label}", callback_data=f"do:{token}"),
                InlineKeyboardButton("取消", callback_data="cancel"),
            ]
        ]
    )


HELP_TEXT = """Docker 远程管理机器人

常用入口：
/menu - 打开 Telegram 按钮菜单
/help - 查看完整命令
/status - 查看 docker compose 状态
/services - 列出 compose 服务
/ps - 列出容器
/server_info - 查看服务器资源

Compose 服务操作：
/service_start <服务名> - 启动服务
/service_stop <服务名> - 停止服务
/service_restart <服务名> - 重启服务
/service_update [服务名] - 拉取镜像并 up -d，留空更新全部
/service_recreate [服务名] - 强制重建服务，留空重建全部
/service_add <服务名> - 按 compose 文件新增/拉起服务
/service_remove <服务名> - 停止并删除 compose 服务容器，需要确认
/service_pull [服务名] - 只拉取镜像
/service_logs <服务名> [行数] - 查看服务日志
/scale <服务名> <副本数> - 调整副本数

容器操作：
/container_start <容器名> - 启动容器
/container_stop <容器名> - 停止容器
/container_restart <容器名> - 重启容器
/container_update <容器名> - 更新容器；compose 容器会更新对应服务，独立容器会拉取镜像并重启
/container_remove <容器名> - 删除容器，需要确认
/container_stats <容器名> - 查看 CPU/内存/网络占用
/container_inspect <容器名> - 查看容器摘要
/container_run <名称> <镜像> [docker run 参数...] - 创建并后台运行容器，需要确认

镜像操作：
/images - 列出镜像
/image_pull <镜像:标签> - 拉取镜像
/image_remove <镜像ID或名称> - 删除镜像，需要确认
/image_prune - 清理未使用镜像，需要确认

存储卷与网络：
/volumes - 列出存储卷
/volume_inspect <卷名> - 查看卷详情
/volume_remove <卷名> - 删除卷，需要确认
/volume_prune - 清理未使用卷，需要确认
/networks - 列出 Docker 网络
/network_prune - 清理未使用网络，需要确认

系统清理：
/docker_prune - 清理未使用容器、镜像、网络、构建缓存，需要确认
/system_df - 查看 Docker 磁盘占用

机器人维护：
/bot_restart - 重启机器人进程，需要 Docker/systemd 配置自动拉起
/bot_info - 查看机器人运行信息

兼容旧命令：
/docker_info /docker_images /docker_containers /update /start_service /stop /restart /logs /clean /clean_volumes
"""


@restricted
def start(update: Update, context: CallbackContext):
    reply(update, "Docker 远程管理机器人已就绪。底部菜单已开启，也可以点击下面的功能入口。", persistent_keyboard())
    reply(update, "请选择要管理的 Docker 资源：", main_menu())


@restricted
def help_command(update: Update, context: CallbackContext):
    send_block(update, "命令帮助", HELP_TEXT)


@restricted
def menu(update: Update, context: CallbackContext):
    reply(update, "请选择要管理的 Docker 资源：", persistent_keyboard())
    reply(update, "功能入口：", main_menu())


@restricted
def callback_router(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    token_action = None
    token_target = None
    if data.startswith("cb:"):
        token_action, token_target = resolve_callback_token(data)
    if data == "cancel":
        edit_or_reply(update, "已取消。", main_menu())
        return
    if data == "menu:main":
        edit_or_reply(update, "请选择要管理的 Docker 资源：", main_menu())
        return
    if data == "menu:help":
        edit_or_reply(update, HELP_TEXT, main_menu())
        return
    if data == "menu:services":
        edit_or_reply(update, "选择服务操作：", services_menu())
        return
    if data == "menu:containers":
        send_block(update, "容器列表", "\n".join(get_container_rows(True)[:60]) or "暂无容器")
        reply(update, "选择容器操作：", container_menu())
        return
    if data == "menu:images":
        edit_or_reply(update, "选择镜像操作：", image_menu())
        return
    if data == "menu:volumes":
        edit_or_reply(update, "选择存储/网络操作：", volume_menu())
        return
    if data == "menu:server":
        edit_or_reply(update, "选择服务器监控项：", server_menu())
        return
    if data == "menu:server_info":
        server_info(update, context)
        return
    if data == "menu:server_load":
        server_load(update, context)
        return
    if data == "menu:server_network":
        server_network(update, context)
        return
    if data == "menu:server_top":
        server_top(update, context)
        return
    if data == "menu:image_list":
        docker_images(update, context)
        return
    if data == "menu:volume_list":
        list_volumes(update, context)
        return
    if data == "menu:network_list":
        list_networks(update, context)
        return
    if data == "menu:system_df":
        system_df(update, context)
        return
    if data == "menu:cleanup":
        edit_or_reply(update, "清理操作需要二次确认：", cleanup_menu())
        return
    if token_action == "svc_view":
        service = token_target
        output = run_compose("ps", service)
        send_block(update, f"{service} 状态", output)
        return
    if token_action == "ctr_view":
        name = token_target
        c = client.containers.get(name)
        body = "\n".join(
            [
                f"名称：{c.name}",
                f"ID：{c.short_id}",
                f"状态：{c.status}",
                f"镜像：{c.image.tags[0] if c.image.tags else c.image.short_id}",
                f"创建时间：{c.attrs.get('Created', '-')}",
            ]
        )
        send_block(update, f"容器详情 {name}", body)
        return
    if data.startswith("confirm:") or (token_action and token_action.startswith("confirm:")):
        if token_action:
            action = token_action.split(":", 1)[1]
            target = token_target
        else:
            _, action, target = data.split(":", 2)
        labels = {
            "svc_start": "启动",
            "svc_stop": "停止",
            "svc_restart": "重启",
            "svc_update": "更新服务",
            "svc_update_all": "更新全部服务",
            "ctr_start": "启动容器",
            "ctr_stop": "停止容器",
            "ctr_restart": "重启容器",
            "ctr_update": "更新容器",
            "docker_prune": "综合清理",
            "volume_prune": "清理未使用卷",
            "image_prune": "清理未使用镜像",
            "network_prune": "清理未使用网络",
        }
        edit_or_reply(update, f"确认执行：{labels.get(action, action)} {target if target != '-' else ''}", confirmation_keyboard(action, target, labels.get(action, action)))
        return
    if data.startswith("do:"):
        token = data.split(":", 1)[1]
        pending = PENDING_ACTIONS.pop(token, None)
        if not pending or time.time() - pending["created"] > 300:
            edit_or_reply(update, "确认已过期，请重新发起操作。", main_menu())
            return
        execute_confirmed_action(update, pending["action"], pending["target"])


@restricted
def text_menu_router(update: Update, context: CallbackContext):
    text = (update.message.text or "").strip()
    if text in ("主菜单", "菜单"):
        menu(update, context)
    elif text == "服务管理":
        reply(update, "选择服务操作：", services_menu())
    elif text == "容器管理":
        send_block(update, "容器列表", "\n".join(get_container_rows(True)[:60]) or "暂无容器")
        reply(update, "选择容器操作：", container_menu())
    elif text == "镜像管理":
        reply(update, "选择镜像操作：", image_menu())
    elif text == "存储管理":
        reply(update, "选择存储/网络操作：", volume_menu())
    elif text == "服务器状态":
        reply(update, "选择服务器监控项：", server_menu())
    elif text == "清理工具":
        reply(update, "清理操作需要二次确认：", cleanup_menu())
    elif text == "帮助":
        help_command(update, context)


def execute_confirmed_action(update: Update, action: str, target: str):
    try:
        if action == "svc_start":
            output = run_compose("start", target)
        elif action == "svc_stop":
            output = run_compose("stop", target)
        elif action == "svc_restart":
            output = run_compose("restart", target)
        elif action == "svc_update":
            output = run_compose("pull", target) + "\n" + run_compose("up", "-d", target)
        elif action == "svc_update_all":
            output = run_compose("pull") + "\n" + run_compose("up", "-d")
        elif action == "ctr_start":
            output = run_cmd(["docker", "start", target])
        elif action == "ctr_stop":
            output = run_cmd(["docker", "stop", target])
        elif action == "ctr_restart":
            output = run_cmd(["docker", "restart", target])
        elif action == "ctr_update":
            output = update_container_by_name(target)
        elif action == "docker_prune":
            output = run_cmd(["docker", "system", "prune", "-af"])
        elif action == "volume_prune":
            output = run_cmd(["docker", "volume", "prune", "-f"])
        elif action == "image_prune":
            output = run_cmd(["docker", "image", "prune", "-af"])
        elif action == "service_remove":
            output = run_compose("rm", "-sf", target)
        elif action == "container_remove":
            output = run_cmd(["docker", "rm", "-f", target])
        elif action == "image_remove":
            output = run_cmd(["docker", "rmi", target])
        elif action == "volume_remove":
            output = run_cmd(["docker", "volume", "rm", target])
        elif action == "network_prune":
            output = run_cmd(["docker", "network", "prune", "-f"])
        elif action == "container_run":
            output = run_cmd(shlex.split(target))
        else:
            output = f"未知操作：{action}"
        send_block(update, "执行完成", output)
    except Exception as e:
        reply(update, f"执行失败：{e}")


def update_container_by_name(name: str) -> str:
    container = client.containers.get(name)
    compose_service = container.labels.get("com.docker.compose.service")
    compose_project = container.labels.get("com.docker.compose.project")
    image = container.image.tags[0] if container.image.tags else container.image.short_id
    if compose_service and compose_project:
        return (
            f"检测到 compose 容器，按服务更新：{compose_service}\n"
            + run_compose("pull", compose_service)
            + "\n"
            + run_compose("up", "-d", compose_service)
        )
    pull_output = run_cmd(["docker", "pull", image], timeout=600) if ":" in image and not image.startswith("sha256:") else f"镜像 {image} 没有可拉取的标签，跳过 pull。"
    restart_output = run_cmd(["docker", "restart", name])
    return (
        f"检测到非 compose 容器：{name}\n"
        f"已拉取镜像并重启容器。注意：独立 docker run 容器不会自动替换为新镜像，如需真正重建，请使用 compose 管理或手动删除后重新创建。\n\n"
        f"{pull_output}\n{restart_output}"
    )


@restricted
def get_status(update: Update, context: CallbackContext):
    send_block(update, "Compose 服务状态", run_compose("ps"))


@restricted
def list_services(update: Update, context: CallbackContext):
    services = list_compose_services()
    send_block(update, "Compose 服务列表", "\n".join(services) or "未找到服务")


@restricted
def list_containers(update: Update, context: CallbackContext):
    rows = get_container_rows(True)
    header = f"{'NAME':28} {'STATUS':12} {'IMAGE':38} PORTS"
    send_block(update, "Docker 容器列表", "\n".join([header, "-" * len(header), *rows]))


@restricted
def docker_info(update: Update, context: CallbackContext):
    version = client.version()
    info = client.info()
    body = "\n".join(
        [
            f"Docker 版本：{version.get('Version', '未知')}",
            f"容器：{info.get('Containers', 0)}，运行中：{info.get('ContainersRunning', 0)}，停止：{info.get('ContainersStopped', 0)}",
            f"镜像：{info.get('Images', 0)}",
            f"存储驱动：{info.get('Driver', '未知')}",
            f"CPU：{info.get('NCPU', 0)} 核",
            f"内存：{info.get('MemTotal', 0) / 1024 / 1024 / 1024:.2f} GB",
        ]
    )
    send_block(update, "Docker 系统信息", body)


@restricted
def docker_images(update: Update, context: CallbackContext):
    rows = []
    for image in client.images.list():
        tags = ", ".join(image.tags) if image.tags else "<none>"
        size = image.attrs.get("Size", 0) / 1024 / 1024
        rows.append(f"{image.short_id:18} {size:10.1f} MB  {tags}")
    send_block(update, "Docker 镜像列表", "\n".join(rows) or "暂无镜像")


@restricted
def list_volumes(update: Update, context: CallbackContext):
    volumes = client.volumes.list()
    rows = []
    for volume in volumes:
        rows.append(f"{volume.name:36} {volume.attrs.get('Driver', '-')}")
    send_block(update, "Docker 存储卷", "\n".join(rows) or "暂无存储卷")


@restricted
def list_networks(update: Update, context: CallbackContext):
    networks = client.networks.list()
    rows = [f"{n.short_id:12} {n.name:28} {n.attrs.get('Driver', '-')}" for n in networks]
    send_block(update, "Docker 网络", "\n".join(rows) or "暂无网络")


@restricted
def service_start(update: Update, context: CallbackContext):
    service = require_arg(context, "/service_start <服务名>")
    send_block(update, f"启动服务 {service}", run_compose("start", service))


@restricted
def service_stop(update: Update, context: CallbackContext):
    service = require_arg(context, "/service_stop <服务名>")
    send_block(update, f"停止服务 {service}", run_compose("stop", service))


@restricted
def service_restart(update: Update, context: CallbackContext):
    service = require_arg(context, "/service_restart <服务名>")
    send_block(update, f"重启服务 {service}", run_compose("restart", service))


@restricted
def service_pull(update: Update, context: CallbackContext):
    service = safe_arg(context.args[0]) if context.args else None
    args = ["pull"] + ([service] if service else [])
    send_block(update, f"拉取镜像 {service or '全部服务'}", run_compose(*args))


@restricted
def service_update(update: Update, context: CallbackContext):
    service = safe_arg(context.args[0]) if context.args else None
    pull_args = ["pull"] + ([service] if service else [])
    up_args = ["up", "-d"] + ([service] if service else [])
    output = run_compose(*pull_args) + "\n" + run_compose(*up_args)
    send_block(update, f"更新服务 {service or '全部服务'}", output)


@restricted
def service_recreate(update: Update, context: CallbackContext):
    service = safe_arg(context.args[0]) if context.args else None
    args = ["up", "-d", "--force-recreate"] + ([service] if service else [])
    send_block(update, f"强制重建 {service or '全部服务'}", run_compose(*args))


@restricted
def service_add(update: Update, context: CallbackContext):
    service = require_arg(context, "/service_add <服务名>")
    send_block(update, f"新增/拉起服务 {service}", run_compose("up", "-d", service))


@restricted
def service_remove(update: Update, context: CallbackContext):
    service = require_arg(context, "/service_remove <服务名>")
    reply(update, f"将停止并删除 compose 服务容器：{service}", confirmation_keyboard("service_remove", service, "删除服务"))


@restricted
def service_logs(update: Update, context: CallbackContext):
    service = require_arg(context, "/service_logs <服务名> [行数]")
    tail = safe_arg(context.args[1]) if len(context.args) > 1 else str(LOG_TAIL)
    send_block(update, f"{service} 日志", run_compose("logs", f"--tail={tail}", service))


@restricted
def scale_service(update: Update, context: CallbackContext):
    if len(context.args) < 2:
        raise ValueError("缺少参数。用法：/scale <服务名> <副本数>")
    service = safe_arg(context.args[0])
    replicas = safe_arg(context.args[1])
    send_block(update, f"调整 {service} 副本数", run_compose("up", "-d", "--scale", f"{service}={replicas}", service))


@restricted
def container_action(update: Update, context: CallbackContext, action: str):
    name = require_arg(context, f"/container_{action} <容器名>")
    send_block(update, f"{action} 容器 {name}", run_cmd(["docker", action, name]))


@restricted
def container_remove(update: Update, context: CallbackContext):
    name = require_arg(context, "/container_remove <容器名>")
    reply(update, f"将强制删除容器：{name}", confirmation_keyboard("container_remove", name, "删除容器"))


@restricted
def container_update(update: Update, context: CallbackContext):
    name = require_arg(context, "/container_update <容器名>")
    reply(update, f"将更新容器：{name}", confirmation_keyboard("ctr_update", name, "更新容器"))


@restricted
def container_inspect(update: Update, context: CallbackContext):
    name = require_arg(context, "/container_inspect <容器名>")
    c = client.containers.get(name)
    body = "\n".join(
        [
            f"名称：{c.name}",
            f"ID：{c.short_id}",
            f"状态：{c.status}",
            f"镜像：{c.image.tags[0] if c.image.tags else c.image.short_id}",
            f"创建时间：{c.attrs.get('Created', '-')}",
            f"重启策略：{c.attrs.get('HostConfig', {}).get('RestartPolicy', {})}",
            f"挂载：{c.attrs.get('Mounts', [])}",
        ]
    )
    send_block(update, f"容器详情 {name}", body)


@restricted
def container_stats(update: Update, context: CallbackContext):
    name = require_arg(context, "/container_stats <容器名>")
    c = client.containers.get(name)
    stats = c.stats(stream=False)
    cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
    system_delta = stats["cpu_stats"].get("system_cpu_usage", 0) - stats["precpu_stats"].get("system_cpu_usage", 0)
    cpu_count = stats["cpu_stats"].get("online_cpus") or len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [])) or 1
    cpu = (cpu_delta / system_delta * cpu_count * 100) if system_delta > 0 else 0
    mem_used = stats.get("memory_stats", {}).get("usage", 0)
    mem_limit = stats.get("memory_stats", {}).get("limit", 1)
    network_rx = sum(n.get("rx_bytes", 0) for n in stats.get("networks", {}).values())
    network_tx = sum(n.get("tx_bytes", 0) for n in stats.get("networks", {}).values())
    body = "\n".join(
        [
            f"CPU：{cpu:.2f}%",
            f"内存：{mem_used / 1024 / 1024:.2f} MB / {mem_limit / 1024 / 1024:.2f} MB ({mem_used / mem_limit * 100:.2f}%)",
            f"网络入站：{network_rx / 1024 / 1024:.2f} MB",
            f"网络出站：{network_tx / 1024 / 1024:.2f} MB",
        ]
    )
    send_block(update, f"{name} 资源占用", body)


@restricted
def container_run(update: Update, context: CallbackContext):
    if len(context.args) < 2:
        raise ValueError("缺少参数。用法：/container_run <名称> <镜像> [docker run 参数...]")
    name = safe_arg(context.args[0])
    image = safe_arg(context.args[1])
    extra = [safe_arg(arg) for arg in context.args[2:]]
    cmd = ["docker", "run", "-d", "--name", name, *extra, image]
    reply(update, f"将创建并后台运行容器：{' '.join(shlex.quote(x) for x in cmd)}", confirmation_keyboard("container_run", " ".join(shlex.quote(x) for x in cmd), "创建容器"))


@restricted
def image_pull(update: Update, context: CallbackContext):
    image = require_arg(context, "/image_pull <镜像:标签>")
    send_block(update, f"拉取镜像 {image}", run_cmd(["docker", "pull", image], timeout=600))


@restricted
def image_remove(update: Update, context: CallbackContext):
    image = require_arg(context, "/image_remove <镜像ID或名称>")
    reply(update, f"将删除镜像：{image}", confirmation_keyboard("image_remove", image, "删除镜像"))


@restricted
def image_prune(update: Update, context: CallbackContext):
    reply(update, "将清理未使用镜像。", confirmation_keyboard("image_prune", "-", "清理镜像"))


@restricted
def volume_inspect(update: Update, context: CallbackContext):
    volume = require_arg(context, "/volume_inspect <卷名>")
    v = client.volumes.get(volume)
    body = "\n".join([f"{key}: {value}" for key, value in v.attrs.items()])
    send_block(update, f"存储卷详情 {volume}", body)


@restricted
def volume_remove(update: Update, context: CallbackContext):
    volume = require_arg(context, "/volume_remove <卷名>")
    reply(update, f"将删除存储卷：{volume}", confirmation_keyboard("volume_remove", volume, "删除存储卷"))


@restricted
def volume_prune(update: Update, context: CallbackContext):
    reply(update, "将清理未使用存储卷。", confirmation_keyboard("volume_prune", "-", "清理存储卷"))


@restricted
def network_prune(update: Update, context: CallbackContext):
    reply(update, "将清理未使用 Docker 网络。", confirmation_keyboard("network_prune", "-", "清理网络"))


@restricted
def docker_prune(update: Update, context: CallbackContext):
    reply(update, "将清理未使用容器、镜像、网络和构建缓存。", confirmation_keyboard("docker_prune", "-", "综合清理"))


@restricted
def system_df(update: Update, context: CallbackContext):
    send_block(update, "Docker 磁盘占用", run_cmd(["docker", "system", "df", "-v"]))


@restricted
def server_info(update: Update, context: CallbackContext):
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    body = "\n".join(
        [
            f"CPU：{cpu:.2f}%",
            f"内存：{mem.used / 1024 / 1024 / 1024:.2f} GB / {mem.total / 1024 / 1024 / 1024:.2f} GB ({mem.percent:.2f}%)",
            f"磁盘(/)：{disk.used / 1024 / 1024 / 1024:.2f} GB / {disk.total / 1024 / 1024 / 1024:.2f} GB ({disk.percent:.2f}%)",
        ]
    )
    send_block(update, "服务器资源状态", body)


@restricted
def server_load(update: Update, context: CallbackContext):
    load = os.getloadavg()
    uptime = time.strftime("%d天%H小时%M分钟", time.gmtime(time.time() - psutil.boot_time()))
    send_block(update, "服务器负载", f"1/5/15 分钟负载：{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}\n运行时长：{uptime}\nCPU 核心数：{psutil.cpu_count()}")


@restricted
def server_network(update: Update, context: CallbackContext):
    net_io = psutil.net_io_counters()
    lines = [f"总入站：{net_io.bytes_recv / 1024 / 1024 / 1024:.2f} GB", f"总出站：{net_io.bytes_sent / 1024 / 1024 / 1024:.2f} GB"]
    for iface, addrs in psutil.net_if_addrs().items():
        if iface in ("lo", "docker0"):
            continue
        for addr in addrs:
            if addr.family == psutil.AF_INET:
                lines.append(f"{iface}: {addr.address}/{addr.netmask}")
    send_block(update, "服务器网络", "\n".join(lines))


@restricted
def server_top(update: Update, context: CallbackContext):
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            rows.append(proc.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows = sorted(rows, key=lambda item: item.get("memory_percent") or 0, reverse=True)[:8]
    body = "\n".join(f"{p['pid']:8} {p['name'][:28]:28} CPU {p['cpu_percent'] or 0:5.1f}%  MEM {p['memory_percent'] or 0:5.1f}%" for p in rows)
    send_block(update, "TOP 进程", body)


@restricted
def bot_info(update: Update, context: CallbackContext):
    body = "\n".join(
        [
            f"PID：{os.getpid()}",
            f"Compose 目录：{DOCKER_COMPOSE_DIR}",
            f"Compose 文件：{DOCKER_COMPOSE_FILE or '(默认)'}",
            f"命令超时：{COMMAND_TIMEOUT}s",
            f"日志行数：{LOG_TAIL}",
            f"允许用户数：{len(ALLOWED_USER_IDS)}",
        ]
    )
    send_block(update, "机器人运行信息", body)


@restricted
def bot_restart(update: Update, context: CallbackContext):
    reply(update, "机器人即将退出。如果已配置 Docker restart: unless-stopped 或 systemd Restart=always，会自动拉起新进程。")
    os.kill(os.getpid(), signal.SIGTERM)


def add_handlers(dp):
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("menu", menu))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CallbackQueryHandler(callback_router))

    dp.add_handler(CommandHandler("status", get_status))
    dp.add_handler(CommandHandler("services", list_services))
    dp.add_handler(CommandHandler("ps", list_containers))
    dp.add_handler(CommandHandler("docker_info", docker_info))
    dp.add_handler(CommandHandler("docker_images", docker_images))
    dp.add_handler(CommandHandler("docker_containers", list_containers))
    dp.add_handler(CommandHandler("images", docker_images))
    dp.add_handler(CommandHandler("volumes", list_volumes))
    dp.add_handler(CommandHandler("networks", list_networks))

    dp.add_handler(CommandHandler("service_start", service_start))
    dp.add_handler(CommandHandler("service_stop", service_stop))
    dp.add_handler(CommandHandler("service_restart", service_restart))
    dp.add_handler(CommandHandler("service_pull", service_pull))
    dp.add_handler(CommandHandler("service_update", service_update))
    dp.add_handler(CommandHandler("service_recreate", service_recreate))
    dp.add_handler(CommandHandler("service_add", service_add))
    dp.add_handler(CommandHandler("service_remove", service_remove))
    dp.add_handler(CommandHandler("service_logs", service_logs))
    dp.add_handler(CommandHandler("scale", scale_service))

    dp.add_handler(CommandHandler("container_start", lambda u, c: container_action(u, c, "start")))
    dp.add_handler(CommandHandler("container_stop", lambda u, c: container_action(u, c, "stop")))
    dp.add_handler(CommandHandler("container_restart", lambda u, c: container_action(u, c, "restart")))
    dp.add_handler(CommandHandler("container_update", container_update))
    dp.add_handler(CommandHandler("container_remove", container_remove))
    dp.add_handler(CommandHandler("container_stats", container_stats))
    dp.add_handler(CommandHandler("container_inspect", container_inspect))
    dp.add_handler(CommandHandler("container_run", container_run))

    dp.add_handler(CommandHandler("image_pull", image_pull))
    dp.add_handler(CommandHandler("image_remove", image_remove))
    dp.add_handler(CommandHandler("image_prune", image_prune))
    dp.add_handler(CommandHandler("volume_inspect", volume_inspect))
    dp.add_handler(CommandHandler("volume_remove", volume_remove))
    dp.add_handler(CommandHandler("volume_prune", volume_prune))
    dp.add_handler(CommandHandler("network_prune", network_prune))
    dp.add_handler(CommandHandler("docker_prune", docker_prune))
    dp.add_handler(CommandHandler("system_df", system_df))

    dp.add_handler(CommandHandler("server_info", server_info))
    dp.add_handler(CommandHandler("server_load", server_load))
    dp.add_handler(CommandHandler("server_network", server_network))
    dp.add_handler(CommandHandler("server_top", server_top))
    dp.add_handler(CommandHandler("bot_info", bot_info))
    dp.add_handler(CommandHandler("bot_restart", bot_restart))

    # 旧命令兼容。
    dp.add_handler(CommandHandler("update", service_update))
    dp.add_handler(CommandHandler("start_service", service_start))
    dp.add_handler(CommandHandler("stop", service_stop))
    dp.add_handler(CommandHandler("restart", service_restart))
    dp.add_handler(CommandHandler("logs", service_logs))
    dp.add_handler(CommandHandler("clean", docker_prune))
    dp.add_handler(CommandHandler("clean_volumes", volume_prune))
    dp.add_handler(CommandHandler("rm", menu))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_menu_router))


def set_bot_commands(updater: Updater):
    updater.bot.set_my_commands(
        [
            BotCommand("start", "打开 Docker 管理菜单"),
            BotCommand("menu", "打开功能菜单"),
            BotCommand("help", "查看帮助和完整命令"),
            BotCommand("status", "查看 compose 服务状态"),
            BotCommand("services", "列出 compose 服务"),
            BotCommand("ps", "列出 Docker 容器"),
            BotCommand("images", "列出 Docker 镜像"),
            BotCommand("volumes", "列出 Docker 存储卷"),
            BotCommand("system_df", "查看 Docker 磁盘占用"),
            BotCommand("server_info", "查看服务器资源状态"),
            BotCommand("bot_info", "查看机器人运行信息"),
            BotCommand("bot_restart", "重启机器人进程"),
        ]
    )


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("请在 .env 中配置 TELEGRAM_TOKEN")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("请在 .env 中配置 ALLOWED_USER_ID 或 ALLOWED_USER_IDS")

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    add_handlers(updater.dispatcher)
    set_bot_commands(updater)
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
