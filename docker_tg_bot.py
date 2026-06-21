import logging
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from functools import wraps

import docker
import psutil
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater


# ─── 日志系统 (#5) ───────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("docker-tg-bot")


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
PULL_TIMEOUT = int(os.getenv("PULL_TIMEOUT", "1000"))  # 镜像拉取默认 1000s
LOG_TAIL = int(os.getenv("LOG_TAIL", "80"))
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "").strip()
TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "15"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "30"))
MESSAGE_LIMIT = 3200

# ─── Token 存储 + 过期清理线程 (#2) ──────────────────────────
PENDING_ACTIONS = {}
CALLBACK_ACTIONS = {}
CALLBACK_TOKEN_TTL = 600   # callback token 有效期 10 分钟
PENDING_TOKEN_TTL = 300    # 确认 token 有效期 5 分钟
TOKEN_CLEANUP_INTERVAL = 300  # 清理间隔 5 分钟

COMPOSE_BIN = None

# ─── 操作并发锁 (#9) ─────────────────────────────────────────
_operation_lock = threading.Lock()

# ─── 异步操作追踪 (#10) ──────────────────────────────────────
_active_ops = {}  # chat_id -> operation description
_bot_instance = None  # 在 main() 中赋值，供后台线程发消息

# ─── 监控配置 (#11, #12) ─────────────────────────────────────
ENABLE_EVENT_MONITOR = os.getenv("ENABLE_EVENT_MONITOR", "true").strip().lower() in ("true", "1", "yes")
EVENT_IGNORE = {s.strip() for s in os.getenv("EVENT_IGNORE", "").split(",") if s.strip()}
ENABLE_RESOURCE_MONITOR = os.getenv("ENABLE_RESOURCE_MONITOR", "true").strip().lower() in ("true", "1", "yes")
DISK_THRESHOLD = int(os.getenv("DISK_THRESHOLD", "85"))
MEM_THRESHOLD = int(os.getenv("MEM_THRESHOLD", "90"))
CPU_THRESHOLD = int(os.getenv("CPU_THRESHOLD", "95"))
RESOURCE_CHECK_INTERVAL = int(os.getenv("RESOURCE_CHECK_INTERVAL", "60"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "1800"))  # 30 分钟

# 定义哪些操作需要异步执行（可能耗时较长）
LONG_OPERATIONS = {
    "svc_update", "svc_update_all", "ctr_update",
    "cleanup_safe", "cleanup_standard", "cleanup_deep",
    "container_run",
}

# ─── Phase 3: 更新检查配置 (#14) ────────────────────────────
ENABLE_UPDATE_CHECKER = os.getenv("ENABLE_UPDATE_CHECKER", "true").strip().lower() in ("true", "1", "yes")
UPDATE_CHECK_INTERVAL = int(os.getenv("UPDATE_CHECK_INTERVAL", "6")) * 3600  # 小时 → 秒
UPDATE_IGNORE = {s.strip() for s in os.getenv("UPDATE_IGNORE", "").split(",") if s.strip()}

# ─── Phase 3: 多 Compose 项目支持 (#15) ─────────────────────
# COMPOSE_DIRS 用冒号分隔多个目录，向后兼容单个 DOCKER_COMPOSE_DIR
_COMPOSE_PROJECTS = []  # [{"name": str, "dir": str, "file": str|None}]

def _load_compose_projects():
    """加载所有 compose 项目配置。"""
    global _COMPOSE_PROJECTS
    dirs_env = os.getenv("COMPOSE_DIRS", "").strip()
    if dirs_env:
        for d in dirs_env.split(":"):
            d = d.strip()
            if d and os.path.isdir(d):
                _COMPOSE_PROJECTS.append({
                    "name": os.path.basename(d.rstrip("/")),
                    "dir": d,
                    "file": None,
                })
    if not _COMPOSE_PROJECTS:
        # 向后兼容：使用 DOCKER_COMPOSE_DIR
        _COMPOSE_PROJECTS.append({
            "name": os.path.basename(DOCKER_COMPOSE_DIR.rstrip("/")) or "default",
            "dir": DOCKER_COMPOSE_DIR,
            "file": DOCKER_COMPOSE_FILE or None,
        })

_load_compose_projects()

# ─── Phase 3: 操作历史 SQLite (#16) ─────────────────────────
HISTORY_DB = os.getenv("HISTORY_DB", "/data/bot_history.db")

# ─── Phase 4: 高级功能配置 ──────────────────────────────────
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_USER_IDS", os.getenv("ALLOWED_USER_IDS", os.getenv("ALLOWED_USER_ID", ""))).split(",")
    if uid.strip()
}
READONLY_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("READONLY_USER_IDS", "").split(",")
    if uid.strip()
}
EXEC_FORBIDDEN = {"rm -rf", "shutdown", "reboot", "mkfs", "dd if=", ":(){:|:&};:"}
EXEC_TIMEOUT = int(os.getenv("EXEC_TIMEOUT", "30"))
LOG_STREAM_DURATION = int(os.getenv("LOG_STREAM_DURATION", "30"))


# ─── Docker 客户端延迟初始化 (#4) ────────────────────────────
_docker_client = None
_docker_client_lock = threading.Lock()


def get_docker_client():
    """延迟初始化 Docker 客户端，支持自动重连。"""
    global _docker_client
    if _docker_client is None:
        with _docker_client_lock:
            if _docker_client is None:
                try:
                    _docker_client = docker.from_env()
                    _docker_client.ping()
                    logger.info("Docker 客户端连接成功")
                except Exception as e:
                    _docker_client = None
                    raise RuntimeError(f"无法连接 Docker: {e}")
    return _docker_client


# ─── Token 过期清理 (#2) ─────────────────────────────────────
def _send_alert_to_all(text: str, markup=None):
    """向所有允许用户推送告警消息（供后台监控线程调用）。"""
    if _bot_instance is None:
        logger.warning("bot 实例未初始化，无法发送告警")
        return
    for user_id in ALLOWED_USER_IDS:
        try:
            _bot_instance.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"发送告警到 {user_id} 失败: {e}")


# ═══════════════════════════════════════════════════════════
# Phase 3: 核心新功能
# ═══════════════════════════════════════════════════════════

# ─── 操作历史 SQLite (#16) ─────────────────────────────────
def _get_db():
    """获取 SQLite 连接，自动初始化表结构。"""
    db_dir = os.path.dirname(HISTORY_DB)
    if db_dir and not os.path.isdir(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception:
            pass
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS update_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        service TEXT NOT NULL,
        image TEXT,
        old_digest TEXT,
        new_digest TEXT,
        status TEXT,
        user_id INTEGER
    )""")
    conn.commit()
    return conn


def record_update_history(service, image, old_digest, new_digest, status, user_id=0):
    """记录一次更新操作到 SQLite。"""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO update_history (timestamp, service, image, old_digest, new_digest, status, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), service, image, old_digest, new_digest, status, user_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"记录更新历史失败: {e}")


def get_update_history(service=None, limit=10):
    """查询更新历史。"""
    try:
        conn = _get_db()
        if service:
            rows = conn.execute(
                "SELECT timestamp, service, image, old_digest, new_digest, status FROM update_history WHERE service=? ORDER BY id DESC LIMIT ?",
                (service, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT timestamp, service, image, old_digest, new_digest, status FROM update_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning(f"查询更新历史失败: {e}")
        return []


def get_last_image_tag(service):
    """获取服务上一次更新前的镜像 tag（用于回滚）。"""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT old_digest FROM update_history WHERE service=? AND old_digest IS NOT NULL ORDER BY id DESC LIMIT 1",
            (service,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ─── 定时镜像更新检查 (#14) ─────────────────────────────────
def _get_image_digest(image_tag):
    """获取本地镜像的 RepoDigest，如果没有返回 None。"""
    try:
        img = get_docker_client().images.get(image_tag)
        digests = img.attrs.get("RepoDigests", [])
        if digests:
            return digests[0].split("@")[-1] if "@" in digests[0] else None
        return None
    except Exception:
        return None


def _get_service_image(service):
    """通过 docker compose config 获取服务的镜像名。"""
    try:
        output = run_compose("config", "--image", service)
        image = output.strip().splitlines()[0].strip() if output.strip() else None
        return image
    except Exception:
        return None


def check_all_updates():
    """检查所有 compose 服务是否有可用更新。
    返回 [{"service": str, "image": str}] 列表。
    """
    updates = []
    try:
        services = list_compose_services()
    except Exception:
        return []

    for svc in services:
        if svc in UPDATE_IGNORE:
            continue
        image = _get_service_image(svc)
        if not image:
            continue
        # 只检查有 tag 的镜像（非 latest 也检查，latest 必然有更新可能）
        # 使用 docker pull + 比较 digest 的方式
        try:
            local_digest = _get_image_digest(image)
            if not local_digest:
                continue
            # 拉取远端 digest（不下载层）
            remote_digest = _inspect_remote_digest(image)
            if remote_digest and remote_digest != local_digest:
                updates.append({"service": svc, "image": image})
        except Exception as e:
            logger.debug(f"检查 {svc} 更新失败: {e}")
    return updates


def _inspect_remote_digest(image_tag):
    """获取远端 registry 的镜像 digest（通过 docker manifest inspect）。"""
    try:
        output = run_cmd(
            ["docker", "manifest", "inspect", image_tag],
            timeout=30,
        )
        # 解析 manifest 输出找 digest
        import json as _json
        data = _json.loads(output)
        # 多架构镜像
        if "manifests" in data:
            # 取第一个 manifest 的 digest
            for m in data["manifests"]:
                platform = m.get("platform", {})
                if platform.get("architecture") in ("amd64", "arm64") and platform.get("os") == "linux":
                    return m.get("digest")
            return data["manifests"][0].get("digest") if data["manifests"] else None
        return data.get("config", {}).get("digest")
    except Exception:
        # 可能没有 manifest 命令或不支持，用 buildx imagetools
        try:
            output = run_cmd(["docker", "buildx", "imagetools", "inspect", image_tag], timeout=30)
            for line in output.splitlines():
                if "Digest:" in line:
                    return line.split("Digest:")[-1].strip()
        except Exception:
            pass
        return None


def update_checker_loop():
    """后台线程：定时检查镜像更新，发现可更新时推送通知。"""
    logger.info(f"镜像更新检查线程已启动 (间隔={UPDATE_CHECK_INTERVAL // 3600}h)")
    # 启动后等待 2 分钟再做第一次检查（避免启动时 Docker 还没就绪）
    time.sleep(120)
    while True:
        try:
            updates = check_all_updates()
            if updates:
                logger.info(f"发现 {len(updates)} 个可更新服务")
                lines = ["🔔 发现以下服务有可用更新：\n"]
                for u in updates:
                    lines.append(f"  ✅ {u['service']:20} {u['image']}")

                lines.append(f"\n共 {len(updates)} 个服务可更新。")

                # 构建一键更新按钮
                keyboard = None
                if _bot_instance:
                    rows = []
                    # 全部更新按钮
                    rows.append([InlineKeyboardButton(
                        f"🚀 全部更新（{len(updates)} 个）",
                        callback_data=callback_token("confirm:svc_update_all", "-"),
                    )])
                    # 逐个更新按钮
                    for u in updates[:8]:
                        rows.append([InlineKeyboardButton(
                            f"更新 {u['service']}",
                            callback_data=callback_token("confirm:svc_update", u["service"]),
                        )])
                    rows.append([InlineKeyboardButton("忽略本次", callback_data="noop")])
                    keyboard = InlineKeyboardMarkup(rows)

                _send_alert_to_all("\n".join(lines), keyboard)
            else:
                logger.debug("所有服务均为最新版本")
        except Exception as e:
            logger.warning(f"更新检查异常: {e}", exc_info=True)

        time.sleep(UPDATE_CHECK_INTERVAL)


# ─── 长操作异步化 (#10) ──────────────────────────────────────
def _async_execute(update: Update, action: str, target: str):
    """在后台线程执行长操作，完成后推送结果。"""
    global _active_ops
    chat_id = update.effective_chat.id
    op_desc = f"{action}({target})"
    _active_ops[chat_id] = op_desc
    logger.info(f"异步操作已启动: {op_desc}")

    try:
        output = _do_action(action, target)
        logger.info(f"异步操作完成: {op_desc}")
        if _bot_instance:
            body = output[:MESSAGE_LIMIT]
            _bot_instance.send_message(
                chat_id=chat_id,
                text=f"✅ 操作完成：{op_desc}\n\n{body}",
                disable_web_page_preview=True,
            )
    except Exception as e:
        logger.error(f"异步操作失败: {op_desc}: {e}", exc_info=True)
        if _bot_instance:
            _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ 操作失败：{op_desc}\n\n{e}",
                disable_web_page_preview=True,
            )
    finally:
        _active_ops.pop(chat_id, None)
        _operation_lock.release()


def _do_action(action: str, target: str) -> str:
    """实际执行操作的纯函数（不含锁逻辑），返回输出。"""
    if action == "svc_start":
        return run_compose("start", target)
    elif action == "svc_stop":
        return run_compose("stop", target)
    elif action == "svc_restart":
        return run_compose("restart", target)
    elif action == "svc_update":
        old_digest = _get_image_digest(_get_service_image(target) or "")
        output = run_compose("pull", target, timeout=PULL_TIMEOUT) + "\n" + run_compose("up", "-d", target)
        new_digest = _get_image_digest(_get_service_image(target) or "")
        record_update_history(target, _get_service_image(target), old_digest, new_digest, "updated")
        return output
    elif action == "svc_update_all":
        services = list_compose_services()
        output = run_compose("pull", timeout=PULL_TIMEOUT) + "\n" + run_compose("up", "-d")
        for svc in services:
            img = _get_service_image(svc) or ""
            new_d = _get_image_digest(img)
            record_update_history(svc, img, None, new_d, "updated_all")
        return output
    elif action == "ctr_start":
        return run_cmd(["docker", "start", target])
    elif action == "ctr_stop":
        return run_cmd(["docker", "stop", target])
    elif action == "ctr_restart":
        return run_cmd(["docker", "restart", target])
    elif action == "ctr_update":
        return update_container_by_name(target)
    elif action == "docker_prune":
        return run_cmd(["docker", "system", "prune", "-f"])
    elif action == "cleanup_safe":
        return run_cmd(["docker", "system", "prune", "-f"])
    elif action == "cleanup_standard":
        return run_cmd(["docker", "system", "prune", "-af"])
    elif action == "cleanup_deep":
        return run_cmd(["docker", "system", "prune", "-af", "--volumes"])
    elif action == "volume_prune":
        return run_cmd(["docker", "volume", "prune", "-f"])
    elif action == "image_prune":
        return run_cmd(["docker", "image", "prune", "-f"])
    elif action == "service_remove":
        return run_compose("rm", "-sf", target)
    elif action == "container_remove":
        return run_cmd(["docker", "rm", "-f", target])
    elif action == "image_remove":
        return run_cmd(["docker", "rmi", target])
    elif action == "volume_remove":
        return run_cmd(["docker", "volume", "rm", target])
    elif action == "network_prune":
        return run_cmd(["docker", "network", "prune", "-f"])
    elif action == "container_run":
        return run_cmd(shlex.split(target))
    elif action in ("batch_start", "batch_stop", "batch_restart"):
        return _do_batch_action(action, target)
    else:
        return f"未知操作：{action}"


# ─── Docker 事件监听 (#11) ───────────────────────────────────
def docker_event_monitor():
    """后台线程：监听 Docker 事件，容器异常退出 / OOM / 健康检查失败时告警。"""
    logger.info("Docker 事件监听线程已启动")
    while True:
        try:
            client = get_docker_client()
            for event in client.events(
                decode=True,
                filters={"event": ["die", "oom", "health_status: unhealthy"]},
            ):
                _handle_docker_event(event)
        except Exception as e:
            logger.warning(f"事件监听异常，10 秒后重连: {e}", exc_info=True)
            time.sleep(10)


def _handle_docker_event(event: dict):
    """处理单个 Docker 事件。"""
    status = event.get("status", "")
    attrs = event.get("Actor", {}).get("Attributes", {})
    name = attrs.get("name", "unknown")

    # 忽略指定容器
    if name in EVENT_IGNORE:
        return

    # 忽略 bot 自身
    if name == "docker-tg-bot":
        return

    if status == "die":
        exit_code = attrs.get("exitCode", "?")
        # 正常退出（exit code 0）只记录日志
        if str(exit_code) == "0":
            logger.info(f"容器 {name} 正常退出 (exit=0)，不发送告警")
            return

        # 获取最后日志
        log_tail = ""
        try:
            container = get_docker_client().containers.get(name)
            log_bytes = container.logs(tail=5)
            log_tail = log_bytes.decode("utf-8", errors="replace")[:300].strip()
        except Exception:
            pass

        # 判断退出原因
        oom_killed = attrs.get("OOMKilled", "false") == "true"
        emoji = "🔴" if oom_killed else "⚠️"
        reason = "OOM Killed（内存不足）" if oom_killed else f"退出码 {exit_code}"

        msg = (
            f"{emoji} 容器异常退出\n"
            f"容器：{name}\n"
            f"原因：{reason}\n"
        )
        if log_tail:
            msg += f"最后日志：\n{log_tail}\n"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"重启 {name}", callback_data=callback_token("confirm:ctr_restart", name))]]
        )
        _send_alert_to_all(msg, keyboard)
        logger.warning(f"Docker 事件告警: {name} {reason}")

    elif status == "oom":
        _send_alert_to_all(f"🔴 容器 {name} 被 OOM Killed！内存不足。")
        logger.warning(f"Docker OOM 事件: {name}")

    elif "health_status" in status or status == "health_status":
        health = attrs.get("healthStatus", attrs.get("status", ""))
        if "unhealthy" in str(health).lower():
            _send_alert_to_all(f"🔴 容器 {name} 健康检查失败：{health}")
            logger.warning(f"Docker 健康检查失败: {name}")


# ─── 资源阈值监控 (#12) ──────────────────────────────────────
_last_alert = {}  # alert_type -> timestamp


def _check_and_alert(alert_type: str, message: str, markup=None):
    """发送告警，但受 ALERT_COOLDOWN 冷却期限制。"""
    now = time.time()
    last = _last_alert.get(alert_type, 0)
    if now - last < ALERT_COOLDOWN:
        return
    _last_alert[alert_type] = now
    _send_alert_to_all(message, markup)
    logger.warning(f"资源告警 [{alert_type}]: {message[:80]}")


def resource_monitor():
    """后台线程：定期检查 CPU/内存/磁盘，超过阈值时告警。"""
    logger.info(f"资源监控线程已启动 (间隔={RESOURCE_CHECK_INTERVAL}s)")
    while True:
        time.sleep(RESOURCE_CHECK_INTERVAL)
        try:
            # 磁盘
            disk = psutil.disk_usage("/")
            if disk.percent >= DISK_THRESHOLD:
                free_gb = disk.free / 1024**3
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🧹 快速清理", callback_data="menu:cleanup")]]
                )
                _check_and_alert(
                    "disk",
                    f"🔴 磁盘空间不足！\n使用率：{disk.percent:.1f}%\n剩余：{free_gb:.1f} GB\n建议清理不需要的镜像和容器。",
                    keyboard,
                )

            # 内存
            mem = psutil.virtual_memory()
            if mem.percent >= MEM_THRESHOLD:
                _check_and_alert(
                    "mem",
                    f"🔴 内存不足！\n使用率：{mem.percent:.1f}%\n"
                    f"已用：{mem.used / 1024**3:.1f} GB / {mem.total / 1024**3:.1f} GB",
                )

            # CPU（采样 5 秒以获得更稳定的读数）
            cpu = psutil.cpu_percent(interval=5)
            if cpu >= CPU_THRESHOLD:
                _check_and_alert(
                    "cpu",
                    f"🟡 CPU 使用率过高：{cpu:.1f}%\n"
                    f"负载：{os.getloadavg()[0]:.2f}",
                )

        except Exception as e:
            logger.warning(f"资源监控异常: {e}", exc_info=True)


def cleanup_expired_tokens():
    """后台线程：定期清理过期的 token，防止内存泄漏。"""
    while True:
        time.sleep(TOKEN_CLEANUP_INTERVAL)
        try:
            now = time.time()
            expired_cb = [k for k, v in CALLBACK_ACTIONS.items() if now - v["created"] > CALLBACK_TOKEN_TTL]
            expired_pa = [k for k, v in PENDING_ACTIONS.items() if now - v["created"] > PENDING_TOKEN_TTL]
            for k in expired_cb:
                CALLBACK_ACTIONS.pop(k, None)
            for k in expired_pa:
                PENDING_ACTIONS.pop(k, None)
            if expired_cb or expired_pa:
                logger.debug(f"清理过期 token: callback={len(expired_cb)}, pending={len(expired_pa)}")
        except Exception as e:
            logger.warning(f"Token 清理线程异常: {e}", exc_info=True)


def restricted(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user
        if not user or user.id not in ALLOWED_USER_IDS:
            reply(update, "无操作权限。")
            logger.warning(f"未授权访问: user_id={user.id if user else '?'}, username={user.username if user else '?'}")
            return
        logger.info(f"用户 {user.id} ({user.username or '?'}) 调用 {func.__name__}")
        try:
            return func(update, context)
        except ValueError as e:
            logger.info(f"{func.__name__} 业务异常: {e}")
            reply(update, str(e))
        except Exception as e:
            logger.error(f"{func.__name__} 未捕获异常: {e}", exc_info=True)
            reply(update, f"操作失败：{e}")

    return wrapper


# ─── 消息发送：callback 时优先编辑原消息 (#6) ─────────────────
def reply(update: Update, text: str, markup=None, parse_mode=None):
    """发送消息。callback_query 场景优先编辑原消息，减少刷屏。"""
    if update.callback_query:
        try:
            update.callback_query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True, parse_mode=parse_mode)
            return
        except Exception:
            # 消息太旧或内容相同无法编辑，降级为新消息
            pass
        update.callback_query.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True, parse_mode=parse_mode)
    elif update.message:
        update.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True, parse_mode=parse_mode)


def edit_or_reply(update: Update, text: str, markup=None):
    """编辑 callback 消息；非 callback 场景发送新消息。"""
    if update.callback_query:
        try:
            update.callback_query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            reply(update, text, markup)
    else:
        reply(update, text, markup)


def send_block(update: Update, title: str, body: str):
    """发送操作结果（始终新消息，保留历史记录）。"""
    body = body or "(无输出)"
    chunks = [body[i : i + MESSAGE_LIMIT] for i in range(0, len(body), MESSAGE_LIMIT)]
    for index, chunk in enumerate(chunks):
        suffix = f" ({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        _send_new_message(update, f"{title}{suffix}\n{chunk}")


def _send_new_message(update: Update, text: str, markup=None):
    """强制发送新消息（不编辑原消息）。"""
    if update.callback_query:
        update.callback_query.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
    elif update.message:
        update.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


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
    if not item or time.time() - item["created"] > CALLBACK_TOKEN_TTL:
        raise ValueError("按钮已过期，请重新打开菜单。")
    return item["action"], item["target"]


def run_cmd(cmd, cwd=None, timeout=COMMAND_TIMEOUT):
    logger.debug(f"执行命令: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
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
        logger.warning(f"命令失败 (exit={result.returncode}): {' '.join(cmd) if isinstance(cmd, list) else cmd}")
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

def run_compose(*args, timeout=COMMAND_TIMEOUT):
    return run_cmd(compose_cmd(*args), cwd=DOCKER_COMPOSE_DIR, timeout=timeout)


def safe_arg(value: str) -> str:
    if not value or any(ch in value for ch in "\n\r;&|`$<>"):
        raise ValueError("参数包含不安全字符，请只使用服务名、容器名、镜像名或普通参数。")
    return value


def require_arg(context: CallbackContext, usage: str):
    if not context.args:
        raise ValueError(f"缺少参数。用法：{usage}")
    return safe_arg(context.args[0])


def get_container_rows(all_containers=True):
    containers = get_docker_client().containers.list(all=all_containers)
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
                InlineKeyboardButton("📊 统计", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton("🔔 检查更新", callback_data="menu:check_updates"),
                InlineKeyboardButton("🧹 清理资源", callback_data="menu:cleanup"),
            ],
            [InlineKeyboardButton("命令帮助", callback_data="menu:help")],
        ]
    )


def services_menu():
    services = list_compose_services()
    rows = []
    total = len(services)
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
    if total > 20:
        rows.append([InlineKeyboardButton(f"⚠️ 仅显示前 20 个服务（共 {total} 个）", callback_data="noop")])
    rows.append([InlineKeyboardButton("全部更新", callback_data=callback_token("confirm:svc_update_all", "-"))])
    rows.append([InlineKeyboardButton("返回", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def container_menu():
    rows = []
    containers = get_docker_client().containers.list(all=True)
    total = len(containers)
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
    if total > 20:
        rows.append([InlineKeyboardButton(f"⚠️ 仅显示前 20 个容器（共 {total} 个）", callback_data="noop")])
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


# ─── 分级清理菜单 (#8) ───────────────────────────────────────
def cleanup_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 安全清理（悬空镜像+停止容器）", callback_data=callback_token("confirm:cleanup_safe", "-"))],
            [InlineKeyboardButton("🟡 标准清理（+未使用镜像）", callback_data=callback_token("confirm:cleanup_standard", "-"))],
            [InlineKeyboardButton("🔴 深度清理（+未使用卷+构建缓存）", callback_data=callback_token("confirm:cleanup_deep", "-"))],
            [InlineKeyboardButton("清理未使用卷", callback_data="confirm:volume_prune:-")],
            [InlineKeyboardButton("清理未使用网络", callback_data="confirm:network_prune:-")],
            [InlineKeyboardButton("📊 查看可清理空间", callback_data="menu:cleanup_preview")],
            [InlineKeyboardButton("返回", callback_data="menu:main")],
        ]
    )


def cleanup_preview_text():
    """获取可清理资源预览 (#8)。"""
    try:
        df = get_docker_client().df()
        dangling_size = sum(i.get("Size", 0) for i in df.get("Images", []) if not i.get("Containers"))
        unused_images = [i for i in df.get("Images", []) if not i.get("Containers")]
        unused_image_size = sum(i.get("Size", 0) for i in unused_images)
        stopped = [c for c in df.get("Containers", []) if c.get("State") != "running"]
        stopped_size = sum(c.get("SizeRootFs", 0) for c in stopped)
        unused_volumes = [v for v in df.get("Volumes", []) if not v.get("Containers") or v.get("Containers") == 0]
        build_cache_size = sum(b.get("Size", 0) for b in df.get("BuildCache", []))

        def fmt_size(n):
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if n < 1024:
                    return f"{n:.1f} {unit}"
                n /= 1024

        lines = [
            "🧹 Docker 清理预览",
            "",
            f"悬空/未使用镜像：{len(unused_images)} 个，{fmt_size(unused_image_size)}",
            f"停止的容器：{len(stopped)} 个，{fmt_size(stopped_size)}",
            f"未使用卷：{len(unused_volumes)} 个",
            f"构建缓存：{fmt_size(build_cache_size)}",
            "",
            "选择清理级别：",
            "🟢 安全：悬空镜像 + 停止容器 + 未使用网络",
            f"🟡 标准：+ 所有未使用镜像（共 {fmt_size(unused_image_size)}）",
            "🔴 深度：+ 未使用卷 + 构建缓存",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"获取清理预览失败：{e}"


def list_compose_services():
    try:
        output = run_compose("config", "--services")
        return [line.strip() for line in output.splitlines() if line.strip()]
    except Exception:
        containers = get_docker_client().containers.list(all=True)
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
    if data == "noop":
        return
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
    if data == "menu:check_updates":
        check_updates_cmd(update, context)
        return
    if data == "menu:stats":
        stats_cmd(update, context)
        return
    if data == "menu:cleanup_preview":
        edit_or_reply(update, cleanup_preview_text(), cleanup_menu())
        return
    if token_action == "svc_view":
        service = token_target
        output = run_compose("ps", service)
        send_block(update, f"{service} 状态", output)
        return
    if token_action == "ctr_view":
        name = token_target
        c = get_docker_client().containers.get(name)
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
    # ─── 确认操作处理 ────────────────────────────────────────
    confirm_action = None
    confirm_target = None
    if token_action and token_action.startswith("confirm:"):
        confirm_action = token_action.split(":", 1)[1]
        confirm_target = token_target
    elif data.startswith("confirm:"):
        parts = data.split(":", 2)
        if len(parts) >= 3:
            _, confirm_action, confirm_target = parts
        elif len(parts) == 2:
            _, confirm_action = parts
            confirm_target = "-"

    if confirm_action or data.startswith("confirm:"):
        action = confirm_action
        target = confirm_target if confirm_target else "-"
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
            "cleanup_safe": "安全清理",
            "cleanup_standard": "标准清理",
            "cleanup_deep": "深度清理",
            "batch_start": "批量启动",
            "batch_stop": "批量停止",
            "batch_restart": "批量重启",
        }
        edit_or_reply(update, f"确认执行：{labels.get(action, action)} {target if target != '-' else ''}", confirmation_keyboard(action, target, labels.get(action, action)))
        return
    if data.startswith("do:"):
        token = data.split(":", 1)[1]
        pending = PENDING_ACTIONS.pop(token, None)
        if not pending or time.time() - pending["created"] > PENDING_TOKEN_TTL:
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


# ─── 操作执行：带并发锁 (#9) + 日志 (#5) + 异步化 (#10) ──────
def execute_confirmed_action(update: Update, action: str, target: str):
    # 尝试获取锁，防止并发冲突操作
    if not _operation_lock.acquire(blocking=False):
        reply(update, "⚠️ 有其他操作正在执行中，请等待完成后再试。", main_menu())
        return

    chat_id = update.effective_chat.id
    if chat_id in _active_ops:
        _operation_lock.release()
        reply(update, "⚠️ 你有一个操作正在后台执行中。", main_menu())
        return

    # 长操作异步执行
    if action in LONG_OPERATIONS:
        op_name = f"{action}({target})"
        reply(update, f"⏳ {op_name} 已提交后台执行，完成后会通知你。", main_menu())
        threading.Thread(
            target=_async_execute,
            args=(update, action, target),
            daemon=True,
        ).start()
        return

    # 短操作同步执行
    try:
        logger.info(f"执行操作: action={action}, target={target}")
        output = _do_action(action, target)
        logger.info(f"操作完成: action={action}, target={target}")
        send_block(update, "执行完成", output)
    except Exception as e:
        logger.error(f"操作失败: action={action}, target={target}: {e}", exc_info=True)
        reply(update, f"执行失败：{e}")
    finally:
        _operation_lock.release()


def update_container_by_name(name: str) -> str:
    container = get_docker_client().containers.get(name)
    compose_service = container.labels.get("com.docker.compose.service")
    compose_project = container.labels.get("com.docker.compose.project")
    image = container.image.tags[0] if container.image.tags else container.image.short_id
    if compose_service and compose_project:
        return (
            f"检测到 compose 容器，按服务更新：{compose_service}\n"
            + run_compose("pull", compose_service, timeout=PULL_TIMEOUT)
            + "\n"
            + run_compose("up", "-d", compose_service)
        )
    pull_output = run_cmd(["docker", "pull", image], timeout=PULL_TIMEOUT) if ":" in image and not image.startswith("sha256:") else f"镜像 {image} 没有可拉取的标签，跳过 pull。"
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
    version = get_docker_client().version()
    info = get_docker_client().info()
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
    for image in get_docker_client().images.list():
        tags = ", ".join(image.tags) if image.tags else "<none>"
        size = image.attrs.get("Size", 0) / 1024 / 1024
        rows.append(f"{image.short_id:18} {size:10.1f} MB  {tags}")
    send_block(update, "Docker 镜像列表", "\n".join(rows) or "暂无镜像")


@restricted
def list_volumes(update: Update, context: CallbackContext):
    volumes = get_docker_client().volumes.list()
    rows = []
    for volume in volumes:
        rows.append(f"{volume.name:36} {volume.attrs.get('Driver', '-')}")
    send_block(update, "Docker 存储卷", "\n".join(rows) or "暂无存储卷")


@restricted
def list_networks(update: Update, context: CallbackContext):
    networks = get_docker_client().networks.list()
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
    send_block(update, f"拉取镜像 {service or '全部服务'}", run_compose(*args, timeout=PULL_TIMEOUT))


@restricted
def service_update(update: Update, context: CallbackContext):
    service = safe_arg(context.args[0]) if context.args else None
    pull_args = ["pull"] + ([service] if service else [])
    up_args = ["up", "-d"] + ([service] if service else [])
    output = run_compose(*pull_args, timeout=PULL_TIMEOUT) + "\n" + run_compose(*up_args)
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
    c = get_docker_client().containers.get(name)
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


# ─── 安全获取嵌套字典值 (#3) ─────────────────────────────────
def _safe_get(data, *keys, default=0):
    """安全获取嵌套字典值，避免 KeyError。"""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key, default)
    return data if data is not None else default


@restricted
def container_stats(update: Update, context: CallbackContext):
    name = require_arg(context, "/container_stats <容器名>")
    c = get_docker_client().containers.get(name)
    stats = c.stats(stream=False)

    # ─── 使用安全访问，防止 KeyError (#3) ──────────────────
    cpu_usage = _safe_get(stats, "cpu_stats", "cpu_usage", "total_usage", default=0)
    precpu_usage = _safe_get(stats, "precpu_stats", "cpu_usage", "total_usage", default=0)
    cpu_delta = cpu_usage - precpu_usage

    system_usage = _safe_get(stats, "cpu_stats", "system_cpu_usage", default=0)
    pre_system = _safe_get(stats, "precpu_stats", "system_cpu_usage", default=0)
    system_delta = system_usage - pre_system

    cpu_count = (
        _safe_get(stats, "cpu_stats", "online_cpus", default=0)
        or len(_safe_get(stats, "cpu_stats", "cpu_usage", "percpu_usage", default=[]) or [])
        or 1
    )
    cpu = (cpu_delta / system_delta * cpu_count * 100) if system_delta > 0 else 0

    mem_stats = stats.get("memory_stats", {})
    mem_used = mem_stats.get("usage", 0)
    mem_limit = mem_stats.get("limit", 1)

    network_rx = sum(n.get("rx_bytes", 0) for n in stats.get("networks", {}).values())
    network_tx = sum(n.get("tx_bytes", 0) for n in stats.get("networks", {}).values())
    body = "\n".join(
        [
            f"CPU：{cpu:.2f}%",
            f"内存：{mem_used / 1024 / 1024:.2f} MB / {mem_limit / 1024 / 1024:.2f} MB ({mem_used / mem_limit * 100:.2f}%)" if mem_limit > 0 else f"内存：{mem_used / 1024 / 1024:.2f} MB",
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
    v = get_docker_client().volumes.get(volume)
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
            f"日志级别：{logging.getLevelName(logger.getEffectiveLevel())}",
            f"待处理 token：callback={len(CALLBACK_ACTIONS)}, pending={len(PENDING_ACTIONS)}",
            f"后台操作：{len(_active_ops)} 个进行中",
            f"事件监听：{'✅开' if ENABLE_EVENT_MONITOR else '❌关'}",
            f"资源监控：{'✅开' if ENABLE_RESOURCE_MONITOR else '❌关'}"
            + (f"（磁盘>{DISK_THRESHOLD}% 内存>{MEM_THRESHOLD}% CPU>{CPU_THRESHOLD}%）" if ENABLE_RESOURCE_MONITOR else ""),
            f"更新检查：{'✅开' if ENABLE_UPDATE_CHECKER else '❌关'}"
            + (f"（间隔 {UPDATE_CHECK_INTERVAL // 3600}h）" if ENABLE_UPDATE_CHECKER else ""),
            f"Compose 项目：{len(_COMPOSE_PROJECTS)} 个",
            f"更新历史：{len(get_update_history())} 条",
        ]
    )
    send_block(update, "机器人运行信息", body)


@restricted
def bot_restart(update: Update, context: CallbackContext):
    reply(update, "机器人即将退出。如果已配置 Docker restart: unless-stopped 或 systemd Restart=always，会自动拉起新进程。")
    logger.info("收到 bot_restart 命令，发送 SIGTERM")
    os.kill(os.getpid(), signal.SIGTERM)


# ═══════════════════════════════════════════════════════════
# Phase 3: 新命令
# ═══════════════════════════════════════════════════════════

@restricted
def check_updates_cmd(update: Update, context: CallbackContext):
    """手动触发更新检查 (#14)。"""
    reply(update, "⏳ 正在检查镜像更新，请稍候...")
    try:
        updates = check_all_updates()
        if not updates:
            send_block(update, "更新检查", "✅ 所有服务均为最新版本，无需更新。")
            return

        lines = [f"🔔 发现 {len(updates)} 个可更新服务：\n"]
        for u in updates:
            lines.append(f"  ✅ {u['service']:20} {u['image']}")

        # 构建按钮
        rows = []
        rows.append([InlineKeyboardButton(
            f"🚀 全部更新（{len(updates)} 个）",
            callback_data=callback_token("confirm:svc_update_all", "-"),
        )])
        for u in updates[:8]:
            rows.append([InlineKeyboardButton(
                f"更新 {u['service']}",
                callback_data=callback_token("confirm:svc_update", u["service"]),
            )])
        rows.append([InlineKeyboardButton("返回主菜单", callback_data="menu:main")])
        keyboard = InlineKeyboardMarkup(rows)

        _send_new_message(update, "\n".join(lines), keyboard)
    except Exception as e:
        send_block(update, "更新检查", f"检查失败：{e}")


@restricted
def history_cmd(update: Update, context: CallbackContext):
    """查看更新历史 (#16)。"""
    service = safe_arg(context.args[0]) if context.args else None
    rows = get_update_history(service, limit=15)
    if not rows:
        send_block(update, "更新历史", "暂无更新记录。")
        return

    lines = []
    for ts, svc, img, old_d, new_d, status in rows:
        digest_short = (new_d or "?")[:19]
        lines.append(f"{ts}  {svc:20} {status:12} {digest_short}")
    title = f"更新历史 — {service}" if service else "更新历史（全部）"
    send_block(update, title, "\n".join(lines))


@restricted
def rollback_cmd(update: Update, context: CallbackContext):
    """回滚服务到上一个版本 (#16)。"""
    service = require_arg(context, "/rollback <服务名>")
    old_tag = get_last_image_tag(service)
    if not old_tag:
        send_block(update, "回滚", f"没有找到 {service} 的历史版本记录，无法回滚。")
        return

    reply(
        update,
        f"将回滚服务 {service}\n目标版本：{old_tag[:50]}\n\n"
        f"回滚将通过 docker pull 旧镜像 + compose up 实现。",
        confirmation_keyboard("svc_update", service, f"回滚 {service}"),
    )


@restricted
def compose_projects_cmd(update: Update, context: CallbackContext):
    """列出所有 compose 项目 (#15)。"""
    if len(_COMPOSE_PROJECTS) <= 1:
        send_block(update, "Compose 项目", f"当前项目：{_COMPOSE_PROJECTS[0]['name']}\n目录：{_COMPOSE_PROJECTS[0]['dir']}")
        return

    lines = [f"Compose 项目（共 {len(_COMPOSE_PROJECTS)} 个）\n"]
    for p in _COMPOSE_PROJECTS:
        lines.append(f"📦 {p['name']}")
        lines.append(f"   目录：{p['dir']}")
        if p["file"]:
            lines.append(f"   文件：{p['file']}")
        lines.append("")
    send_block(update, "Compose 项目列表", "\n".join(lines))


# ═══════════════════════════════════════════════════════════
# Phase 4: 高级功能
# ═══════════════════════════════════════════════════════════

# ─── #18 容器 exec + 实时日志流 ─────────────────────────────
@restricted
def container_exec_cmd(update: Update, context: CallbackContext):
    """在容器内执行命令。"""
    if len(context.args) < 2:
        raise ValueError("用法：/container_exec <容器名> <命令>")
    name = safe_arg(context.args[0])
    command = " ".join(context.args[1:])

    # 安全检查：禁止危险命令
    cmd_lower = command.lower()
    for forbidden in EXEC_FORBIDDEN:
        if forbidden in cmd_lower:
            raise ValueError(f"命令包含被禁止的操作：{forbidden}")

    reply(update, f"⏳ 在容器 {name} 中执行：{command}")
    try:
        container = get_docker_client().containers.get(name)
        exit_code, output = container.exec_run(
            cmd=shlex.split(command),
            workdir="/",
        )
        result = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        if exit_code != 0:
            result = f"退出码：{exit_code}\n\n{result}"
        send_block(update, f"exec {name}", result)
    except Exception as e:
        send_block(update, f"exec {name}", f"执行失败：{e}")


@restricted
def container_logs_cmd(update: Update, context: CallbackContext):
    """查看非 compose 容器日志。"""
    name = require_arg(context, "/container_logs <容器名> [行数]")
    tail = safe_arg(context.args[1]) if len(context.args) > 1 else str(LOG_TAIL)
    container = get_docker_client().containers.get(name)
    logs = container.logs(tail=int(tail)).decode("utf-8", errors="replace")
    send_block(update, f"{name} 日志（最后 {tail} 行）", logs or "(无日志)")


@restricted
def container_logstream_cmd(update: Update, context: CallbackContext):
    """实时监听容器日志 N 秒。"""
    name = require_arg(context, "/container_logstream <容器名>")
    duration = LOG_STREAM_DURATION
    chat_id = update.effective_chat.id
    reply(update, f"📡 开始监听 {name} 日志 {duration} 秒...")

    def _stream_worker():
        try:
            container = get_docker_client().containers.get(name)
            buffer = []
            for line in container.logs(stream=True, follow=True, since=int(time.time())):
                buffer.append(line.decode("utf-8", errors="replace").rstrip())
                if len(buffer) > 100:
                    buffer = buffer[-50:]
                if time.time() - start_time > duration:
                    break
            summary = "\n".join(buffer[-50:]) or "(无输出)"
            if _bot_instance:
                _bot_instance.send_message(
                    chat_id=chat_id,
                    text=f"📋 {name} 日志摘要（{duration}s）：\n\n{summary[:MESSAGE_LIMIT]}",
                    disable_web_page_preview=True,
                )
        except Exception as e:
            if _bot_instance:
                _bot_instance.send_message(chat_id=chat_id, text=f"日志监听失败：{e}")

    start_time = time.time()
    threading.Thread(target=_stream_worker, daemon=True).start()


# ─── #19 批量操作 ────────────────────────────────────────────
@restricted
def batch_menu_cmd(update: Update, context: CallbackContext):
    """批量操作入口：展示容器列表供多选。"""
    containers = get_docker_client().containers.list(all=True)
    if not containers:
        send_block(update, "批量操作", "暂无容器")
        return

    rows = []
    # 每行最多 3 个按钮
    batch = []
    for c in containers[:15]:
        batch.append(InlineKeyboardButton(
            c.name[:15],
            callback_data=callback_token("batch_select", c.name),
        ))
        if len(batch) == 3:
            rows.append(batch)
            batch = []
    if batch:
        rows.append(batch)

    rows.append([
        InlineKeyboardButton("🟢 全部启动", callback_data=callback_token("confirm:batch_start", "all")),
        InlineKeyboardButton("🔴 全部停止", callback_data=callback_token("confirm:batch_stop", "all")),
        InlineKeyboardButton("🔄 全部重启", callback_data=callback_token("confirm:batch_restart", "all")),
    ])
    rows.append([InlineKeyboardButton("返回主菜单", callback_data="menu:main")])
    reply(update, "批量操作 — 选择操作：", InlineKeyboardMarkup(rows))


def _do_batch_action(action, target):
    """执行批量操作。"""
    if target == "all":
        containers = get_docker_client().containers.list(all=True)
        names = [c.name for c in containers]
    else:
        names = [target]

    results = []
    for name in names:
        try:
            if action == "batch_start":
                run_cmd(["docker", "start", name])
            elif action == "batch_stop":
                run_cmd(["docker", "stop", name])
            elif action == "batch_restart":
                run_cmd(["docker", "restart", name])
            results.append(f"✅ {name}")
        except Exception as e:
            results.append(f"❌ {name}: {e}")
    return "\n".join(results)


# ─── #20 定时任务管理 (cron) ─────────────────────────────────
_SCHEDULED_JOBS = {}  # job_id -> {"desc": str, "schedule": str, "command": str}


@restricted
def schedule_cmd(update: Update, context: CallbackContext):
    """管理定时任务。"""
    if not context.args or context.args[0] == "list":
        if not _SCHEDULED_JOBS:
            send_block(update, "定时任务", "暂无定时任务。\n\n用法：\n/schedule add <每日HH:MM> <命令>\n/schedule remove <ID>")
            return
        lines = ["定时任务列表：\n"]
        for jid, job in _SCHEDULED_JOBS.items():
            lines.append(f"  [{jid}] {job['schedule']} → {job['command']}")
        send_block(update, "定时任务", "\n".join(lines))
        return

    sub = context.args[0]
    if sub == "add":
        if len(context.args) < 3:
            raise ValueError("用法：/schedule add <每日HH:MM> <命令>\n命令支持：update_all, cleanup_safe, cleanup_standard, cleanup_deep, restart <服务名>")
        schedule_str = safe_arg(context.args[1])
        command = " ".join(context.args[2:])
        jid = uuid.uuid4().hex[:6]
        _SCHEDULED_JOBS[jid] = {"schedule": schedule_str, "command": command}
        logger.info(f"用户 {update.effective_user.id} 添加定时任务 [{jid}]: {schedule_str} → {command}")
        send_block(update, "定时任务", f"✅ 已添加定时任务 [{jid}]\n时间：{schedule_str}\n命令：{command}\n\n注意：定时任务在 bot 重启后需要重新添加（持久化开发中）。")
    elif sub == "remove":
        if len(context.args) < 2:
            raise ValueError("用法：/schedule remove <ID>")
        jid = safe_arg(context.args[1])
        if jid in _SCHEDULED_JOBS:
            del _SCHEDULED_JOBS[jid]
            send_block(update, "定时任务", f"✅ 已删除定时任务 [{jid}]")
        else:
            raise ValueError(f"找不到任务 [{jid}]")
    else:
        raise ValueError("用法：/schedule [list|add|remove]")


def scheduled_jobs_checker():
    """后台线程：检查定时任务是否到时执行。"""
    logger.info("定时任务检查线程已启动")
    while True:
        time.sleep(60)
        try:
            now = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            for jid, job in list(_SCHEDULED_JOBS.items()):
                sched = job["schedule"]
                # 支持 "每日HH:MM" 格式
                if sched.startswith("每日") and sched[2:] == now:
                    cmd = job["command"]
                    if jid + today in _SCHEDULED_JOBS:  # 今天已执行过
                        continue
                    _SCHEDULED_JOBS[jid + today] = {"_executed": True}
                    logger.info(f"执行定时任务 [{jid}]: {cmd}")
                    _execute_scheduled_command(cmd)
        except Exception as e:
            logger.warning(f"定时任务检查异常: {e}", exc_info=True)


def _execute_scheduled_command(cmd_str):
    """执行定时任务命令。"""
    try:
        if cmd_str == "update_all":
            output = run_compose("pull") + "\n" + run_compose("up", "-d")
            _send_alert_to_all(f"⏰ 定时更新完成\n\n{output[:500]}")
        elif cmd_str == "cleanup_safe":
            output = run_cmd(["docker", "system", "prune", "-f"])
            _send_alert_to_all(f"⏰ 定时安全清理完成\n\n{output[:500]}")
        elif cmd_str == "cleanup_standard":
            output = run_cmd(["docker", "system", "prune", "-af"])
            _send_alert_to_all(f"⏰ 定时标准清理完成\n\n{output[:500]}")
        elif cmd_str == "cleanup_deep":
            output = run_cmd(["docker", "system", "prune", "-af", "--volumes"])
            _send_alert_to_all(f"⏰ 定时深度清理完成\n\n{output[:500]}")
        elif cmd_str.startswith("restart "):
            svc = cmd_str.split(" ", 1)[1]
            output = run_compose("restart", svc)
            _send_alert_to_all(f"⏰ 定时重启 {svc} 完成\n\n{output[:500]}")
        else:
            _send_alert_to_all(f"⚠️ 未知定时任务命令：{cmd_str}")
    except Exception as e:
        _send_alert_to_all(f"❌ 定时任务执行失败：{cmd_str}\n\n{e}")


# ─── #21 Compose 文件查看与验证 ──────────────────────────────
@restricted
def compose_view_cmd(update: Update, context: CallbackContext):
    """查看 compose 配置。"""
    try:
        output = run_compose("config")
        send_block(update, "Compose 配置", output)
    except Exception as e:
        send_block(update, "Compose 配置", f"获取失败：{e}")


@restricted
def compose_validate_cmd(update: Update, context: CallbackContext):
    """验证 compose 配置语法。"""
    try:
        result = run_cmd(compose_cmd("config", "-q"), cwd=DOCKER_COMPOSE_DIR)
        send_block(update, "配置验证", "✅ 配置语法有效" if not result else f"⚠️ {result}")
    except Exception as e:
        send_block(update, "配置验证", f"❌ 配置有误：{e}")


# ─── #22 统计仪表盘 ──────────────────────────────────────────
@restricted
def stats_cmd(update: Update, context: CallbackContext):
    """Docker 资源使用摘要。"""
    client = get_docker_client()
    info = client.info()
    df = client.df()

    # 容器统计
    total_ctn = info.get("Containers", 0)
    running = info.get("ContainersRunning", 0)
    stopped = info.get("ContainersStopped", 0)
    images_count = info.get("Images", 0)

    # 磁盘统计
    total_image_size = sum(i.get("Size", 0) for i in df.get("Images", []))
    dangling_images = [i for i in df.get("Images", []) if not i.get("Containers")]
    unused_image_size = sum(i.get("Size", 0) for i in dangling_images)

    def fmt_size(n):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024

    # 系统资源
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = os.getloadavg()

    # 更新历史
    history = get_update_history(limit=5)

    lines = [
        f"📊 Docker 资源摘要 — {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "🐳 容器概览：",
        f"   运行中：{running}  停止：{stopped}  总计：{total_ctn}",
        f"   镜像数：{images_count}",
        "",
        "💾 存储占用：",
        f"   镜像总大小：{fmt_size(total_image_size)}",
        f"   可清理镜像：{len(dangling_images)} 个（{fmt_size(unused_image_size)}）",
        "",
        "🖥 服务器资源：",
        f"   CPU：{cpu:.1f}%  负载：{load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f}",
        f"   内存：{mem.used / 1024**3:.1f} / {mem.total / 1024**3:.1f} GB ({mem.percent:.0f}%)",
        f"   磁盘：{disk.used / 1024**3:.1f} / {disk.total / 1024**3:.1f} GB ({disk.percent:.0f}%)",
    ]

    if history:
        lines.append("")
        lines.append("📋 最近更新：")
        for ts, svc, img, old_d, new_d, status in history[:3]:
            lines.append(f"   {ts}  {svc} ({status})")

    send_block(update, "Docker 统计", "\n".join(lines))


# ─── #23 安全增强 — 分级权限 + 审计 ──────────────────────────
def restricted_admin(func):
    """管理员权限装饰器：只允许 ADMIN_USER_IDS 中的用户。"""
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user
        if not user or user.id not in ADMIN_USER_IDS:
            reply(update, "⛔ 此操作需要管理员权限。")
            logger.warning(f"非管理员用户 {user.id} 尝试管理员操作 {func.__name__}")
            return
        return func(update, context)
    return wrapper


# 写操作集合 — readonly 用户被禁止执行
_WRITE_COMMANDS = {
    "service_start", "service_stop", "service_restart", "service_pull",
    "service_update", "service_recreate", "service_add", "service_remove",
    "scale", "container_start", "container_stop", "container_restart",
    "container_update", "container_remove", "container_run",
    "container_exec", "container_logstream",
    "image_pull", "image_remove", "image_prune",
    "volume_remove", "volume_prune", "network_prune",
    "docker_prune", "rollback", "schedule",
}


def is_readonly_user(user_id):
    """检查用户是否为只读权限。"""
    return user_id in READONLY_USER_IDS and user_id not in ADMIN_USER_IDS


def _audit_log(user_id, action, target, result="ok"):
    """记录审计日志到 SQLite。"""
    try:
        conn = _get_db()
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id INTEGER,
            action TEXT,
            target TEXT,
            result TEXT
        )""")
        conn.execute(
            "INSERT INTO audit_log (timestamp, user_id, action, target, result) VALUES (?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), user_id, action, target, result),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"审计日志写入失败: {e}")


@restricted
def audit_log_cmd(update: Update, context: CallbackContext):
    """查看审计日志（仅管理员）。"""
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        reply(update, "⛔ 此操作需要管理员权限。")
        return
    try:
        conn = _get_db()
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id INTEGER,
            action TEXT,
            target TEXT,
            result TEXT
        )""")
        rows = conn.execute(
            "SELECT timestamp, user_id, action, target, result FROM audit_log ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()
        if not rows:
            send_block(update, "审计日志", "暂无审计记录。")
            return
        lines = []
        for ts, uid, act, tgt, res in rows:
            lines.append(f"{ts}  user={uid}  {act}({tgt})  → {res}")
        send_block(update, "审计日志（最近 20 条）", "\n".join(lines))
    except Exception as e:
        send_block(update, "审计日志", f"查询失败：{e}")


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

    # Phase 3: 新命令
    dp.add_handler(CommandHandler("check_updates", check_updates_cmd))
    dp.add_handler(CommandHandler("history", history_cmd))
    dp.add_handler(CommandHandler("rollback", rollback_cmd))
    dp.add_handler(CommandHandler("compose_projects", compose_projects_cmd))

    # Phase 4: 高级功能
    dp.add_handler(CommandHandler("container_exec", container_exec_cmd))
    dp.add_handler(CommandHandler("container_logs", container_logs_cmd))
    dp.add_handler(CommandHandler("container_logstream", container_logstream_cmd))
    dp.add_handler(CommandHandler("batch", batch_menu_cmd))
    dp.add_handler(CommandHandler("schedule", schedule_cmd))
    dp.add_handler(CommandHandler("compose_view", compose_view_cmd))
    dp.add_handler(CommandHandler("compose_validate", compose_validate_cmd))
    dp.add_handler(CommandHandler("stats", stats_cmd))
    dp.add_handler(CommandHandler("audit_log", audit_log_cmd))

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
    try:
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
                BotCommand("check_updates", "检查镜像更新"),
                BotCommand("history", "查看更新历史"),
                BotCommand("rollback", "回滚服务版本"),
                BotCommand("compose_projects", "列出 compose 项目"),
                BotCommand("stats", "查看 Docker 资源摘要"),
                BotCommand("batch", "批量操作容器"),
                BotCommand("schedule", "管理定时任务"),
                BotCommand("compose_view", "查看 compose 配置"),
                BotCommand("compose_validate", "验证 compose 配置"),
                BotCommand("audit_log", "查看审计日志"),
            ]
        )
    except TelegramError as e:
        logger.warning(f"设置 Telegram 命令菜单失败，机器人将继续启动：{e}")


def telegram_request_kwargs():
    request_kwargs = {
        "connect_timeout": TELEGRAM_CONNECT_TIMEOUT,
        "read_timeout": TELEGRAM_READ_TIMEOUT,
    }
    if TELEGRAM_PROXY_URL:
        request_kwargs["proxy_url"] = TELEGRAM_PROXY_URL
    return request_kwargs


# ─── 健康检查心跳文件 (#7) ───────────────────────────────────
HEALTH_FILE = "/tmp/bot_healthy"


def heartbeat_loop():
    """后台线程：定期更新心跳文件，供 Docker HEALTHCHECK 使用。"""
    while True:
        try:
            with open(HEALTH_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        time.sleep(30)


def main():
    global _bot_instance
    if not TELEGRAM_TOKEN:
        raise RuntimeError("请在 .env 中配置 TELEGRAM_TOKEN")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("请在 .env 中配置 ALLOWED_USER_ID 或 ALLOWED_USER_IDS")

    logger.info("Docker 管理机器人启动中...")
    updater = Updater(TELEGRAM_TOKEN, use_context=True, request_kwargs=telegram_request_kwargs())
    _bot_instance = updater.bot  # 供后台监控线程使用

    add_handlers(updater.dispatcher)
    set_bot_commands(updater)
    updater.start_polling()

    # 启动后台线程
    threading.Thread(target=cleanup_expired_tokens, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    if ENABLE_EVENT_MONITOR:
        threading.Thread(target=docker_event_monitor, daemon=True).start()
    if ENABLE_RESOURCE_MONITOR:
        threading.Thread(target=resource_monitor, daemon=True).start()
    if ENABLE_UPDATE_CHECKER:
        threading.Thread(target=update_checker_loop, daemon=True).start()
    threading.Thread(target=scheduled_jobs_checker, daemon=True).start()

    logger.info("机器人已启动，开始接收消息")
    updater.idle()


if __name__ == "__main__":
    main()
