#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
凭证文件 GitHub 自动备份脚本
- 每小时自动备份到 GitHub
- 使用东八区时间
- 96小时（4天）滚动备份
- 文件夹结构：年/月/日/时
- 文件命名：creds_{count}_{timestamp}.toml.bak
- 每天凌晨0点清理超过4天的旧备份
"""

import os
import sys
import shutil
import subprocess
import toml
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


class CredsBackup:
    def __init__(self, config_path="creds/config.toml"):
        """初始化备份系统"""
        self.config_path = config_path
        self.config = self._load_config()
        self.backup_config = self.config.get("backup", {})

        # 验证配置
        if not self.backup_config.get("enabled", False):
            print("❌ 备份功能未启用")
            sys.exit(1)

        self.github_token = self.backup_config.get("github_token")
        self.github_repo = self.backup_config.get("github_repo")
        self.max_backup_days = 4  # 保留4天的备份

        if not self.github_token or not self.github_repo:
            print("❌ GitHub 配置不完整")
            sys.exit(1)

        # 项目根目录
        self.project_root = Path(__file__).parent.absolute()
        self.creds_dir = self.project_root / "creds"
        self.backup_repo_dir = self.project_root / ".backup_repo"

    def _load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return toml.load(f)
        except Exception as e:
            print(f"❌ 读取配置失败: {e}")
            sys.exit(1)

    def _get_china_time(self):
        """获取东八区时间"""
        china_tz = timezone(timedelta(hours=8))
        return datetime.now(china_tz)

    def _count_credentials(self, toml_file):
        """统计 TOML 文件中的凭证数量"""
        try:
            with open(toml_file, "r", encoding="utf-8") as f:
                data = toml.load(f)

                # 对于 accounts.toml，统计 [[accounts]] 数组的长度
                if toml_file.name == "accounts.toml" and "accounts" in data:
                    if isinstance(data["accounts"], list):
                        return len(data["accounts"])

                # 对于 creds.toml，统计顶层字典键的数量
                if isinstance(data, dict):
                    # 排除非凭证的配置项
                    excluded_keys = {"backup", "api_password", "panel_password", "admin_password"}
                    cred_count = sum(1 for key in data.keys() if key not in excluded_keys)
                    return cred_count

                return 0
        except Exception as e:
            print(f"⚠️  统计凭证数量失败: {e}")
            return 0

    def _run_git_command(self, cmd, cwd=None):
        """执行 Git 命令"""
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd or self.backup_repo_dir,
                check=True,
                capture_output=True,
                text=True,
                encoding='utf-8'  # [FIX] 明确指定 UTF-8 编码，避免 Windows GBK 解码错误
            )
            return result
        except subprocess.CalledProcessError as e:
            print(f"❌ Git 命令失败: {' '.join(cmd)}")
            print(f"错误: {e.stderr}")
            raise

    def _init_repo(self):
        """初始化或更新仓库"""
        repo_url_with_token = self.github_repo.replace(
            "https://",
            f"https://{self.github_token}@"
        )

        if self.backup_repo_dir.exists():
            print("📥 拉取最新备份...")
            self._run_git_command(["git", "pull", "origin", "main"])
        else:
            print("📥 克隆备份仓库...")
            self._run_git_command(
                ["git", "clone", repo_url_with_token, str(self.backup_repo_dir)],
                cwd=self.project_root
            )
            # 确保有 main 分支
            try:
                self._run_git_command(["git", "checkout", "main"])
            except:
                self._run_git_command(["git", "checkout", "-b", "main"])

        # 配置 git 用户信息（每次都配置，确保 commit 不会失败）
        self._run_git_command(["git", "config", "user.name", "Auto Backup Bot"])
        self._run_git_command(["git", "config", "user.email", "backup@2apifare.local"])

    def _create_backup_path(self, china_time):
        """创建备份路径：YYYYMMDD/HH（日期文件夹/小时文件夹）"""
        date_str = china_time.strftime("%Y%m%d")  # 20251128
        hour = china_time.strftime("%H")          # 00

        backup_path = self.backup_repo_dir / date_str / hour
        backup_path.mkdir(parents=True, exist_ok=True)
        return backup_path

    def _backup_file(self, source_file, backup_dir, china_time):
        """备份单个文件"""
        if not source_file.exists():
            print(f"⚠️  文件不存在: {source_file.name}")
            return None

        # 统计凭证数量
        count = self._count_credentials(source_file)

        # 生成时间戳 HHmmss（时分秒）
        timestamp = china_time.strftime("%H%M%S")

        # 生成文件名
        filename = f"{source_file.stem}_{count}_{timestamp}{source_file.suffix}.bak"
        dest_file = backup_dir / filename

        # 复制文件
        shutil.copy2(source_file, dest_file)
        print(f"✅ 备份: {filename} (凭证数: {count})")

        return dest_file

    def _cleanup_old_backups(self, china_time):
        """清理超过4天的旧备份（每次备份时都检查）"""
        print("🗑️  检查是否需要清理旧备份...")

        # 计算4天前的日期
        cutoff_date = china_time - timedelta(days=self.max_backup_days)
        cutoff_date_str = cutoff_date.strftime("%Y%m%d")

        print(f"   清理截止日期: {cutoff_date.strftime('%Y-%m-%d')} (保留 {self.max_backup_days} 天)")

        # 获取所有日期文件夹
        all_day_dirs = []
        deleted_days = []

        for day_dir in self.backup_repo_dir.iterdir():
            if not day_dir.is_dir() or day_dir.name.startswith('.'):
                continue

            # 检查是否是日期文件夹（YYYYMMDD格式，8位数字）
            if len(day_dir.name) == 8 and day_dir.name.isdigit():
                try:
                    # 验证是否是有效日期
                    datetime.strptime(day_dir.name, "%Y%m%d")
                    all_day_dirs.append(day_dir)
                except:
                    continue

        print(f"   当前总共有 {len(all_day_dirs)} 天的备份")

        # 只有当备份天数 > max_backup_days 时才删除
        if len(all_day_dirs) <= self.max_backup_days:
            print(f"ℹ️  备份天数未超过 {self.max_backup_days} 天，无需清理")
            return

        # 删除超过截止日期的备份（简单的字符串比较即可）
        for day_dir in all_day_dirs:
            if day_dir.name <= cutoff_date_str:
                try:
                    print(f"   删除旧备份: {day_dir.name}")
                    shutil.rmtree(day_dir)
                    deleted_days.append(day_dir.name)
                except Exception as e:
                    print(f"   ⚠️  删除失败 {day_dir.name}: {e}")

        if deleted_days:
            print(f"✅ 已清理 {len(deleted_days)} 天的旧备份")
        else:
            print("ℹ️  暂无需要清理的旧备份")

    def _commit_and_push(self, china_time):
        """提交并推送"""
        # 添加所有更改
        self._run_git_command(["git", "add", "."])

        # 检查是否有变更
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.backup_repo_dir,
            capture_output=True,
            text=True,
            encoding='utf-8'  # [FIX] 明确指定 UTF-8 编码，避免 Windows GBK 解码错误
        )

        if not status.stdout.strip():
            print("ℹ️  没有变更，跳过提交")
            return

        # 提交
        commit_msg = f"🔒 自动备份 {china_time.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"
        self._run_git_command(["git", "commit", "-m", commit_msg])

        # 推送
        print("🚀 推送到 GitHub...")
        self._run_git_command(["git", "push", "origin", "main"])

        print("✅ 备份已推送到 GitHub")

    def run(self):
        """执行备份"""
        try:
            china_time = self._get_china_time()
            print("\n" + "="*60)
            print(f"🚀 开始备份 - {china_time.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
            print("="*60 + "\n")

            # 1. 初始化仓库
            self._init_repo()

            # 2. 创建备份目录
            backup_dir = self._create_backup_path(china_time)
            print(f"📁 备份目录: {backup_dir.relative_to(self.backup_repo_dir)}")

            # 3. 备份文件
            creds_file = self.creds_dir / "creds.toml"
            accounts_file = self.creds_dir / "accounts.toml"

            self._backup_file(creds_file, backup_dir, china_time)
            self._backup_file(accounts_file, backup_dir, china_time)

            # 4. 清理旧备份（每天凌晨0点执行）
            self._cleanup_old_backups(china_time)

            # 5. 提交并推送
            self._commit_and_push(china_time)

            print("\n" + "="*60)
            print("✅ 备份完成")
            print("="*60)

        except KeyboardInterrupt:
            print("\n⚠️  备份中断")
            sys.exit(1)
        except Exception as e:
            print(f"\n❌ 备份失败: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


def main():
    backup = CredsBackup()
    backup.run()


if __name__ == "__main__":
    main()
