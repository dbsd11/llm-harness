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

    def unregister(self, name: str) -> bool:
        """
        Unregister a tool (hot-swap support).

        Args:
            name: Tool name

        Returns:
            True if removed, False if not found
        """
        if name not in self._tools:
            logger.warning(f"Tool {name} not found, cannot unregister")
            return False
        del self._tools[name]
        logger.info(f"Tool unregistered: {name}")
        return True

    def get_tool_info(self, name: str) -> Dict[str, Any]:
        """
        Get full metadata for a tool.

        Args:
            name: Tool name

        Returns:
            Tool metadata dict

        Raises:
            ValueError: if tool not found
        """
        if name not in self._tools:
            raise ValueError(f"Tool not found: {name}")
        info = self._tools[name]
        return {
            "name": name,
            "description": info["description"],
            "parameters": info["parameters"],
        }

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """
        Convert all registered tools to OpenAI function-calling format.

        Returns:
            List of OpenAI tool definitions
        """
        tools = []
        for name, info in self._tools.items():
            tool_def = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
            # Build parameter schema from tool's parameters dict
            # Format: {param_name: {"type": str, "description": str, "required": bool, ...}}
            params = info.get("parameters", {})
            for param_name, param_info in params.items():
                if not isinstance(param_info, dict):
                    continue
                param_schema = {"type": param_info.get("type", "string")}
                if "description" in param_info:
                    param_schema["description"] = param_info["description"]
                tool_def["function"]["parameters"]["properties"][param_name] = param_schema
                if param_info.get("required", True):
                    tool_def["function"]["parameters"]["required"].append(param_name)
            tools.append(tool_def)
        return tools


# Global tool registry singleton
tool_registry = ToolRegistry()
