#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   __init__.py
@Time    :   2024/11/04 10:34:05
@Author  :   huihuidehui
@Version :   1.0
"""

# here put the import lib
from datetime import datetime, timedelta
from time import sleep
from loguru import logger
from tasks.services import PtTorrentService, QBTorrentService, BrushService
from db import SystemMessage


# 给所有任务加一个装饰器，进行错误捕获
# 给所有任务加一个装饰器，进行错误捕获
def catch_error(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # 这里还需要打印出函数名称
            logger.error(f"任务执行出错: {str(e)}")

    return wrapper


# 抓取PT站种子
@catch_error
def fetch_pt_torrents():
    PtTorrentService().fetcher()


# 抓取QB中的种子信息
@catch_error
def fetch_qb_torrents():
    QBTorrentService().fetcher()


# 抓取QB的信息
@catch_error
def fetch_qb_status():
    QBTorrentService().fetch_qb_status()


# 刷流
@catch_error
def brush():
    add_torrent_count = BrushService().brush()
    if add_torrent_count > 0:
        logger.info(f"1分钟后,开始拆包任务...")
        sleep(60)
        QBTorrentService().torrent_thinned()


# 清理长时间没有上传的种子
@catch_error
def clean_long_time_no_activate_torrents():
    QBTorrentService().clean_long_time_no_activate()


# 清理即将过期种子
@catch_error
def clean_will_expire_torrents():
    QBTorrentService().clean_will_expired()


# 对大包种子进行瘦身
@catch_error
def torrent_thinned():
    QBTorrentService().torrent_thinned()


# 检查磁盘空间并清理
@catch_error
def check_disk_space_and_cleanup():
    QBTorrentService().check_disk_space_and_cleanup()


# 清理系统的系统日志
@catch_error
def clean_db_logs(hours=24):
    cutoff = datetime.now() - timedelta(hours=hours)
    # 如果hours=0，则清理所有 (cutoff > created_time) -> created_time < now -> all past logs.
    count = SystemMessage.delete().where(SystemMessage.created_time < cutoff).execute()

    # 同时清理物理日志文件
    import glob
    import os
    from pathlib import Path

    log_dir = Path(__file__).parent.parent / "data"
    log_file = log_dir / "ptbrush.log"

    # 1. 清空主日志文件 (使用截断而非删除，防止文件占用报错)
    if log_file.exists():
        try:
            with open(log_file, "w") as f:
                f.truncate(0)
            logger.bind(category="SYSTEM").info("已清空物理日志文件 ptbrush.log")
        except Exception as e:
            logger.warning(f"清空日志文件失败: {e}")

    # 2. 删除 Loguru 生成的历史轮转日志
    rotated_patterns = ["ptbrush.log.*", "ptbrush.*.log"]
    removed_files = 0
    for pattern in rotated_patterns:
        for fpath in glob.glob(str(log_dir / pattern)):
            try:
                os.remove(fpath)
                removed_files += 1
            except Exception:
                pass

    msg = f"日志清理完成: 数据库记录 {count} 条, 物理文件 {removed_files} 个"
    if count > 0 or removed_files > 0:
        logger.bind(category="SYSTEM").info(msg)
