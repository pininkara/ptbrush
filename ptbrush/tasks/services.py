#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   task.py
@Time    :   2024/11/04 09:56:17
@Author  :   huihuidehui
@Version :   1.0
"""

from datetime import datetime, timedelta
import re
from typing import List, Optional
from loguru import logger
from config.config import PTBrushConfig, SiteModel
from model import Torrent
from db import Torrent as TorrentDB, BrushTorrent, QBStatus, SystemMessage, database
from qbittorrent import QBittorrent
from ptsite import TorrentFetch
import peewee


# 从PT站获取种子
class PtTorrentService:
    def fetcher(self):
        """
        抓取种子，并进行存储
        """
        sites = PTBrushConfig().sites
        logger.info(f"开始抓取PT站点FREE种子，准备处理{len(sites)}个站点")
        count = 0
        for site in sites:
            logger.info(f"开始处理站点:{site.name}, 正在初始化抓取器...")
            torrent_fetcher = TorrentFetch(
                site.name, cookie=site.cookie, headers=site.headers
            )
            for torrent in torrent_fetcher.free_torrents:
                logger.info(
                    f"从{site.name}抓取到种子: {torrent.name}, 大小: {torrent.size / 1024 / 1024:.2f}MB, 做种数: {torrent.seeders}, 下载数: {torrent.leechers}, 评分: {torrent.score}"
                )
                self._insert_or_update_torrent(torrent)
                count += 1
            logger.info(f"站点{site.name}处理完成，已抓取{count}个种子")
        logger.info(f"抓取PT站点FREE种子完成，本轮共抓取到{count}个种子")

    def _insert_or_update_torrent(self, torrent: Torrent):
        updated_time = datetime.now()
        logger.info(
            f"正在更新种子信息: 站点={torrent.site}, ID={torrent.id}, 名称={torrent.name}"
        )
        TorrentDB.insert(
            name=torrent.name,
            site=torrent.site,
            torrent_id=torrent.id,
            leechers=torrent.leechers,
            seeders=torrent.seeders,
            size=torrent.size,
            free_end_time=torrent.free_end_time,
            score=torrent.score,
        ).on_conflict(
            conflict_target=[TorrentDB.site, TorrentDB.torrent_id],
            update=dict(
                updated_time=updated_time,
                leechers=torrent.leechers,
                seeders=torrent.seeders,
                free_end_time=torrent.free_end_time,
                score=torrent.score,
            ),
        ).execute()


# 从qb获取种子状态、以及下载器状态、 清理临近过期的种子
class QBTorrentService:
    def __init__(self):
        self._config = PTBrushConfig()
        self._qb = QBittorrent(
            self._config.downloader.url,
            self._config.downloader.username,
            self._config.downloader.password,
        )

    def fetch_qb_status(self):
        """
        获取qb状态
        """
        qb_status = self._qb.status
        logger.info(
            f"正在记录QB状态 - 上传速度: {qb_status.upspeed / 1024 / 1024:.2f}MB/s, 下载速度: {qb_status.dlspeed / 1024 / 1024:.2f}MB/s, 剩余空间: {qb_status.free_space_size / 1024 / 1024 / 1024:.2f}GB"
        )
        QBStatus.create(
            dlspeed=qb_status.dlspeed,
            upspeed=qb_status.upspeed,
            free_space_size=qb_status.free_space_size,
            up_total_size=qb_status.up_total_size,
            dl_total_size=qb_status.dl_total_size,
        )
        pass

    def clean_will_expired(self):
        """
        清理临近过期的种子
        """
        logger.info(f"开始清理即将过期的种子")
        count = 0
        current_timestamp = datetime.now().timestamp()
        # 截止时间戳，1小时后
        # 当free时间不足1小时时，取消下载所有文件，但不删除种子，已下载的文件会继续做种.
        expire_timestamp = current_timestamp + 3600
        for torrent in self._qb.torrents:
            if torrent.completed:
                continue

            free_timestamp = torrent.free_end_time.timestamp()
            if free_timestamp > expire_timestamp:
                continue

            logger.info(
                f"删除Free即将结束的种子，种子名称:{torrent.name}, Free结束时间:{torrent.free_end_time}"
            )

            # 直接删除种子
            self._qb.cancel_download(torrent.hash)
            count += 1
            logger.bind(category="DELETE_TORRENT").info(
                f"即将过期，删除种子: {torrent.name}"
            )

        if count > 0:
            logger.info(f"清理即将过期的种子完成，本次删除种子数:{count}")
            # 记录系统消息
            logger.bind(category="DELETE_TORRENT").info(
                f"任务完成：清理即将过期的种子，共删除 {count} 个"
            )

    def fetcher(self):
        """
        获取所有正在刷流的种子，记录其信息，并同步已删除的种子状态
        """
        logger.info(f"开始抓取QB中种子状态")
        count = 0

        # 记录本次在QB中发现的种子key (site, torrent_id)
        current_qb_keys = set()

        for torrent in self._qb.torrents:
            count += 1
            current_qb_keys.add((torrent.site, torrent.torrent_id))

            torrent_db, flag = TorrentDB.get_or_create(
                site=torrent.site,
                torrent_id=torrent.torrent_id,
                defaults={
                    "name": torrent.name,
                    "brushed": True,
                    "free_end_time": torrent.free_end_time,
                },
            )
            torrent_db.brushed = True

            # 更新分数等信息，虽然这些可能不会变，但保持最新比较好
            # 这里不更新score，因为fetcher主要关注状态同步
            if flag:
                logger.info(f"发现新种子加入刷流: {torrent.name}")

            # 始终更新关键字段
            torrent_db.size = torrent.size
            torrent_db.save()

            BrushTorrent.create(
                torrent=torrent_db,
                up_total_size=torrent.up_total_size,
                upspeed=torrent.upspeed,
                dl_total_size=torrent.dl_total_size,
                dlspeed=torrent.dlspeed,
            )
            logger.info(
                f"记录种子状态: {torrent.name} - 上传速度: {torrent.upspeed / 1024 / 1024:.2f}MB/s, 下载速度: {torrent.dlspeed / 1024 / 1024:.2f}MB/s, 已上传: {torrent.up_total_size / 1024 / 1024:.2f}MB, 已下载: {torrent.dl_total_size / 1024 / 1024:.2f}MB"
            )

        # 同步逻辑: 将数据库中标记为brushed=True但不在本次QB列表中的种子，标记为brushed=False
        # 这样State界面就会立刻移除它们
        active_db_torrents = TorrentDB.select().where(TorrentDB.brushed == True)
        removed_count = 0
        for t in active_db_torrents:
            if (t.site, str(t.torrent_id)) not in current_qb_keys:
                logger.info(f"种子 {t.name} 已不在QB中，标记为停止刷流")
                t.brushed = False
                t.save()
                removed_count += 1

        logger.info(
            f"抓取QB中种子状态完成，记录{count}个活跃种子，标记{removed_count}个种子已移除"
        )

    def clean_long_time_no_activate(self):
        """
        清理长时间未活动的种子
        """
        logger.info(f"开始清理长时间未活动的种子")
        qb_torrents = self._qb.torrents

        # 统计出所有正在刷流以及历史刷流的种子ID
        brush_torrents = BrushTorrent.select().group_by(BrushTorrent.torrent)
        torrents = set([brush_torrent.torrent for brush_torrent in brush_torrents])
        logger.info(f"当前数据库中共有{len(torrents)}个种子记录需要检查")

        # 处理每个torrent
        cleaned_count = 0
        for torrent in torrents:
            # 查询出所有记录
            brush_torrent_records = list(
                BrushTorrent.select()
                .where(BrushTorrent.torrent == torrent)
                .order_by(BrushTorrent.created_time.asc())
            )
            if not brush_torrent_records:
                continue

            # 查询对应的qb中的种子
            target_qb_torrent = [
                i
                for i in qb_torrents
                if i.site == torrent.site
                and str(i.torrent_id) == str(torrent.torrent_id)
            ]
            if not target_qb_torrent:
                # qb中已经删除了此种子，则删除记录
                logger.info(f"种子 {torrent.name} 在QB中已不存在，清理相关记录")
                BrushTorrent.delete().where(BrushTorrent.torrent == torrent).execute()
                continue
            target_qb_torrent = target_qb_torrent[0]

            # 策略调整：发现排队或错误状态种子直接删除
            if target_qb_torrent.state in [
                "queuedUP",
                "queuedDL",
                "error",
                "missingFiles",
            ]:
                logger.info(
                    f"发现排队/错误种子: {torrent.name} ({target_qb_torrent.state})，执行直接删除"
                )
                BrushTorrent.delete().where(BrushTorrent.torrent == torrent).execute()
                self._qb.delete_torrent(target_qb_torrent.hash)
                cleaned_count += 1
                logger.bind(category="DELETE_TORRENT").info(
                    f"异常清理: {torrent.name} ({target_qb_torrent.state})"
                )
                continue

            # 保护处于检查/维护状态的种子 (不包括排队)
            maintenance_states = [
                "checkingUP",
                "checkingDL",
                "checkingResumeData",
                "allocating",
                "metaDL",
                "moving",
            ]
            if target_qb_torrent.state in maintenance_states:
                logger.info(
                    f"种子 {torrent.name} 处于维护状态 {target_qb_torrent.state}，跳过清理"
                )
                continue

            latest_record = brush_torrent_records.pop()

            if latest_record.upspeed != 0 or latest_record.dlspeed != 0:
                # 跳过正在活动的种子
                logger.info(
                    f"种子活动中: {torrent.name} (UP:{latest_record.upspeed}/DL:{latest_record.dlspeed})"
                )
                continue

            end_time = latest_record.created_time
            # 统计出无活动的持续时间
            while brush_torrent_records:
                latest_record = brush_torrent_records.pop()
                if latest_record.upspeed != 0 or latest_record.dlspeed != 0:
                    break
            start_time = latest_record.created_time
            if self._config.brush.max_no_activate_time < 5:
                # logger.warning(f"max_no_activate_time配置值小于5分钟，使用默认值5分钟")
                max_no_activate_time = 5
            else:
                max_no_activate_time = self._config.brush.max_no_activate_time
            inactive_duration = (end_time - start_time).total_seconds() / 60

            logger.info(
                f"种子 {torrent.name} 无活动: {inactive_duration:.1f}min (阈值: {max_no_activate_time}min)"
            )

            if inactive_duration > max_no_activate_time:
                logger.info(
                    f"清理无活动种子: {torrent.name}, 无活动时长: {inactive_duration:.1f}分钟, 超过配置阈值: {max_no_activate_time}分钟"
                )
                BrushTorrent.delete().where(BrushTorrent.torrent == torrent).execute()
                self._qb.delete_torrent(target_qb_torrent.hash)
                cleaned_count += 1
                logger.bind(category="DELETE_TORRENT").info(
                    f"无活动清理: {torrent.name} ({inactive_duration:.0f}min)"
                )

        logger.info(f"开始清理brushtorrent表7天前的历史记录...")
        old_records = (
            BrushTorrent.delete()
            .where(BrushTorrent.created_time < (datetime.now() - timedelta(days=7)))
            .execute()
        )
        logger.info(f"已清理{old_records}条7天前的历史记录")

        # 对数据库瘦身
        logger.info("开始对数据库进行瘦身优化...")
        database.execute_sql("VACUUM;")

        if cleaned_count > 0:
            logger.bind(category="DELETE_TORRENT").info(
                f"长时间未活动种子清理完成，本次共清理{cleaned_count}个无活动种子"
            )
            # 用户策略：清理后立即补充
            logger.info("清理完毕，尝试立即补充新种子...")
            try:
                BrushService().brush()
            except Exception as e:
                logger.error(f"补充种子失败: {e}")

    def check_disk_space_and_cleanup(self):
        """
        检查磁盘空间，如果不足则按策略（最低分优先）清理种子
        """
        min_disk_space = self._config.brush.min_disk_space
        # 重新获取状态以确保是最新的
        qb_status = self._qb.status
        current_free_space = qb_status.free_space_size

        if current_free_space >= min_disk_space:
            return

        logger.warning(
            f"磁盘空间不足 (剩余: {current_free_space / 1024 / 1024 / 1024:.2f}GB, 阈值: {min_disk_space / 1024 / 1024 / 1024:.2f}GB)，开始执行清理策略"
        )
        SystemMessage.create(
            message_type="WARNING",
            category="SYSTEM",
            content=f"磁盘空间不足 (剩余: {current_free_space / 1024 / 1024 / 1024:.2f}GB)，开始执行清理策略",
        )

        # 获取所有QB中的种子
        qb_torrents = self._qb.torrents
        if not qb_torrents:
            return

        # 收集候选种子信息
        candidates = []
        for qb_t in qb_torrents:
            score = 0
            created_time = datetime.now()

            if qb_t.site and qb_t.torrent_id:
                t_db = TorrentDB.get_or_none(
                    TorrentDB.site == qb_t.site, TorrentDB.torrent_id == qb_t.torrent_id
                )
                if t_db:
                    score = t_db.score
                    created_time = t_db.created_time

            candidates.append(
                {
                    "hash": qb_t.hash,
                    "name": qb_t.name,
                    "size": qb_t.size,
                    "score": score,
                    "created_time": created_time,
                    "site": qb_t.site,
                    "torrent_id": qb_t.torrent_id,
                }
            )

        # 排序策略：
        # 1. 分数越低越优先 (升序)
        # 2.同样分数，越早创建越优先 (升序)
        candidates.sort(key=lambda x: (x["score"], x["created_time"]))

        deleted_count = 0
        freed_space = 0

        # 目标是释放出只要比 min_disk_space 多一点空间即可，比如多留 10GB 缓冲
        target_free_space = min_disk_space + 10 * 1024 * 1024 * 1024

        for cand in candidates:
            if current_free_space >= target_free_space:
                break

            logger.info(
                f"清理低分种子: {cand['name']}, 分数: {cand['score']}, 大小: {cand['size'] / 1024 / 1024:.2f}MB"
            )

            # 删除种子
            self._qb.delete_torrent(cand["hash"])

            if cand["site"] and cand["torrent_id"]:
                site = cand["site"]
                tid = cand["torrent_id"]
                # 标记种子为未刷流，并删除刷流记录
                TorrentDB.update(brushed=False).where(
                    TorrentDB.site == site, TorrentDB.torrent_id == tid
                ).execute()
                # BrushTorrent 删除逻辑稍复杂，因为它没有 direct foreign key to QB hash.
                # 但 TorrentDB 外键关联 BrushTorrent.
                # 我们可以找到 TorrentDB 对应的 BrushTorrent 并删除.
                # 简单做法：让 clean_long_time_no_activate 或 DB 外键级联删除来处理。
                # 但这里手动删一下 BrushTorrent 比较好。
                t_db_q = TorrentDB.select().where(
                    TorrentDB.site == site, TorrentDB.torrent_id == tid
                )
                BrushTorrent.delete().where(BrushTorrent.torrent.in_(t_db_q)).execute()

            current_free_space += cand["size"]
            freed_space += cand["size"]
            deleted_count += 1

        if deleted_count > 0:
            msg = f"磁盘空间清理完成，共删除 {deleted_count} 个种子，释放 {freed_space / 1024 / 1024:.2f}MB 空间"
            logger.info(msg)
            SystemMessage.create(
                message_type="SUCCESS", category="DELETE_TORRENT", content=msg
            )

    def torrent_thinned(self):
        """
        对下载中的种子，进行瘦身
        """
        logger.info(f"开始瘦身种子任务...")
        thinned_count = 0
        for torrent in self._qb.torrents:
            # 跳过已完成任务
            if torrent.completed:
                continue

            # 跳过非大包种子
            if torrent.size < self._config.brush.torrent_max_size:
                continue

            logger.info(
                f"正在处理大包种子: {torrent.name}, 大小: {torrent.size / 1024 / 1024 / 1024:.2f}GB"
            )
            files = self._qb.get_torrent_files(torrent.hash)
            all_file_ids = [file["index"] for file in files]

            current_size = sum(
                [file["size"] for file in files if file["priority"] != 0]
            )

            download_file_ids = []
            total_files = len(files)
            selected_files = 0
            for file in files:
                if file["priority"] != 0:
                    if current_size > self._config.brush.torrent_max_size:
                        # 超出了大小限制，将文件设置为不下载
                        file["priority"] = 0
                        current_size -= file["size"]
                    else:
                        download_file_ids.append(file["index"])
                        selected_files += 1

            no_download_file_ids = list(set(all_file_ids) - set(download_file_ids))

            self._qb.set_no_download_files(torrent.hash, no_download_file_ids)
            logger.info(
                f"种子{torrent.name}瘦身完成 - 选择下载: {selected_files}/{total_files}个文件, 预计大小: {current_size / 1024 / 1024:.2f}MB"
            )
            thinned_count += 1

        logger.info(f"瘦身种子任务完成，本次共处理{thinned_count}个大包种子")
        # logger.info(torrent.size)


# 刷流逻辑
class BrushService:
    def __init__(self):
        self._config = PTBrushConfig()
        self._qb = QBittorrent(
            self._config.downloader.url,
            self._config.downloader.username,
            self._config.downloader.password,
        )

    @property
    def last_cycle_max_dlspeed(self) -> int:
        """
        上一个周期内，qb的最大下载速度
        """
        start_time = datetime.now() - timedelta(
            seconds=self._config.brush.download_cycle
        )
        (avg_dlspeed,) = (
            QBStatus.select(peewee.fn.MAX(QBStatus.dlspeed))
            .where(QBStatus.created_time > start_time)
            .scalar(as_tuple=True)
        )
        # 如果还没有采集过qb的信息，那么应该等采集完再决定要不要刷流，因此这里返回最大值
        return avg_dlspeed if avg_dlspeed != None else 9999999999999

    @property
    def qb_free_space_size(self) -> int:
        """
        当前qb剩余空间
        """
        return self._qb.status.free_space_size

    @property
    def last_cycle_average_upspeed(self) -> int:
        """
        上一个周期内，qb的平均上传速度
        """
        start_time = datetime.now() - timedelta(seconds=self._config.brush.upload_cycle)
        (avg_upspeed,) = (
            QBStatus.select(peewee.fn.AVG(QBStatus.upspeed))
            .where(QBStatus.created_time > start_time)
            .scalar(as_tuple=True)
        )
        # 如果还没有采集过qb的信息，那么应该等采集完再决定要不要刷流，因此这里返回最大值
        return avg_upspeed if avg_upspeed != None else 9999999999999

    @property
    def uncompleted_count(self) -> int:
        """
        当前qb中未完成的刷流任务数
        """
        return len([i for i in self._qb.torrents if i.completed == 0])

    def get_brush_torrent(self, count: int = 10) -> List[Torrent]:
        # 至少要留3个小时来下载
        now = datetime.now() + timedelta(hours=3)
        torrents_db = (
            TorrentDB.select()
            .where(
                (TorrentDB.free_end_time > now)
                & (TorrentDB.brushed == False)
                & (TorrentDB.size <= self._config.brush.torrent_max_size)
            )
            .order_by(TorrentDB.created_time.desc())
            .limit(count)
        )
        result = []
        for i in torrents_db:
            result.append(
                Torrent(
                    id=i.torrent_id,
                    leechers=i.leechers,
                    name=i.name,
                    seeders=i.seeders,
                    created_time=i.created_time,
                    free_end_time=i.free_end_time,
                    size=i.size,
                    site=i.site,
                )
            )

        return result

    def _get_site_config(self, site: str) -> Optional[SiteModel]:
        for i in self._config.sites:
            if i.name == site:
                return i
        return None

    def _set_brushed(self, torrent: Torrent):
        torrent_db = TorrentDB.get_or_none(
            TorrentDB.site == torrent.site, TorrentDB.torrent_id == torrent.id
        )
        torrent_db.brushed = True
        torrent_db.save()

    def add_brush_torrent(self, torrents: List[Torrent]):
        for torrent in torrents:
            site_config = self._get_site_config(torrent.site)
            if not site_config:
                logger.error(f"获取站点{torrent.site}配置失败，跳过种子{torrent.name}")
                continue

            logger.info(
                f"正在处理种子: {torrent.name} (站点:{torrent.site}, ID:{torrent.id})"
            )
            torrent_fetch = TorrentFetch(
                torrent.site, site_config.cookie, site_config.headers
            )
            torrent_link = torrent_fetch.parse_torrent_link(torrent.id)
            if not torrent_link:
                logger.error(f"获取种子下载链接失败，跳过种子{torrent.name}")
                continue
            clean_name = torrent.name.split("__meta")[0]
            torrent_rename = f"{clean_name}__meta.{torrent.site}.{torrent.id}.endTime.{torrent.free_end_time.strftime('%Y-%m-%d-%H:%M:%S')}"
            torrent_content = torrent_fetch.download_torrent_content(torrent_link)
            if not torrent_content:
                logger.error(f"下载种子内容失败，跳过种子{torrent.name}")
                continue

            res = self._qb.download_torrent_url(torrent_content, torrent_rename)
            if res:
                logger.info(
                    f"成功添加种子到QB: {torrent.name} (大小:{torrent.size / 1024 / 1024:.2f}MB)"
                )
                self._set_brushed(torrent)
                SystemMessage.create(
                    message_type="SUCCESS",
                    category="ADD_TORRENT",
                    content=f"成功添加种子: {torrent.name} ({torrent.site})",
                )
            else:
                logger.error(f"添加种子到QB失败: {torrent.name}")

            # return res

    def brush(self) -> int:
        """
        刷流入口,返回添加种子的个数
        """
        logger.info(f"刷流任务开始...")
        # 检查当前qb下载器的剩余空间
        if self.qb_free_space_size < self._config.brush.min_disk_space:
            logger.info(
                f"qb剩余空间不足，停止刷流，当前剩余空间为:{self.qb_free_space_size / 1024 / 1024 / 1024:.2f}GB"
            )
            return 0

        # 用户策略调整：取消速度限制检查，改为严格数量控制
        # 检查当前QB中总种子数
        current_count = len(self._qb.torrents)

        if current_count >= self._config.brush.max_active_torrents:
            logger.info(
                f"QB中种子数已满 ({current_count}/{self._config.brush.max_active_torrents})，停止添加"
            )
            return 0

        need_add_count = self._config.brush.max_active_torrents - current_count
        logger.info(f"QB中种子数:{current_count}, 还需要添加: {need_add_count}")

        # 添加最新种子
        torrents = self.get_brush_torrent(need_add_count)
        if not torrents:
            logger.info("没有可添加的种子")
            return 0

        logger.info(f"获取到{len(torrents)}个最新种子，开始添加...")
        self.add_brush_torrent(torrents)

        logger.info(f"刷流任务完成，本次成功添加{len(torrents)}个新种子")
        return len(torrents)
