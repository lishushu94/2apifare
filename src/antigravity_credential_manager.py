"""
Antigravity Credential Manager - 管理 Antigravity 凭证
"""

import asyncio
import os
import time
import json
import re
import httpx
from typing import Dict, Any, List, Optional, Tuple
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

from config import get_calls_per_rotation, get_credentials_dir, get_antigravity_skip_project_verification, get_antigravity_oauth_endpoint, get_antigravity_api_endpoint
from log import log
from .storage_adapter import get_storage_adapter
from antigravity.converter import generate_project_id

# Google OAuth 客户端信息（与 antigravity2api-nodejs 保持一致）
CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"


def _parse_duration_to_seconds(duration_str: str) -> int:
    """
    解析 Google API 的 duration 格式为秒数

    例如：
    - "2h22m50s" -> 8570 秒
    - "2h22m50.065102829s" -> 8570 秒
    - "8570.065102829s" -> 8570 秒

    Args:
        duration_str: duration 字符串（例如 "2h22m50s"）

    Returns:
        秒数（整数）
    """
    if not duration_str:
        return 0

    total_seconds = 0

    # 匹配小时
    hours_match = re.search(r'(\d+)h', duration_str)
    if hours_match:
        total_seconds += int(hours_match.group(1)) * 3600

    # 匹配分钟
    minutes_match = re.search(r'(\d+)m', duration_str)
    if minutes_match:
        total_seconds += int(minutes_match.group(1)) * 60

    # 匹配秒（可能包含小数点）
    seconds_match = re.search(r'([\d.]+)s', duration_str)
    if seconds_match:
        total_seconds += int(float(seconds_match.group(1)))

    return total_seconds


def _parse_429_error_details(error_message: str) -> Optional[Dict[str, Any]]:
    """
    解析 429 错误响应，提取配额重置信息

    Args:
        error_message: 错误消息字符串

    Returns:
        {
            "model": "gemini-3-pro-high",
            "quota_reset_timestamp": "2025-11-29T11:46:46Z",
            "quota_reset_delay_seconds": 8570
        }
        如果解析失败返回 None
    """
    try:
        # 尝试从错误消息中提取 JSON 部分
        # 错误格式通常是：API 请求失败 (429): {json_data}
        json_match = re.search(r'\{.*\}', error_message, re.DOTALL)
        if not json_match:
            return None

        error_json = json.loads(json_match.group(0))

        # 提取 error.details 中的元数据
        error_obj = error_json.get('error', {})
        details = error_obj.get('details', [])

        result = {}

        for detail in details:
            if detail.get('@type') == 'type.googleapis.com/google.rpc.ErrorInfo':
                metadata = detail.get('metadata', {})

                # 提取模型名称
                if 'model' in metadata:
                    result['model'] = metadata['model']

                # 提取配额重置时间戳
                if 'quotaResetTimeStamp' in metadata:
                    result['quota_reset_timestamp'] = metadata['quotaResetTimeStamp']

                # 提取配额重置延迟
                if 'quotaResetDelay' in metadata:
                    delay_str = metadata['quotaResetDelay']
                    result['quota_reset_delay_seconds'] = _parse_duration_to_seconds(delay_str)

        # 如果至少提取到了模型或延迟信息，返回结果
        if result.get('model') or result.get('quota_reset_delay_seconds'):
            return result

        return None

    except Exception as e:
        log.debug(f"Failed to parse 429 error details: {e}")
        return None


def _identify_model_series(model_name: str) -> Optional[str]:
    """
    识别模型所属系列

    Args:
        model_name: 模型名称（例如 "gemini-3-pro-high", "claude-sonnet-4-5", "gemini-2.5-pro"）

    Returns:
        "claude_series", "gemini_3_series", "gemini_2_series" 或 "gemini_series"
        如果无法识别返回 None
    """
    if not model_name:
        return None

    model_lower = model_name.lower()

    # Claude 系列
    if 'claude' in model_lower:
        return 'claude_series'

    # Gemini 3 系列
    if 'gemini-3' in model_lower or 'gemini3' in model_lower:
        return 'gemini_3_series'

    # Gemini 2.5 系列
    if 'gemini-2.5' in model_lower or 'gemini2.5' in model_lower:
        return 'gemini_2_5_series'

    # Gemini 2 系列
    if 'gemini-2' in model_lower or 'gemini2' in model_lower:
        return 'gemini_2_series'

    # 通用 Gemini 系列（兜底）
    if 'gemini' in model_lower:
        return 'gemini_series'

    return None


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

            # [CRITICAL FIX] 检查 None 返回值（读取失败）
            if accounts_data is None:
                log.error("[CRITICAL] Failed to load accounts.toml during discovery - keeping existing queue")
                log.error("[CRITICAL] This prevents clearing the queue from corrupt file reads")
                return  # 保留现有队列，不清空

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

            # 获取配置的 OAuth 端点（支持代理）
            oauth_endpoint = await get_antigravity_oauth_endpoint()
            log.debug(f"Using OAuth endpoint: {oauth_endpoint}")

            # 发送刷新请求
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    oauth_endpoint,
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
        
        支持保存的字段：
        - access_token, expires_in, timestamp（token 刷新）
        - project_id, has_antigravity_access（projectId 验证）

        Args:
            account: 账号数据字典
        """
        try:
            # 读取现有的 accounts.toml
            accounts_data = await self._storage_adapter.load_antigravity_accounts()

            # [CRITICAL FIX] 检查 None 返回值（读取失败）
            if accounts_data is None:
                log.error("[CRITICAL] Failed to load accounts.toml for saving - data read failed")
                return

            if not accounts_data or "accounts" not in accounts_data:
                log.error("Failed to load accounts.toml for saving")
                return

            # 查找并更新对应的账号（优先使用 user_id，其次使用 email）
            user_id = account.get("user_id")
            email = account.get("email")
            found = False

            for i, existing_account in enumerate(accounts_data["accounts"]):
                # 匹配条件：user_id 或 email
                if (user_id and existing_account.get("user_id") == user_id) or \
                   (email and existing_account.get("email") == email):
                    # 更新账号信息（保留其他字段）
                    update_fields = {}
                    
                    # Token 刷新相关字段
                    if "access_token" in account:
                        update_fields["access_token"] = account["access_token"]
                    if "expires_in" in account:
                        update_fields["expires_in"] = account["expires_in"]
                    if "timestamp" in account:
                        update_fields["timestamp"] = account["timestamp"]
                        update_fields["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    
                    # ProjectId 相关字段
                    if "project_id" in account:
                        update_fields["project_id"] = account["project_id"]
                    if "has_antigravity_access" in account:
                        update_fields["has_antigravity_access"] = account["has_antigravity_access"]
                    
                    accounts_data["accounts"][i].update(update_fields)
                    found = True
                    break

            if not found:
                log.warning(f"Account not found in accounts.toml (user_id={user_id}, email={email})")
                return

            # 保存回 accounts.toml
            await self._storage_adapter.save_antigravity_accounts(accounts_data)

            log.debug(f"Saved account data to accounts.toml: user_id={user_id}, email={email}")

        except Exception as e:
            log.error(f"Error saving account to storage: {e}")

    async def get_valid_credential(self, model_name: str = None, _checked_count: int = 0) -> Optional[Dict[str, Any]]:
        """
        获取有效的凭证，自动处理轮换和失效凭证切换

        Args:
            model_name: 模型名称（用于配额检查和系列封禁检查）
            _checked_count: 内部参数，已检查的账号数量（防止死循环）

        Returns:
            Dict with 'account' and 'virtual_filename' keys, or None if no valid credential
        """
        async with self._operation_lock:
            if not self._credential_accounts:
                await self._discover_credentials()
                if not self._credential_accounts:
                    return None

            # [FIX] 防止死循环：如果已检查账号数超过总账号数，说明所有账号都不可用
            if _checked_count >= len(self._credential_accounts):
                log.error(f"All {len(self._credential_accounts)} Antigravity accounts checked, none available for model {model_name}")
                return None

            # 检查是否需要轮换（基于调用次数）
            if await self._should_rotate():
                await self._rotate_credential()

            # 如果当前没有加载凭证，加载第一个
            if not self._current_credential_account:
                await self._load_current_credential()

            # 检查并处理 projectId（自动生成或验证）
            if self._current_credential_account:
                await self._ensure_project_id(self._current_credential_account)

            # 检查系列级临时封禁（如果提供了模型名称）
            if model_name and self._current_credential_account:
                is_banned = await self._check_series_ban(self._current_credential_account, model_name)

                if is_banned:
                    # 系列被封禁，切换到下一个账号
                    if len(self._credential_accounts) > 1:
                        log.info(f"Rotating to next account due to series ban (checked {_checked_count + 1}/{len(self._credential_accounts)})")
                        await self._rotate_credential()
                        await self._load_current_credential()

                        # 递归调用检查新账号，增加检查计数
                        return await self.get_valid_credential(model_name, _checked_count + 1)
                    else:
                        log.error("Only one account available and series is banned")
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
                            # 递归调用以确保新账号也是有效的，增加检查计数
                            return await self.get_valid_credential(model_name, _checked_count + 1)
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

    async def mark_credential_error(self, virtual_filename: str, error_code: int, error_message: str = ""):
        """
        标记凭证错误

        Args:
            virtual_filename: 虚拟文件名
            error_code: 错误码
            error_message: 完整错误消息（用于 429 错误解析）
        """
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

            # 特殊处理 429 错误：临时封禁系列
            if error_code == 429:
                await self._handle_429_series_ban(virtual_filename, error_message)
                return

            # 如果需要自动封禁，检查配置（非 429 错误）
            from config import get_auto_ban_enabled, get_auto_ban_error_codes

            auto_ban_enabled = await get_auto_ban_enabled()
            auto_ban_error_codes = await get_auto_ban_error_codes()

            if auto_ban_enabled and error_code in auto_ban_error_codes:
                await self.disable_credential(virtual_filename)

        except Exception as e:
            log.error(f"Error marking Antigravity credential error: {e}")

    async def _handle_429_series_ban(self, virtual_filename: str, error_message: str):
        """
        处理 429 错误：临时封禁特定模型系列

        Args:
            virtual_filename: 虚拟文件名
            error_message: 完整错误消息
        """
        try:
            # 解析 429 错误详情
            error_details = _parse_429_error_details(error_message)

            # 计算封禁时间
            ban_duration_seconds = 6 * 3600  # 默认 6 小时

            if error_details:
                delay_seconds = error_details.get('quota_reset_delay_seconds', 0)
                reset_timestamp_str = error_details.get('quota_reset_timestamp')

                if reset_timestamp_str:
                    # 使用 quotaResetTimeStamp
                    try:
                        reset_time = datetime.fromisoformat(reset_timestamp_str.replace('Z', '+00:00'))
                        now = datetime.now(timezone.utc)
                        api_duration = (reset_time - now).total_seconds()
                        # 封禁时间 = API 时间 × 2
                        ban_duration_seconds = max(api_duration * 2, 3600)  # 至少 1 小时
                    except Exception:
                        pass
                elif delay_seconds > 0:
                    # 使用 quotaResetDelay
                    ban_duration_seconds = delay_seconds * 2

            ban_until = datetime.now(timezone.utc) + timedelta(seconds=ban_duration_seconds)
            hours = ban_duration_seconds / 3600

            # 提取模型名称并识别系列
            model_name = error_details.get('model', '') if error_details else ''
            series = _identify_model_series(model_name)

            if series:
                # 有明确的模型系列，只封禁该系列
                await self._set_series_ban(virtual_filename, series, ban_until)
                log.warning(
                    f"[429 BAN] Account {virtual_filename}: {series} banned until {ban_until.isoformat()} "
                    f"(~{hours:.1f} hours) - Model: {model_name}"
                )
            else:
                # 无法识别模型系列（如 "Chat API requests" 配额错误）
                # 封禁所有常见系列
                log.warning(f"[429] Could not identify series for model: '{model_name}', applying global ban")
                all_series = ['gemini_series', 'gemini_2_series', 'gemini_2_5_series', 'gemini_3_series', 'claude_series']
                for s in all_series:
                    await self._set_series_ban(virtual_filename, s, ban_until)
                log.warning(
                    f"[429 BAN] Account {virtual_filename}: ALL SERIES banned until {ban_until.isoformat()} "
                    f"(~{hours:.1f} hours) - Reason: Global quota exceeded"
                )

        except Exception as e:
            log.error(f"Error handling 429 series ban: {e}")

    async def _set_series_ban(self, virtual_filename: str, series: str, ban_until: datetime):
        """
        设置系列级临时封禁

        Args:
            virtual_filename: 虚拟文件名
            series: 系列名称（"claude_series" 或 "gemini_3_series"）
            ban_until: 封禁到期时间（UTC）
        """
        try:
            # 读取 accounts.toml
            accounts_data = await self._storage_adapter.load_antigravity_accounts()

            if not accounts_data or "accounts" not in accounts_data:
                log.error("Failed to load accounts.toml for series ban")
                return

            # 提取 user_id
            user_id = virtual_filename.replace("userID_", "")
            field_name = f"{series}_banned_until"
            ban_until_str = ban_until.isoformat()

            # 查找并更新对应账号（文件）
            for i, account in enumerate(accounts_data["accounts"]):
                if account.get("user_id") == user_id:
                    # 设置系列封禁时间戳（ISO 格式）
                    accounts_data["accounts"][i][field_name] = ban_until_str
                    break

            # 保存回 accounts.toml
            await self._storage_adapter.save_antigravity_accounts(accounts_data)

            # [CRITICAL FIX] 同时更新内存中的账号数据，确保封禁立即生效
            for cred_entry in self._credential_accounts:
                if cred_entry.get("virtual_filename") == virtual_filename:
                    cred_entry["account"][field_name] = ban_until_str
                    log.info(f"[429 BAN] Updated in-memory ban for {virtual_filename}: {field_name} = {ban_until_str}")
                    break

            # 同时更新当前凭证（如果是当前凭证被封禁）
            if self._current_credential_account and self._current_credential_account.get("user_id") == user_id:
                self._current_credential_account[field_name] = ban_until_str

            log.debug(f"Set {series} ban for {virtual_filename} until {ban_until_str}")

        except Exception as e:
            log.error(f"Error setting series ban: {e}")

    async def _check_series_ban(self, account: Dict[str, Any], model_name: str) -> bool:
        """
        检查账号的指定系列是否被临时封禁

        Args:
            account: 账号数据
            model_name: 请求的模型名称

        Returns:
            True 如果被封禁，False 如果可用
        """
        try:
            # 识别模型系列
            series = _identify_model_series(model_name)
            if not series:
                return False  # 无法识别系列，允许使用

            # 检查封禁字段
            field_name = f"{series}_banned_until"
            ban_until_str = account.get(field_name)

            if not ban_until_str:
                return False  # 没有封禁记录

            # 解析封禁到期时间
            ban_until = datetime.fromisoformat(ban_until_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)

            if now < ban_until:
                # 仍在封禁期内
                remaining_seconds = (ban_until - now).total_seconds()
                remaining_hours = remaining_seconds / 3600
                log.info(
                    f"[429 BAN] Account {account.get('email')} - {series} still banned "
                    f"(~{remaining_hours:.1f} hours remaining)"
                )
                return True
            else:
                # 封禁已过期，自动解封
                log.info(f"[429 UNBAN] Account {account.get('email')} - {series} ban expired, auto-unbanning")
                await self._clear_series_ban(account.get('user_id'), series)
                return False

        except Exception as e:
            log.error(f"Error checking series ban: {e}")
            return False  # 检查失败时允许使用，避免阻塞

    async def _clear_series_ban(self, user_id: str, series: str):
        """
        清除系列封禁

        Args:
            user_id: 用户 ID
            series: 系列名称
        """
        try:
            # 读取 accounts.toml
            accounts_data = await self._storage_adapter.load_antigravity_accounts()

            if not accounts_data or "accounts" not in accounts_data:
                return

            # 查找并清除封禁字段
            for i, account in enumerate(accounts_data["accounts"]):
                if account.get("user_id") == user_id:
                    field_name = f"{series}_banned_until"
                    if field_name in accounts_data["accounts"][i]:
                        accounts_data["accounts"][i][field_name] = ""
                    break

            # 保存回 accounts.toml
            await self._storage_adapter.save_antigravity_accounts(accounts_data)

        except Exception as e:
            log.error(f"Error clearing series ban: {e}")

    async def _ensure_project_id(self, account: Dict[str, Any]) -> None:
        """
        确保账号有 projectId，如果没有则根据配置生成或验证

        Args:
            account: 账号数据字典
        """
        # 检查是否已有 projectId
        if 'project_id' in account and account['project_id']:
            return  # 已有，无需处理

        # 获取配置
        skip_verification = await get_antigravity_skip_project_verification()

        if skip_verification:
            # ===== Pro 账号模式：生成随机 projectId =====
            account['project_id'] = generate_project_id()
            log.info(f"[Pro Account] Generated random projectId: {account['project_id']} for {account.get('email', 'unknown')}")

            # 保存到存储
            await self._save_account_to_storage(account)

        else:
            # ===== 免费账号模式：API 验证获取 projectId =====
            log.info(f"[Free Account] Fetching projectId from API for {account.get('email', 'unknown')}")

            try:
                project_id = await self._fetch_project_id_from_api(account)

                if not project_id:
                    email = account.get('email', 'unknown')
                    log.warning(f"Account {email} has no Antigravity access (projectId not found)")
                    # [FIX] 标记账号无资格并禁用，避免重复尝试
                    account['has_antigravity_access'] = False
                    
                    # 查找并禁用该账号
                    for cred_entry in self._credential_accounts:
                        if cred_entry.get('account', {}).get('email') == email:
                            virtual_filename = cred_entry.get('virtual_filename')
                            if virtual_filename:
                                log.warning(f"[403] Disabling account {email} due to no Antigravity access")
                                await self.disable_credential(virtual_filename)
                            break
                    return

                account['project_id'] = project_id
                account['has_antigravity_access'] = True
                log.info(f"[Free Account] ProjectId verified: {project_id}")

                # 保存到存储
                await self._save_account_to_storage(account)

            except Exception as e:
                log.error(f"Failed to fetch projectId from API: {e}")
                # 验证失败，使用随机 projectId 作为降级方案（可能会失败）
                account['project_id'] = generate_project_id()
                log.warning(f"[Fallback] Using random projectId due to API error: {account['project_id']}")

    async def _fetch_project_id_from_api(self, account: Dict[str, Any]) -> Optional[str]:
        """
        从 Google API 获取 projectId

        Args:
            account: 账号数据字典

        Returns:
            projectId 或 None（如果账号无资格）
        """
        try:
            # 从配置的 API 端点构建 loadCodeAssist URL
            # 例如: https://ant00.0vv0.tech/daily/v1internal:streamGenerateContent?alt=sse
            #    -> https://ant00.0vv0.tech/daily/v1internal:loadCodeAssist
            api_endpoint = await get_antigravity_api_endpoint()
            
            # 提取基础 URL（去掉 :streamGenerateContent?alt=sse 部分）
            if ':streamGenerateContent' in api_endpoint:
                base_url = api_endpoint.split(':streamGenerateContent')[0]
            elif '/v1internal' in api_endpoint:
                base_url = api_endpoint.split('/v1internal')[0] + '/v1internal'
            else:
                # 降级到默认 URL
                base_url = 'https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal'
            
            load_code_assist_url = f"{base_url}:loadCodeAssist"
            log.debug(f"Using loadCodeAssist endpoint: {load_code_assist_url}")

            # 调用 loadCodeAssist API
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    load_code_assist_url,
                    headers={
                        'Host': 'daily-cloudcode-pa.sandbox.googleapis.com',
                        'User-Agent': 'antigravity/1.11.9 windows/amd64',
                        'Authorization': f"Bearer {account['access_token']}",
                        'Content-Type': 'application/json',
                        'Accept-Encoding': 'gzip'
                    },
                    json={'metadata': {'ideType': 'ANTIGRAVITY'}}
                )

            if response.status_code == 200:
                data = response.json()
                project_id = data.get('cloudaicompanionProject')
                return project_id

            elif response.status_code == 403:
                log.warning(f"Account {account.get('email')} has no permission (403)")
                return None

            elif response.status_code == 404:
                log.warning(f"Account {account.get('email')} not found (404)")
                return None

            else:
                log.error(f"Unexpected status code {response.status_code}: {response.text}")
                return None

        except Exception as e:
            log.error(f"Error fetching projectId: {e}")
            raise

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

    async def add_account(self, account_data: Dict[str, Any]):
        """
        新增或更新 Antigravity 账号，立即加入轮换队列

        Args:
            account_data: 账号数据，包含 email, password, user_id, access_token, refresh_token 等

        使用场景：
            - Antigravity OAuth 认证成功后调用
            - 手动添加账号后调用
            - 新账号立即参与轮换，无需等待轮询
        """
        async with self._operation_lock:
            try:
                # 1. 读取现有 accounts.toml
                accounts_data = await self._storage_adapter.load_antigravity_accounts()

                # [CRITICAL FIX] 检查 None 返回值（读取失败）
                if accounts_data is None:
                    log.error("[CRITICAL] Failed to load accounts.toml - refusing to add account")
                    log.error("[CRITICAL] This prevents data loss from corrupt file reads")
                    return False

                if not isinstance(accounts_data, dict):
                    accounts_data = {"accounts": []}

                if "accounts" not in accounts_data:
                    accounts_data["accounts"] = []

                # 2. 检查账号是否已存在（根据 email 或 user_id）
                existing_index = None
                new_email = account_data.get("email", "")
                new_user_id = account_data.get("user_id", "")

                for i, acc in enumerate(accounts_data["accounts"]):
                    if acc.get("email") == new_email or acc.get("user_id") == new_user_id:
                        existing_index = i
                        break

                if existing_index is not None:
                    # 更新现有账号
                    accounts_data["accounts"][existing_index] = account_data
                    log.info(f"[OK] Antigravity 账号 {new_email} 已更新")
                else:
                    # 添加新账号
                    accounts_data["accounts"].append(account_data)
                    log.info(f"[OK] Antigravity 新账号 {new_email} 已添加")

                # 3. 保存到 accounts.toml
                await self._storage_adapter.save_antigravity_accounts(accounts_data)

                # 4. 创建或更新状态记录
                virtual_filename = f"userID_{new_user_id}"
                existing_state = await self._storage_adapter.get_credential_state(virtual_filename)

                if not existing_state:
                    default_state = {
                        "disabled": False,
                        "error_codes": [],
                        "last_success": None,
                        "user_email": new_email,
                    }
                    await self._storage_adapter.update_credential_state(virtual_filename, default_state)

                # 5. 检查是否被禁用
                state = await self._storage_adapter.get_credential_state(virtual_filename)
                is_disabled = state.get("disabled", False) if state else False

                if is_disabled:
                    log.info(f"账号 {new_email} 已添加但处于禁用状态，不加入队列")
                    return

                # 6. ⚡ 立即加入轮换队列
                new_account_entry = {
                    "account": account_data,
                    "virtual_filename": virtual_filename,
                    "state": state,
                }

                # 检查是否已在队列中（根据 user_id）
                existing_queue_index = None
                for i, acc_entry in enumerate(self._credential_accounts):
                    if acc_entry["account"].get("user_id") == new_user_id:
                        existing_queue_index = i
                        break

                if existing_queue_index is not None:
                    # 更新队列中的账号数据
                    self._credential_accounts[existing_queue_index] = new_account_entry
                    log.info(f"[OK] Antigravity 账号 {new_email} 在队列中已更新")
                else:
                    # 添加到队列
                    self._credential_accounts.append(new_account_entry)
                    log.info(f"[OK] Antigravity 账号 {new_email} 已立即加入轮换队列 (队列大小: {len(self._credential_accounts)})")

            except Exception as e:
                log.error(f"添加 Antigravity 账号失败: {e}")
                raise

    async def refresh_accounts(self):
        """
        手动刷新账号列表（保留接口，用于特殊情况）

        使用场景：
            - 直接修改 accounts.toml 文件后手动刷新
            - 系统恢复后重新扫描
        """
        log.info("手动刷新 Antigravity 账号列表...")
        await self._discover_credentials()
        log.info(f"刷新完成，当前队列大小: {len(self._credential_accounts)}")


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
