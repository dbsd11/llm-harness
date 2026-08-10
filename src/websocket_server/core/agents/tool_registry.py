# Tool Registry - agent tool management system
from typing import Dict, Callable, Any, List
from logger import logger


class ToolRegistry:
    """
    Tool registry for scheduling agent.

    Manages registration, retrieval, and invocation of tools that agents can use.
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        func: Callable,
        description: str,
        parameters: Dict[str, Any] = None
    ):
        """
        Register a tool.

        Args:
            name: Tool name (unique identifier)
            func: Tool function
            description: Tool description
            parameters: Parameter schema (optional)
        """
        if name in self._tools:
            logger.warning(f"Tool {name} already registered, overwriting")

        self._tools[name] = {
            "func": func,
            "description": description,
            "parameters": parameters or {},
        }
        logger.info(f"Tool registered: {name}")

    def get(self, name: str) -> Callable:
        """
        Get a tool function by name.

        Args:
            name: Tool name

        Returns:
            Tool function

        Raises:
            ValueError: if tool not found
        """
        if name not in self._tools:
            raise ValueError(f"Tool not found: {name}")
        return self._tools[name]["func"]

    def call(self, name: str, **kwargs) -> Any:
        """
        Call a tool with arguments.

        Args:
            name: Tool name
            **kwargs: Tool arguments

        Returns:
            Tool result
        """
        func = self.get(name)
        return func(**kwargs)

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        List all registered tools.

        Returns:
            List of tool metadata dicts
        """
        return [
            {
                "name": name,
                "description": info["description"],
                "parameters": info["parameters"],
            }
            for name, info in self._tools.items()
        ]

    def has_tool(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools


# Global tool registry singleton
tool_registry = ToolRegistry()
