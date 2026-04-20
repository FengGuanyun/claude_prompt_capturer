"""
Memory Compactor - 3层记忆压缩系统
基于会话摘要的多层记忆管理
"""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime


@dataclass
class Message:
    """一条消息"""
    role: str  # "user", "assistant", "system"
    content: str
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class CompactionResult:
    """压缩结果"""
    summary: str
    compacted_messages: List[Message]
    tokens_used: int


class MemoryCompactor:
    """
    3层记忆压缩系统:
    1. 短期记忆: 最近的消息（保持原样）
    2. 中期记忆: 中间的消息（压缩为摘要）
    3. 长期记忆: 整体项目上下文（高层次摘要）

    当会话过长时，自动触发压缩
    """

    # 配置
    PRESERVE_RECENT = 4  # 保留最近 N 条消息
    MID_THRESHOLD = 20   # 当消息数 > MID_THRESHOLD 时触发中期压缩
    LONG_THRESHOLD = 50 # 当消息数 > LONG_THRESHOLD 时触发长期压缩

    def __init__(self):
        self.messages: List[Message] = []
        self.long_term_summary: str = ""
        self.mid_term_summaries: List[str] = []
        self.total_compactions = 0

    def add_message(self, role: str, content: str):
        """添加消息"""
        self.messages.append(Message(
            role=role,
            content=content,
            timestamp=datetime.now().strftime("%H:%M:%S")
        ))

    def should_compact(self) -> bool:
        """检查是否需要压缩"""
        return len(self.messages) > self.MID_THRESHOLD

    def estimate_tokens(self, text: str) -> int:
        """简单估算 token 数（中文约 2 字符 = 1 token，英文约 4 字符 = 1 token）"""
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return chinese_chars // 2 + other_chars // 4

    def estimate_messages_tokens(self) -> int:
        """估算当前消息列表的 token 数"""
        total = 0
        for msg in self.messages:
            total += self.estimate_tokens(f"{msg.role}: {msg.content}")
        return total

    def _generate_summary(self, messages: List[Message]) -> str:
        """
        生成消息摘要
        实际实现中应该调用 LLM，但这里提供简化版本
        """
        if not messages:
            return ""

        # 简化摘要：提取关键信息
        summary_parts = []
        for msg in messages:
            if msg.role == "user":
                # 用户消息：提取意图
                content = msg.content[:100]
                if len(msg.content) > 100:
                    content += "..."
                summary_parts.append(f"User asked: {content}")
            elif msg.role == "assistant":
                # 助手消息：简化处理
                if msg.content.startswith("Task"):
                    summary_parts.append(f"Created task")
                elif "Written to" in msg.content or "Created" in msg.content:
                    summary_parts.append(f"Created/modified files")
                elif "Error" in msg.content:
                    summary_parts.append(f"Encountered error")

        if summary_parts:
            return "; ".join(summary_parts[:5])  # 最多5条
        return f"Summarized {len(messages)} messages"

    def compact(self, max_tokens: int = 8000) -> CompactionResult:
        """
        执行压缩
        将中间的消息压缩为摘要，保留最近的消息
        """
        if len(self.messages) <= self.PRESERVE_RECENT:
            return CompactionResult(
                summary="No compaction needed",
                compacted_messages=self.messages.copy(),
                tokens_used=self.estimate_messages_tokens()
            )

        # 保留最近的消息
        recent = self.messages[-self.PRESERVE_RECENT:]
        middle = self.messages[:-self.PRESERVE_RECENT]

        # 生成中期摘要
        mid_summary = self._generate_summary(middle)

        # 更新长期摘要
        if self.long_term_summary:
            self.long_term_summary += f"\n[Compaction #{self.total_compactions + 1}] {mid_summary}"
        else:
            self.long_term_summary = mid_summary

        self.mid_term_summaries.append(mid_summary)

        # 构建压缩后的消息列表
        compacted = []

        # 添加长期摘要（如果有）
        if self.long_term_summary:
            compacted.append(Message(
                role="system",
                content=f"[Session Summary - Earlier Work]\n{self.long_term_summary}"
            ))

        # 添加中间摘要
        if mid_summary:
            compacted.append(Message(
                role="system",
                content=f"[Recent Summary]\n{mid_summary}"
            ))

        # 添加最近消息
        compacted.extend(recent)

        # 更新状态
        self.messages = compacted
        self.total_compactions += 1

        return CompactionResult(
            summary=mid_summary,
            compacted_messages=compacted,
            tokens_used=self.estimate_messages_tokens()
        )

    def get_messages_for_llm(self) -> List[dict]:
        """获取发送给 LLM 的消息格式"""
        return [msg.to_dict() for msg in self.messages]

    def get_stats(self) -> dict:
        """获取记忆统计"""
        return {
            "total_messages": len(self.messages),
            "total_compactions": self.total_compactions,
            "long_term_summary_length": len(self.long_term_summary),
            "mid_term_summaries_count": len(self.mid_term_summaries),
            "estimated_tokens": self.estimate_messages_tokens()
        }

    def clear(self):
        """清空所有记忆"""
        self.messages.clear()
        self.long_term_summary = ""
        self.mid_term_summaries.clear()
        self.total_compactions = 0


# 全局记忆管理器
_memory = MemoryCompactor()


def get_memory_compactor() -> MemoryCompactor:
    """获取全局记忆管理器"""
    return _memory
