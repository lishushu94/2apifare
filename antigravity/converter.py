"""
格式转换器 - OpenAI 格式 ↔ Google Antigravity 格式
基于 antigravity2api-nodejs/src/utils/utils.js
"""
import json
import uuid
import random
from typing import Dict, Any, List, Optional

# 导入默认安全设置
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DEFAULT_SAFETY_SETTINGS


def generate_request_id() -> str:
    """生成请求 ID"""
    return f"agent-{uuid.uuid4()}"


def generate_session_id() -> str:
    """生成会话 ID（负数）"""
    return str(-random.randint(1_000_000_000_000_000_000, 9_999_999_999_999_999_999))


def generate_project_id() -> str:
    """生成项目 ID"""
    adjectives = ['useful', 'bright', 'swift', 'calm', 'bold']
    nouns = ['fuze', 'wave', 'spark', 'flow', 'core']
    random_adj = random.choice(adjectives)
    random_noun = random.choice(nouns)
    random_str = uuid.uuid4().hex[:5]
    return f"{random_adj}-{random_noun}-{random_str}"


def extract_images_from_content(content: Any) -> Dict[str, Any]:
    """
    从 OpenAI content 提取文本和图片

    Args:
        content: OpenAI 格式的 content（字符串或数组）

    Returns:
        {'text': str, 'images': [{'inlineData': {'mimeType': str, 'data': str}}]}
    """
    result = {'text': '', 'images': []}

    # 如果是字符串，直接返回
    if isinstance(content, str):
        result['text'] = content
        return result

    # 如果是数组（multimodal 格式）
    if isinstance(content, list):
        for item in content:
            if item.get('type') == 'text':
                result['text'] += item.get('text', '')
            elif item.get('type') == 'image_url':
                image_url = item.get('image_url', {}).get('url', '')

                # 匹配 data:image/{format};base64,{data} 格式
                if image_url.startswith('data:image/'):
                    try:
                        # 解析格式：data:image/png;base64,iVBORw0KGgo...
                        parts = image_url.split(';base64,')
                        if len(parts) == 2:
                            mime_type = parts[0].replace('data:', '')  # image/png
                            base64_data = parts[1]
                            result['images'].append({
                                'inlineData': {
                                    'mimeType': mime_type,
                                    'data': base64_data
                                }
                            })
                    except Exception as e:
                        print(f"Failed to parse image: {e}")

    return result


def openai_messages_to_antigravity(openai_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    转换 OpenAI 消息格式为 Google Antigravity 格式

    OpenAI:
    [
      {"role": "user", "content": "你好"},
      {"role": "assistant", "content": "你好！", "tool_calls": [...]},
      {"role": "tool", "tool_call_id": "call_xxx", "content": "结果"}
    ]

    Google Antigravity:
    [
      {"role": "user", "parts": [{"text": "你好"}]},
      {"role": "model", "parts": [{"text": "你好！"}, {"functionCall": {...}}]},
      {"role": "user", "parts": [{"functionResponse": {...}}]}
    ]
    """
    antigravity_messages = []

    for message in openai_messages:
        role = message.get('role')

        if role in ('user', 'system'):
            # 用户/系统消息
            extracted = extract_images_from_content(message.get('content', ''))
            parts = []
            if extracted['text']:
                parts.append({'text': extracted['text']})
            parts.extend(extracted['images'])

            antigravity_messages.append({
                'role': 'user',
                'parts': parts
            })

        elif role == 'assistant':
            # 助手消息
            content = message.get('content', '')
            tool_calls = message.get('tool_calls', [])

            parts = []

            # 添加文本内容
            if content and content.strip():
                parts.append({'text': content})

            # 添加函数调用
            for tool_call in tool_calls:
                parts.append({
                    'functionCall': {
                        'id': tool_call.get('id', ''),
                        'name': tool_call.get('function', {}).get('name', ''),
                        'args': {
                            'query': tool_call.get('function', {}).get('arguments', '{}')
                        }
                    }
                })

            # 如果上一条消息是 model，且当前只有 functionCall，合并到上一条
            if (antigravity_messages and
                antigravity_messages[-1]['role'] == 'model' and
                tool_calls and not content.strip()):
                antigravity_messages[-1]['parts'].extend(parts)
            else:
                antigravity_messages.append({
                    'role': 'model',
                    'parts': parts
                })

        elif role == 'tool':
            # 工具结果消息
            tool_call_id = message.get('tool_call_id', '')
            content = message.get('content', '')

            # 从之前的 model 消息中找到对应的 functionCall name
            function_name = ''
            for prev_msg in reversed(antigravity_messages):
                if prev_msg['role'] == 'model':
                    for part in prev_msg['parts']:
                        if 'functionCall' in part and part['functionCall'].get('id') == tool_call_id:
                            function_name = part['functionCall'].get('name', '')
                            break
                    if function_name:
                        break

            function_response = {
                'functionResponse': {
                    'id': tool_call_id,
                    'name': function_name,
                    'response': {
                        'output': content
                    }
                }
            }

            # 如果上一条消息是 user 且包含 functionResponse，合并
            if (antigravity_messages and
                antigravity_messages[-1]['role'] == 'user' and
                any('functionResponse' in part for part in antigravity_messages[-1]['parts'])):
                antigravity_messages[-1]['parts'].append(function_response)
            else:
                antigravity_messages.append({
                    'role': 'user',
                    'parts': [function_response]
                })

    return antigravity_messages


def convert_openai_tools_to_antigravity(openai_tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    转换 OpenAI tools 格式为 Google Antigravity 格式

    OpenAI:
    [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "获取天气",
          "parameters": {...}
        }
      }
    ]

    Google Antigravity:
    [
      {
        "functionDeclarations": [
          {
            "name": "get_weather",
            "description": "获取天气",
            "parameters": {...}
          }
        ]
      }
    ]
    """
    if not openai_tools:
        return []

    antigravity_tools = []
    for tool in openai_tools:
        function = tool.get('function', {})
        parameters = function.get('parameters', {}).copy()

        # 移除 $schema 字段（Google API 不需要）
        parameters.pop('$schema', None)

        antigravity_tools.append({
            'functionDeclarations': [
                {
                    'name': function.get('name', ''),
                    'description': function.get('description', ''),
                    'parameters': parameters
                }
            ]
        })

    return antigravity_tools


def generate_generation_config(
    parameters: Dict[str, Any],
    enable_thinking: bool,
    actual_model_name: str
) -> Dict[str, Any]:
    """
    生成 generationConfig

    Args:
        parameters: 用户提供的参数（temperature, top_p, max_tokens 等）
        enable_thinking: 是否启用 thinking 模式
        actual_model_name: 实际模型名称

    Returns:
        generationConfig 对象
    """
    config = {
        'topP': parameters.get('top_p', 0.95),
        'topK': parameters.get('top_k', 50),
        'temperature': parameters.get('temperature', 1),
        'candidateCount': 1,
        # 不设置 maxOutputTokens，让 API 使用默认值（可获得更长输出）
        'stopSequences': [
            '<|user|>',
            '<|bot|>',
            '<|context_request|>',
            '<|endoftext|>',
            '<|end_of_turn|>'
        ],
        'thinkingConfig': {
            'includeThoughts': enable_thinking,
            'thinkingBudget': 1024 if enable_thinking else 0
        }
    }

    # Claude 模型在 thinking 模式下不支持 topP
    if enable_thinking and 'claude' in actual_model_name.lower():
        config.pop('topP', None)

    return config


def generate_request_body(
    openai_messages: List[Dict[str, Any]],
    model_name: str,
    parameters: Dict[str, Any],
    openai_tools: Optional[List[Dict[str, Any]]] = None,
    system_instruction: str = "你是一个有帮助的 AI 助手。"
) -> Dict[str, Any]:
    """
    生成完整的 Google Antigravity API 请求体

    Args:
        openai_messages: OpenAI 格式的消息数组
        model_name: 模型名称
        parameters: 生成参数（temperature, top_p, max_tokens 等）
        openai_tools: OpenAI 格式的工具定义
        system_instruction: 系统指令

    Returns:
        完整的请求体
    """
    # 导入模型别名映射
    from config import ANTIGRAVITY_MODEL_ALIAS

    # 判断是否启用 thinking 模式
    enable_thinking = (
        model_name.endswith('-thinking') or
        model_name == 'gemini-2.5-pro' or
        model_name.startswith('gemini-3-pro-') or
        model_name == 'rev19-uic3-1p' or
        model_name == 'gemini-2.5-computer-use-preview-10-2025' or
        model_name == 'gpt-oss-120b-medium'
    )

    # 去除 -thinking 后缀
    actual_model_name = model_name[:-9] if model_name.endswith('-thinking') else model_name

    # 应用模型别名映射（用户友好名称 -> API内部名称）
    actual_model_name = ANTIGRAVITY_MODEL_ALIAS.get(actual_model_name, actual_model_name)

    # 转换消息和工具
    antigravity_messages = openai_messages_to_antigravity(openai_messages)
    antigravity_tools = convert_openai_tools_to_antigravity(openai_tools)

    # 构建请求体
    request_body = {
        'project': generate_project_id(),
        'requestId': generate_request_id(),
        'request': {
            'contents': antigravity_messages,
            'systemInstruction': {
                'role': 'user',
                'parts': [{'text': system_instruction}]
            },
            'tools': antigravity_tools,
            'toolConfig': {
                'functionCallingConfig': {
                    'mode': 'VALIDATED'
                }
            },
            'generationConfig': generate_generation_config(parameters, enable_thinking, actual_model_name),
            'safetySettings': DEFAULT_SAFETY_SETTINGS,  # 添加安全设置，全部关闭过滤
            'sessionId': generate_session_id()
        },
        'model': actual_model_name,
        'userAgent': 'antigravity'
    }

    return request_body
