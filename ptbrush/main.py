#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   main.py
@Time    :   2024/11/05 14:38:04
@Author  :   huihuidehui
@Version :   1.0
"""

from datetime import datetime, timedelta
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from loguru import logger
import tasks as tasks
from config.config import BrushConfig, PTBrushConfig
from web.server import start_web_server_thread
import os
from db import migrate_database, db_log_sink

# 设置不打印 debug 级别的日志，最小级别为 INFO
logger.remove()  # 移除默认的 handler
logger.add(
    Path(__file__).parent / "data" / "ptbrush.log",
    rotation="10 MB",
    retention="10 days",
    level="INFO",
)
logger.add(db_log_sink, level="INFO")


def check_work_time(brush_config: BrushConfig):
    """检查当前是否在工作时间内"""

    if not brush_config.is_work_time():
        logger.info("当前不在工作时间范围内，跳过任务执行")
        return False
    return True


def run_if_work_time(func):
    """只在工作时间内运行的装饰器"""

    def wrapper():
        config = PTBrushConfig()
        if check_work_time(config.brush):
            func()

    return wrapper


def main():
    # 初始化配置文件
    PTBrushConfig.init_config()

    # 确保数据库结构是最新的
    migrate_database()

    # Start web server
    web_port = int(os.environ.get("WEB_PORT", 8000))
    web_thread = start_web_server_thread(port=web_port)
    logger.info(f"Web界面已启动，端口: {web_port}")

    executors = {"default": ThreadPoolExecutor(max_workers=6)}
    job_defaults = {"coalesce": True, "max_instances": 1}
    scheduler = BlockingScheduler(executors=executors, job_defaults=job_defaults)

    # 每10分钟执行一次刷流任务，受刷流任务工作时间设置
    scheduler.add_job(run_if_work_time(tasks.brush), "cron", minute="*/10")

    # 每10分钟检查一次即将过期的种子
    scheduler.add_job(tasks.clean_will_expire_torrents, "cron", minute="*/10")

    # 每15秒记录一次QB状态
    scheduler.add_job(tasks.fetch_qb_status, "cron", second="*/15")

    # 每分钟记录一次QB中的种子状态
    scheduler.add_job(tasks.fetch_qb_torrents, "cron", minute="*")

    # 每15分钟抓取一次PT站的种子
    config = PTBrushConfig()
    scheduler.add_job(
        tasks.fetch_pt_torrents, "cron", minute=f"*/{config.brush.pt_fetch_interval}"
    )

    # 每3分钟清理一次长时间无活跃的种子
    scheduler.add_job(tasks.clean_long_time_no_activate_torrents, "cron", minute="*/3")

    # 每5分钟检查一次磁盘空间并清理
    scheduler.add_job(tasks.check_disk_space_and_cleanup, "cron", minute="*/5")

    # 每小时清理一次过期的系统日志（只保留24小时）
    scheduler.add_job(tasks.clean_db_logs, "cron", hour="*")

    logger.info(f"开始运行，稍后你可以在日志文件中查看日志，观察运行情况...")
    logger.info(f"Web界面已启动，访问 http://your-server-ip:{web_port} 查看刷流状态")

    # 启动时顺序执行一次所有任务
    logger.info("正在执行启动时自检任务...")

    # 启动时任务：注册为一次性Job，由Scheduler的线程池并行执行
    logger.info("正在注册启动时自检任务...")

    startup_tasks = [
        ("同步QB状态", tasks.fetch_qb_status),
        ("同步QB种子", tasks.fetch_qb_torrents),
        ("清理过期日志", tasks.clean_db_logs),
        ("抓取PT新种", tasks.fetch_pt_torrents),
        ("执行刷流", run_if_work_time(tasks.brush)),
        ("清理过期种子", tasks.clean_will_expire_torrents),
        ("清理无活动种子", tasks.clean_long_time_no_activate_torrents),
        ("检查磁盘空间", tasks.check_disk_space_and_cleanup),
    ]

    now = datetime.now() + timedelta(seconds=2)  # 延后2秒执行，确保scheduler启动
    for name, task in startup_tasks:
        scheduler.add_job(task, "date", run_date=now, name=f"Startup-{name}")

    logger.info("启动任务已注册，Scheduler启动后将并行执行")

    scheduler.start()


if __name__ == "__main__":
    main()
