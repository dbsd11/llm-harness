"""Conversation history compression logic."""
from typing import List, Any


# Ponytail: 40 messages is ~20 conversation turns, reasonable threshold before context gets unwieldy
COMPRESS_THRESHOLD = 40
# Compress oldest 30 messages in one batch, keeping 10 most recent
COMPRESS_BATCH = 30


def should_compress(message_count: int) -> bool:
    """Determine if conversation history should be compressed."""
    return message_count > COMPRESS_THRESHOLD


def build_summary_prompt(messages: List[Any]) -> str:
    """Build LLM prompt to summarize conversation messages."""
    formatted = []
    for msg in messages:
        role_label = "用户" if msg.role == "user" else "助手"
        formatted.append(f"{role_label}: {msg.content}")

    conversation_text = "\n".join(formatted)

    return f"""请用2-3句话总结以下对话，保留关键事实和决策：

{conversation_text}"""


def compress_if_needed(session_id: str, repo, llm_client) -> str | None:
    """Compress oldest messages if count exceeds threshold.

    Returns summary text if compression occurred, None otherwise.
    """
    count = repo.count_messages(session_id)
    if not should_compress(count):
        return None

    messages = repo.find_old_messages(session_id, limit=COMPRESS_BATCH)
    if len(messages) < COMPRESS_BATCH:
        return None

    prompt = build_summary_prompt(messages)
    summary = llm_client.chat([{"role": "user", "content": prompt}], temperature=0.3)
    if not summary:
        return None

    repo.save("summary", summary, session_id)
    ids_to_delete = [msg.id for msg in messages]
    repo.delete_messages_by_ids(ids_to_delete)

    return summary
