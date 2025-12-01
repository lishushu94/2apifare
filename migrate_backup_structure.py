#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备份文件夹结构迁移脚本
从 2025/11/28 迁移到 20251128
"""

import shutil
from pathlib import Path


def migrate_backup_structure():
    """迁移备份文件夹结构"""
    backup_repo_dir = Path(__file__).parent / ".backup_repo"
    
    if not backup_repo_dir.exists():
        print("❌ 备份仓库不存在")
        return
    
    print("🔄 开始迁移备份文件夹结构...")
    print(f"   仓库路径: {backup_repo_dir}")
    
    migrated_count = 0
    
    # 遍历年份文件夹
    for year_dir in backup_repo_dir.iterdir():
        if not year_dir.is_dir() or year_dir.name.startswith('.'):
            continue
        
        # 检查是否是年份文件夹（4位数字）
        if len(year_dir.name) != 4 or not year_dir.name.isdigit():
            continue
        
        print(f"\n📁 处理年份: {year_dir.name}")
        
        # 遍历月份文件夹
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            
            # 检查是否是月份文件夹（2位数字）
            if len(month_dir.name) != 2 or not month_dir.name.isdigit():
                continue
            
            print(f"   📁 处理月份: {month_dir.name}")
            
            # 遍历日期文件夹
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                
                # 检查是否是日期文件夹（2位数字）
                if len(day_dir.name) != 2 or not day_dir.name.isdigit():
                    continue
                
                # 构建新的文件夹名称：YYYYMMDD
                new_dir_name = f"{year_dir.name}{month_dir.name}{day_dir.name}"
                new_dir_path = backup_repo_dir / new_dir_name
                
                print(f"      📦 迁移: {year_dir.name}/{month_dir.name}/{day_dir.name} -> {new_dir_name}")
                
                # 如果新文件夹已存在，合并内容
                if new_dir_path.exists():
                    print(f"         ⚠️  目标文件夹已存在，合并内容...")
                    for hour_dir in day_dir.iterdir():
                        if hour_dir.is_dir():
                            target_hour_dir = new_dir_path / hour_dir.name
                            if target_hour_dir.exists():
                                # 合并小时文件夹中的文件
                                for file in hour_dir.iterdir():
                                    if file.is_file():
                                        shutil.copy2(file, target_hour_dir / file.name)
                            else:
                                shutil.copytree(hour_dir, target_hour_dir)
                else:
                    # 直接移动整个文件夹
                    shutil.move(str(day_dir), str(new_dir_path))
                
                migrated_count += 1
    
    # 清理空的年份和月份文件夹
    print("\n🗑️  清理空文件夹...")
    for year_dir in backup_repo_dir.iterdir():
        if not year_dir.is_dir() or year_dir.name.startswith('.'):
            continue
        
        if len(year_dir.name) == 4 and year_dir.name.isdigit():
            # 清理空的月份文件夹
            for month_dir in year_dir.iterdir():
                if month_dir.is_dir() and not any(month_dir.iterdir()):
                    print(f"   删除空文件夹: {year_dir.name}/{month_dir.name}")
                    month_dir.rmdir()
            
            # 清理空的年份文件夹
            if not any(year_dir.iterdir()):
                print(f"   删除空文件夹: {year_dir.name}")
                year_dir.rmdir()
    
    print(f"\n✅ 迁移完成！共迁移 {migrated_count} 天的备份")
    print("\n📊 当前备份结构:")
    for day_dir in sorted(backup_repo_dir.iterdir()):
        if day_dir.is_dir() and not day_dir.name.startswith('.'):
            if len(day_dir.name) == 8 and day_dir.name.isdigit():
                hour_count = len([d for d in day_dir.iterdir() if d.is_dir()])
                print(f"   {day_dir.name}: {hour_count} 小时")


if __name__ == "__main__":
    migrate_backup_structure()
