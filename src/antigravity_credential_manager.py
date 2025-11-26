"""
Antigravity Credential Manager - 管理 Antigravity 凭证
"""

import asyncio
import os
import time
import httpx
from typing import Dict, Any, List, Optional, Tuple
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from config import get_calls_per_rotation, get_credentials_dir
from log import log
from .storage_adapter import get_storage_adapter

# Google OAuth 客户端信息（与 antigravity2api-nodejs 保持一致）
CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"


class AntigravityCredentialManager:
    """
    Antigravity 凭证管理器

    功能：
    1. 从 accounts.toml 加载 Antigravity 凭证
    2. 自动轮换凭证
    3. 处理禁用和错误的凭证
    4. 与 storage_adapter 集成管理状态
    """

    def __init__(self):
        # 核心状态
        self._initialized = False
        self._storage_adapter = None

        # 凭证列表和状态
        self._credential_accounts = []  # List of account dicts from accounts.toml
        self._current_credential_index = 0
        self._current_credential_account = None
        self._current_credential_state = None

        # 调用计数
        self._call_count = 0
        self._last_rotation_time = 0

        # 线程安全
        self._operation_lock = asyncio.Lock()

    async def initialize(self):
        """初始化凭证管理器"""
        if self._initialized:
            return

        async with self._operation_lock:
            if self._initialized:
                return

            try:
                # 获取 storage adapter
                self._storage_adapter = await get_storage_adapter()

                # 加载凭证
                await self._discover_credentials()

                self._initialized = True
                log.info(
                    f"Antigravity credential manager initialized with {len(self._credential_accounts)} accounts"
                )

            except Exception as e:
                log.error(f"Failed to initialize Antigravity credential manager: {e}")
                raise

    async def _discover_credentials(self):
        """从 accounts.toml 加载凭证"""
        try:
            # 读取 accounts.toml
            accounts_data = await self._storage_adapter.load_antigravity_accounts()

            if not accounts_data or not accounts_data.get("accounts"):
                log.warning("No Antigravity accounts found in accounts.toml")
                self._credential_accounts = []
                return

            # 获取所有账号
            all_accounts = accounts_data["accounts"]

            # 过滤禁用的账号
            enabled_accounts = []
            for account in all_accounts:
                email = account.get("email", "unknown")

                # 构建虚拟文件名（用于状态管理）
                virtual_filename = f"userID_{account.get('user_id', 'unknown')}"

                # 获取状态
                state = await self._storage_adapter.get_credential_state(virtual_filename)

                if not state:
                    # 创建默认状态
                    state = {
                        "disabled": False,
                        "error_codes": [],
                        "last_success": None,
                        "user_email": email,
                    }
                    await self._storage_adapter.update_credential_state(virtual_filename, state)

                # 检查是否被禁用
                if not state.get("disabled", False):
                    enabled_accounts.append({
                        "account": account,
                        "virtual_filename": virtual_filename,
                        "state": state,
                    })
                else:
                    log.debug(f"Skipping disabled Antigravity account: {email}")

            self._credential_accounts = enabled_accounts
            log.info(f"Discovered {len(enabled_accounts)} enabled Antigravity accounts")

        except Exception as e:
            log.error(f"Error discovering Antigravity credentials: {e}")
            self._credential_accounts = []

    async def _should_rotate(self) -> bool:
        """检查是否需要轮换凭证"""
        if not self._credential_accounts:
            return False

        if len(self._credential_accounts) <= 1:
            return False

        # 获取轮换配置
        calls_per_rotation = await get_calls_per_rotation()

        # 检查调用次数
        if self._call_count >= calls_per_rotation:
            log.info(
                f"Antigravity credential rotation triggered: {self._call_count} calls >= {calls_per_rotation}"
            )
            return True

        return False

    async def _rotate_credential(self):
        """轮换到下一个凭证"""
        if not self._credential_accounts:
            return

        # 移到下一个凭证
        self._current_credential_index = (self._current_credential_index + 1) % len(
            self._credential_accounts
        )

        # 重置调用计数
        self._call_count = 0
        self._last_rotation_time = time.time()

        # 加载新凭证
        await self._load_current_credential()

        log.info(
            f"Rotated to Antigravity account {self._current_credential_index + 1}/{len(self._credential_accounts)}: "
            f"{self._current_credential_account.get('email', 'unknown')}"
        )

    async def force_rotate_credential(self):
        """强制轮换到下一个凭证（用于错误处理）"""
        async with self._operation_lock:
            await self._rotate_credential()
            log.info("Forced credential rotation due to error")

    async def _load_current_credential(self) -> Optional[Dict[str, Any]]:
        """加载当前凭证"""
        try:
            if not self._credential_accounts:
                return None

            current_cred_data = self._credential_accounts[self._current_credential_index]

            self._current_credential_account = current_cred_data["account"]
            self._current_credential_state = current_cred_data["state"]

            return current_cred_data

        except Exception as e:
            log.error(f"Error loading current Antigravity credential: {e}")
            return None

    def _is_token_expired(self, account: Dict[str, Any]) -> bool:
        """
        检查 access_token 是否过期

        Args:
            account: 账号数据字典

        Returns:
            True 如果 token 已过期或缺少必要字段
        """
        # 检查必要字段
        if "timestamp" not in account or "expires_in" not in account:
            log.debug(f"Token missing timestamp or expires_in, treating as expired")
            return True

        timestamp = account["timestamp"]
        expires_in = account["expires_in"]

        # 计算过期时间（提前5分钟刷新）
        expires_at = timestamp + (expires_in * 1000) - (5 * 60 * 1000)  # 提前5分钟
        current_time = time.time() * 1000  # 转换为毫秒

        is_expired = current_time >= expires_at

        if is_expired:
            email = account.get("email", "unknown")
            log.info(f"Token expired for account: {email}")

        return is_expired

    async def _refresh_access_token(self, account: Dict[str, Any]) -> bool:
        """
        使用 refresh_token 刷新 access_token

        Args:
            account: 账号数据字典（会被原地修改）

        Returns:
            True 表示刷新成功，False 表示刷新失败
        """
        refresh_token = account.get("refresh_token")
        if not refresh_token:
            log.error("Account missing refresh_token, cannot refresh")
            return False

        email = account.get("email", "unknown")
        log.info(f"Refreshing access token for account: {email}")

        try:
            # 构建请求数据
            data = {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token
            }

            # 发送刷新请求
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    headers={
                        "Host": "oauth2.googleapis.com",
                        "User-Agent": "Go-http-client/1.1",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept-Encoding": "gzip"
                    },
                    data=urlencode(data)
                )

                if response.status_code == 200:
                    result = response.json()

                    # 更新 account 数据
                    account["access_token"] = result["access_token"]
                    account["expires_in"] = result["expires_in"]
                    account["timestamp"] = int(time.time() * 1000)  # 毫秒时间戳

                    # 保存到 accounts.toml
                    await self._save_account_to_storage(account)

                    log.info(f"Successfully refreshed token for account: {email}")
                    return True

                elif response.status_code == 403:
                    log.error(f"Token refresh failed (403 Forbidden) for account: {email}")
                    # 403 错误通常表示 refresh_token 已失效，应该禁用账号
                    return False

                else:
                    error_text = response.text
                    log.error(f"Token refresh failed ({response.status_code}) for account {email}: {error_text}")
                    return False

        except Exception as e:
            log.error(f"Error refreshing token for account {email}: {e}")
            return False

    async def _save_account_to_storage(self, account: Dict[str, Any]):
        """
        将更新后的账号信息保存到 accounts.toml

        Args:
            account: 账号数据字典
        """
        try:
            # 读取现有的 accounts.toml
            accounts_data = await self._storage_adapter.load_antigravity_accounts()

            if not accounts_data or "accounts" not in accounts_data:
                log.error("Failed to load accounts.toml for saving")
                return

            # 查找并更新对应的账号
            user_id = account.get("user_id")
            found = False

            for i, existing_account in enumerate(accounts_data["accounts"]):
                if existing_account.get("user_id") == user_id:
                    # 更新账号信息（保留其他字段）
                    accounts_data["accounts"][i].update({
                        "access_token": account["access_token"],
                        "expires_in": account["expires_in"],
                        "timestamp": account["timestamp"],
                        "last_used": time.strftime("%Y-%m-%d %H:%M:%S")
                    })
                    found = True
                    break

            if not found:
                log.warning(f"Account with user_id {user_id} not found in accounts.toml")
                return

            # 保存回 accounts.toml
            await self._storage_adapter.save_antigravity_accounts(accounts_data)

            log.debug(f"Saved updated token to accounts.toml for user_id: {user_id}")

        except Exception as e:
            log.error(f"Error saving account to storage: {e}")

    async def get_valid_credential(self, model_name: str = None) -> Optional[Dict[str, Any]]:
        """
        获取有效的凭证，自动处理轮换和失效凭证切换

        Args:
            model_name: 模型名称（用于配额检查）

        Returns:
            Dict with 'account' and 'virtual_filename' keys, or None if no valid credential
        """
        async with self._operation_lock:
            if not self._credential_accounts:
                await self._discover_credentials()
                if not self._credential_accounts:
                    return None

            # 检查是否需要轮换（基于调用次数）
            if await self._should_rotate():
                await self._rotate_credential()

            # 如果当前没有加载凭证，加载第一个
            if not self._current_credential_account:
                await self._load_current_credential()

            # 检查配额（如果提供了模型名称）
            if model_name and self._current_credential_account:
                virtual_filename = self._credential_accounts[self._current_credential_index]["virtual_filename"]

                # 检查配额
                from .antigravity_usage_stats import check_antigravity_quota
                quota_available, reason = await check_antigravity_quota(virtual_filename, model_name)

                if not quota_available:
                    log.warning(f"Account {self._current_credential_account.get('email')} quota exhausted: {reason}")

                    # 配额用尽，尝试切换到下一个账号
                    if len(self._credential_accounts) > 1:
                        log.info("Rotating to next account due to quota limit")
                        await self._rotate_credential()
                        await self._load_current_credential()

                        # 递归调用检查新账号
                        return await self.get_valid_credential(model_name)
                    else:
                        log.error("Only one account available and quota exhausted")
                        return None

            # 检查 token 是否过期，如果过期则刷新
            if self._current_credential_account:
                if self._is_token_expired(self._current_credential_account):
                    log.info("Current token expired, refreshing...")

                    refresh_success = await self._refresh_access_token(self._current_credential_account)

                    if not refresh_success:
                        # 刷新失败，禁用当前账号并尝试下一个
                        log.warning(f"Failed to refresh token for {self._current_credential_account.get('email')}, disabling account")

                        virtual_filename = self._credential_accounts[self._current_credential_index]["virtual_filename"]
                        await self.disable_credential(virtual_filename)

                        # 重新加载凭证列表
                        await self._discover_credentials()

                        if not self._credential_accounts:
                            log.error("No valid Antigravity credentials remaining after refresh failure")
                            return None

                        # 尝试下一个账号
                        await self._load_current_credential()

                        if self._current_credential_account:
                            # 递归调用以确保新账号也是有效的
                            return await self.get_valid_credential(model_name)
                        else:
                            return None

            # 返回当前凭证
            if self._current_credential_account:
                return {
                    "account": self._current_credential_account,
                    "virtual_filename": self._credential_accounts[self._current_credential_index][
                        "virtual_filename"
                    ],
                }

            return None

    def increment_call_count(self):
        """增加调用计数"""
        self._call_count += 1

    async def mark_credential_error(self, virtual_filename: str, error_code: int):
        """标记凭证错误"""
        try:
            # 获取当前状态
            state = await self._storage_adapter.get_credential_state(virtual_filename)
            if not state:
                state = {}

            # 添加错误码
            error_codes = state.get("error_codes", [])
            if error_code not in error_codes:
                error_codes.append(error_code)
                state["error_codes"] = error_codes

            # 更新状态
            await self._storage_adapter.update_credential_state(virtual_filename, state)

            log.warning(
                f"Marked Antigravity credential error: {virtual_filename}, error_code={error_code}"
            )

            # 如果需要自动封禁，检查配置
            from config import get_auto_ban_enabled, get_auto_ban_error_codes

            auto_ban_enabled = await get_auto_ban_enabled()
            auto_ban_error_codes = await get_auto_ban_error_codes()

            if auto_ban_enabled and error_code in auto_ban_error_codes:
                await self.disable_credential(virtual_filename)

        except Exception as e:
            log.error(f"Error marking Antigravity credential error: {e}")

    async def disable_credential(self, virtual_filename: str):
        """禁用凭证"""
        try:
            # 更新状态
            state = await self._storage_adapter.get_credential_state(virtual_filename)
            if not state:
                state = {}

            state["disabled"] = True
            await self._storage_adapter.update_credential_state(virtual_filename, state)

            log.warning(f"Disabled Antigravity credential: {virtual_filename}")

            # 重新加载凭证列表
            await self._discover_credentials()

        except Exception as e:
            log.error(f"Error disabling Antigravity credential: {e}")

    async def mark_credential_success(self, virtual_filename: str):
        """标记凭证成功使用"""
        try:
            state = await self._storage_adapter.get_credential_state(virtual_filename)
            if not state:
                state = {}

            # 清除错误码
            state["error_codes"] = []
            state["last_success"] = time.strftime("%Y-%m-%d %H:%M:%S")

            await self._storage_adapter.update_credential_state(virtual_filename, state)

        except Exception as e:
            log.error(f"Error marking Antigravity credential success: {e}")


# 全局单例
_antigravity_credential_manager = None
_antigravity_credential_manager_lock = asyncio.Lock()


async def get_antigravity_credential_manager() -> AntigravityCredentialManager:
    """获取 Antigravity 凭证管理器单例"""
    global _antigravity_credential_manager

    if _antigravity_credential_manager is None:
        async with _antigravity_credential_manager_lock:
            if _antigravity_credential_manager is None:
                _antigravity_credential_manager = AntigravityCredentialManager()
                await _antigravity_credential_manager.initialize()

    return _antigravity_credential_manager
