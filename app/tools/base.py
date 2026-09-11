"""工具抽象与注册表。每个工具自带 JSON Schema 描述，可无缝对接 LLM function calling。"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..core.models import ToolResult
    from ..core.context import RunContext


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict                       # JSON Schema: {"type":"object","properties":{...},"required":[...]}
    requires_confirm: bool = False         # 敏感操作（如退款）需用户确认后才真正执行


class BaseTool(abc.ABC):
    """所有工具的基类：业务系统接入时，继承并实现 run() 即可。"""
    spec: ToolSpec

    @abc.abstractmethod
    def run(self, ctx: "RunContext", **kwargs) -> "ToolResult": ...

    def openai_schema(self) -> dict:
        return {
            "name": self.spec.name,
            "description": self.spec.description,
            "parameters": self.spec.parameters,
        }


class ToolRegistry:
    """工具注册表：按名字登记/检索，并可输出 function calling 所需的 schema。"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        self._tools[tool.spec.name] = tool
        return self

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, allowed: set[str] | None = None) -> list[dict]:
        return [t.openai_schema() for n, t in self._tools.items()
                if allowed is None or n in allowed]
