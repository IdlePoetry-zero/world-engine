"""User-actionable application failures with deliberately safe public text."""
from world_engine.domain.errors import Conflict


STORAGE_BUSY_CODE = "STORAGE_BUSY"
STORAGE_BUSY_MESSAGE = "世界存档当前正忙，请稍后使用同一请求重试。"


MODEL_OUTPUT_TRUNCATED_MESSAGE = "模型回复被截断。请检查职责路由的输出 Token 上限，或缩短当前输入后重新提交。"
OLLAMA_CONTEXT_LIMIT_MESSAGE = (
    "Ollama 上下文容量不足。请到「模型与运行 → 模型连接 → Ollama 上下文容量」增加容量，"
    "或缩短输入、选择较浅的深度后重试；若请求涉及行动，请先查看世界历史与调用记录。"
)


class ApplicationFailure(Conflict):
    def __init__(self, code: str, public_message: str, *, status_code: int = 409, retryable: bool = False):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.status_code = status_code
        self.retryable = retryable


class InvalidDialogueReply(ApplicationFailure):
    def __init__(self):
        super().__init__(
            "INVALID_DIALOGUE_REPLY",
            "人物回复格式无效，这次对话没有写入世界。你可以重新发送。",
            status_code=502,
        )
