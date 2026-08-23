# Tool Registry - generic tool management for execution agent server.
#
# Provides a registry of tools that can be called by name, and converts
# registered tools to OpenAI function-calling format for LLM tool use.
import json
from typing import Any, Callable, Dict, List, Optional
from logger import logger


class Tool:
    """Base class for execution agent tools."""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    def execute(self, **kwargs) -> str:
        raise NotImplementedError


class FunctionTool(Tool):
    """Tool wrapping a plain function."""

    def __init__(self, name: str, description: str,
                 parameters: Dict[str, Any],
                 fn: Callable[..., str]):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._fn = fn

    def execute(self, **kwargs) -> str:
        return self._fn(**kwargs)


class ToolRegistry:
    """Registry of tools available to the execution agent."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.name}")

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def call(self, name: str, **kwargs) -> str:
        tool = self._tools.get(name)
        if not tool:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            return tool.execute(**kwargs)
        except Exception as e:
            logger.error(f"Tool {name} execution failed: {e}")
            return json.dumps({"error": str(e)})

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """Convert registered tools to OpenAI function-calling format."""
        result = []
        for tool in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        return result

    def list_names(self) -> List[str]:
        return list(self._tools.keys())

    def clear(self) -> None:
        self._tools.clear()
