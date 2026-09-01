import re


class ModelCallError(RuntimeError):
    """Raised when an external model-style API fails after timeout/retries."""

    EVENT_BY_COMPONENT = {
        "agent": "agent_model_call_failed",
        "embedding": "embedding_call_failed",
        "reranker": "reranker_call_failed",
    }

    LABEL_BY_COMPONENT = {
        "agent": "Agent 模型",
        "embedding": "Embedding 模型",
        "reranker": "Reranker 模型",
    }

    def __init__(
        self,
        component: str,
        detail: str,
        http_status: int | None = None,
        error_type: str | None = None,
    ) -> None:
        self.component = component
        self.detail = detail
        self.http_status = http_status
        self.error_type = error_type or "model_call_error"
        self.event_type = self.EVENT_BY_COMPONENT.get(component, "model_call_failed")
        label = self.LABEL_BY_COMPONENT.get(component, "模型")
        if self.error_type == "response_validation_error":
            self.user_message = f"模型响应格式校验失败（{label}）"
        else:
            self.user_message = f"模型调用失败（{label}）"
        super().__init__(f"{self.user_message}：{detail}")

    def as_task_error(self) -> dict:
        return {
            "code": self.event_type,
            "message": self.user_message,
            "component": self.component,
            "http_status": self.http_status,
            "error_type": self.error_type,
            "detail": str(self),
        }


def _extract_http_status(detail: str) -> int | None:
    match = re.search(r"(?:error code|status|http)\D*(\d{3})", detail, flags=re.IGNORECASE)
    if not match:
        return None
    status = int(match.group(1))
    if 100 <= status <= 599:
        return status
    return None


def _extract_error_type(detail: str) -> str | None:
    match = re.search(r"error_type=([A-Za-z0-9_.-]+)", detail)
    return match.group(1) if match else None


def classify_model_call_error(
    detail: str,
    default_component: str = "agent",
    http_status: int | None = None,
    error_type: str | None = None,
) -> ModelCallError:
    """Classify a lower-level model error message into a UI/event error."""

    lowered = detail.lower()
    if "rerank" in lowered or "reranker" in lowered:
        component = "reranker"
    elif "embed" in lowered or "embedding" in lowered:
        component = "embedding"
    else:
        component = default_component
    resolved_http_status = http_status if http_status is not None else _extract_http_status(detail)
    return ModelCallError(
        component,
        detail,
        http_status=resolved_http_status,
        error_type=error_type or _extract_error_type(detail),
    )
