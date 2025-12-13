#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备份调度器 - 每小时自动执行备份
在主程序启动时作为后台线程运行
"""

import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from backup_creds import CredsBackup

logger = logging.getLogger(__name__)


class BackupScheduler:
    """备份调度器 - 每小时执行一次备份"""

    def __init__(self, interval_hours=1):
        """
        初始化调度器
        Args:
            interval_hours: 备份间隔（小时），默认1小时
        """
        self.interval_hours = interval_hours
        self.interval_seconds = interval_hours * 3600
        self.running = False
        self.thread = None

    def _get_china_time(self):
        """获取东八区时间"""
        china_tz = timezone(timedelta(hours=8))
        return datetime.now(china_tz)

    def _run_backup(self):
        """执行备份任务（带重试机制）"""
        china_time = self._get_china_time()
        max_retries = 3
        retry_delay = 60  # 失败后等待60秒再重试

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    f"开始执行定时备份 (尝试 {attempt}/{max_retries}) - "
                    f"{china_time.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"
                )

                backup = CredsBackup()
                backup.run()

                logger.info("✅ 定时备份完成")
                return  # 成功则退出

            except Exception as e:
                logger.error(
                    f"❌ 定时备份失败 (尝试 {attempt}/{max_retries}): {e}",
                    exc_info=True
                )

                # 如果不是最后一次尝试，等待后重试
                if attempt < max_retries:
                    logger.info(f"等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"❌ 备份失败，已达到最大重试次数 ({max_retries})，跳过本次备份")

    def _scheduler_loop(self):
        """调度器主循环"""
        logger.info(f"备份调度器已启动，每 {self.interval_hours} 小时执行一次备份")

        # 不在启动时立即备份，等到下一个整点
        china_time = self._get_china_time()
        logger.info(f"服务启动时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
        logger.info("等待下一个整点小时执行首次备份...")

        last_backup_hour = -1  # 记录上次备份的小时，避免重复备份

        while self.running:
            try:
                # 获取当前时间
                china_time = self._get_china_time()
                current_hour = china_time.hour
                current_minute = china_time.minute
                current_second = china_time.second

                # 如果当前是整点（0-2分钟内）且还没有在这个小时备份过
                if current_minute <= 2 and current_hour != last_backup_hour:
                    logger.info(f"到达整点 {current_hour}:00，执行备份")
                    self._run_backup()
                    last_backup_hour = current_hour
                    # 等待3分钟，避免在同一小时内重复备份
                    time.sleep(180)
                    continue

                # 计算距离下一个整点的秒数
                seconds_to_next_hour = (60 - current_minute) * 60 - current_second

                # 如果距离下一个整点超过5分钟，就等待到下一个整点前4分钟
                if seconds_to_next_hour > 300:
                    wait_time = seconds_to_next_hour - 240  # 提前4分钟醒来
                    logger.info(f"下次备份将在 {wait_time//60} 分钟后执行（整点 {(current_hour+1)%24}:00）")
                    time.sleep(wait_time)
                else:
                    # 距离整点不到5分钟，每30秒检查一次
                    time.sleep(30)

            except Exception as e:
                logger.error(f"调度器循环错误: {e}", exc_info=True)
                time.sleep(60)  # 出错后等待1分钟再继续

    def start(self):
        """启动调度器"""
        if self.running:
            logger.warning("调度器已在运行")
            return

        self.running = True
        self.thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.thread.start()
        logger.info("备份调度器线程已启动")

    def stop(self):
        """停止调度器"""
        if not self.running:
            return

        logger.info("正在停止备份调度器...")
        self.running = False

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

        logger.info("备份调度器已停止")


# 全局调度器实例
_scheduler = None


def start_backup_scheduler(interval_hours=1):
    """启动全局备份调度器"""
    global _scheduler

    if _scheduler is not None:
        logger.warning("备份调度器已存在")
        return _scheduler

    _scheduler = BackupScheduler(interval_hours=interval_hours)
    _scheduler.start()
    return _scheduler


def stop_backup_scheduler():
    """停止全局备份调度器"""
    global _scheduler

    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None


if __name__ == "__main__":
    # 测试运行
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    scheduler = BackupScheduler(interval_hours=1)
    scheduler.start()

    try:
        # 保持运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止调度器...")
        scheduler.stop()
