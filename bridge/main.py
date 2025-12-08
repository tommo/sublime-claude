#!/usr/bin/env python3
"""
Bridge process between Sublime Text (Python 3.8) and Claude Agent SDK (Python 3.10+).
Communicates via JSON-RPC over stdio.
"""
import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    PermissionResultAllow,
    PermissionResultDeny,
)


def serialize(obj: Any) -> Any:
    """Serialize SDK objects to JSON-compatible dicts."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    return obj


def send(msg: dict) -> None:
    """Send JSON message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_error(id: int | None, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


def send_result(id: int, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": id, "result": result})


def send_notification(method: str, params: Any) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


class Bridge:
    def __init__(self):
        self.client: ClaudeSDKClient | None = None
        self.options: ClaudeAgentOptions | None = None
        self.running = True
        self.current_task: asyncio.Task | None = None
        self.pending_permissions: dict[int, asyncio.Future] = {}
        self.permission_id = 0

    async def handle_request(self, req: dict) -> None:
        id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        try:
            if method == "initialize":
                await self.initialize(id, params)
            elif method == "query":
                await self.query(id, params)
            elif method == "interrupt":
                await self.interrupt(id)
            elif method == "shutdown":
                await self.shutdown(id)
            elif method == "permission_response":
                await self.handle_permission_response(id, params)
            else:
                send_error(id, -32601, f"Method not found: {method}")
        except Exception as e:
            send_error(id, -32000, str(e))

    async def initialize(self, id: int, params: dict) -> None:
        """Initialize the Claude SDK client."""
        resume_id = params.get("resume")
        fork_session = params.get("fork_session", False)
        cwd = params.get("cwd")

        # Change to project directory so SDK finds CLAUDE.md etc.
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)

        # Load MCP servers and agents from project settings
        mcp_servers = self._load_mcp_servers(cwd)
        agents = self._load_agents(cwd)

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"initialize: params={params}\n")
            f.write(f"  resume_id={resume_id}, fork={fork_session}, cwd={cwd}, actual_cwd={os.getcwd()}\n")
            f.write(f"  mcp_servers={list(mcp_servers.keys()) if mcp_servers else None}\n")
            f.write(f"  agents={list(agents.keys()) if agents else None}\n")

        options_dict = {
            "allowed_tools": params.get("allowed_tools", []),
            "permission_mode": params.get("permission_mode", "default"),
            "cwd": cwd,
            "system_prompt": params.get("system_prompt"),
            "can_use_tool": self.can_use_tool,
            "resume": resume_id,
            "fork_session": fork_session,
            "setting_sources": ["project"],
        }

        # Add MCP servers if found
        if mcp_servers:
            options_dict["mcp_servers"] = mcp_servers

        # Add agents if found
        if agents:
            options_dict["agents"] = agents

        self.options = ClaudeAgentOptions(**options_dict)

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"  options.resume={self.options.resume}, options.fork_session={self.options.fork_session}\n")
        self.client = ClaudeSDKClient(options=self.options)
        await self.client.connect()
        send_result(id, {
            "status": "initialized",
            "mcp_servers": list(mcp_servers.keys()) if mcp_servers else [],
            "agents": list(agents.keys()) if agents else [],
        })

    def _load_project_settings(self, cwd: str) -> dict:
        """Load project settings from .claude/settings.json or .mcp.json."""
        if not cwd:
            return {}

        # Try .claude/settings.json first
        settings_path = os.path.join(cwd, ".claude", "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  error loading {settings_path}: {e}\n")

        # Try .mcp.json (MCP servers only)
        mcp_path = os.path.join(cwd, ".mcp.json")
        if os.path.exists(mcp_path):
            try:
                with open(mcp_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  error loading {mcp_path}: {e}\n")

        return {}

    def _load_mcp_servers(self, cwd: str) -> dict:
        """Load MCP server config from project settings."""
        settings = self._load_project_settings(cwd)
        servers = settings.get("mcpServers", {})
        if servers:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  loaded MCP servers: {list(servers.keys())}\n")
        return servers

    def _load_agents(self, cwd: str) -> dict:
        """Load subagent definitions from project settings, plus built-in agents."""
        settings = self._load_project_settings(cwd)
        project_agents = settings.get("agents", {})

        # Built-in agents (can be overridden by project settings)
        builtin = {
            "planner": {
                "description": "Use at the START of complex tasks to create an implementation plan. Saves plan to blackboard.",
                "prompt": """You are a planning specialist. Create clear implementation plans.

When planning:
1. Break down the task into concrete steps
2. Identify files that need changes
3. Note any risks or dependencies
4. Save plan using bb_write tool with key "plan"

Keep plans concise and actionable. Output the plan, then save it.""",
                "tools": ["Read", "Glob", "Grep", "bb_write", "bb_read"],
                "model": "haiku"
            },
            "reporter": {
                "description": "Use AFTER completing significant work to update the walkthrough/progress report.",
                "prompt": """You are a progress reporter. Update the walkthrough for the user.

1. Use bb_read with key "walkthrough" to get current state
2. Update with: what was completed, current status, next steps
3. Use bb_write with key "walkthrough" to save (markdown format)

Be concise. Focus on what matters to the user.""",
                "tools": ["bb_write", "bb_read", "bb_list"],
                "model": "haiku"
            },
        }

        # Merge: project agents override built-ins
        merged = {**builtin, **project_agents}

        # Convert dicts to AgentDefinition objects
        agents = {}
        for name, config in merged.items():
            agents[name] = AgentDefinition(
                description=config.get("description", ""),
                prompt=config.get("prompt", ""),
                tools=config.get("tools"),
                model=config.get("model"),
            )

        if agents:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  loaded agents: {list(agents.keys())}\n")
        return agents

    async def can_use_tool(self, tool_name: str, tool_input: dict, context=None):
        """Handle permission request - ask Sublime for approval."""
        self.permission_id += 1
        pid = self.permission_id

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"can_use_tool: tool={tool_name}, pid={pid}, input={str(tool_input)[:100]}\n")

        # Create a future to wait for the response
        future = asyncio.get_event_loop().create_future()
        self.pending_permissions[pid] = future

        # Send permission request to Sublime
        send_notification("permission_request", {
            "id": pid,
            "tool": tool_name,
            "input": tool_input,
        })

        # Wait for response from Sublime
        try:
            allowed = await asyncio.wait_for(future, timeout=300)  # 5 min timeout
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"can_use_tool returning: pid={pid}, allowed={allowed}\n")
            if allowed:
                return PermissionResultAllow(updated_input=tool_input)
            else:
                return PermissionResultDeny(message="User denied permission")
        except asyncio.TimeoutError:
            return PermissionResultDeny(message="Permission request timed out")
        finally:
            self.pending_permissions.pop(pid, None)

    async def handle_permission_response(self, id: int, params: dict) -> None:
        """Handle permission response from Sublime."""
        pid = params.get("id")
        allow = params.get("allow", False)

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"permission_response: pid={pid}, allow={allow}\n")

        if pid in self.pending_permissions:
            future = self.pending_permissions[pid]
            future.set_result(allow)
        else:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  -> WARNING: pid {pid} not found in pending!\n")

        send_result(id, {"status": "ok"})

    async def query(self, id: int, params: dict) -> None:
        """Send a query and stream responses."""
        if not self.client:
            send_error(id, -32002, "Not initialized")
            return

        prompt = params.get("prompt", "")

        async def run_query():
            await self.client.query(prompt)
            async for message in self.client.receive_response():
                await self.emit_message(message)
            send_result(id, {"status": "complete"})

        self.current_task = asyncio.create_task(run_query())
        try:
            await self.current_task
        except asyncio.CancelledError:
            send_result(id, {"status": "interrupted"})

    async def emit_message(self, message: Any) -> None:
        """Emit a message notification."""
        msg_type = type(message).__name__
        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"emit_message: type={msg_type}\n")

        if isinstance(message, AssistantMessage):
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  blocks: {[type(b).__name__ for b in message.content]}\n")
            for block in message.content:
                if isinstance(block, TextBlock):
                    send_notification("message", {
                        "type": "text",
                        "text": block.text,
                    })
                elif isinstance(block, ToolUseBlock):
                    send_notification("message", {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                elif isinstance(block, ToolResultBlock):
                    with open("/tmp/claude_bridge.log", "a") as f:
                        f.write(f"tool_result: id={block.tool_use_id}, is_error={block.is_error}, content={str(block.content)[:200]}\n")
                    send_notification("message", {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    })
                elif isinstance(block, ThinkingBlock):
                    send_notification("message", {
                        "type": "thinking",
                        "thinking": block.thinking,
                    })
        elif isinstance(message, UserMessage):
            # UserMessage contains tool results
            content = message.content
            if isinstance(content, list):
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  UserMessage blocks: {[type(b).__name__ for b in content]}\n")
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        with open("/tmp/claude_bridge.log", "a") as f:
                            f.write(f"tool_result: id={block.tool_use_id}, is_error={block.is_error}\n")
                        send_notification("message", {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content if hasattr(block, 'content') else None,
                            "is_error": block.is_error,
                        })
        elif isinstance(message, ResultMessage):
            send_notification("message", {
                "type": "result",
                "session_id": message.session_id,
                "duration_ms": message.duration_ms,
                "is_error": message.is_error,
                "num_turns": message.num_turns,
                "total_cost_usd": message.total_cost_usd,
            })
        elif isinstance(message, SystemMessage):
            send_notification("message", {
                "type": "system",
                "subtype": message.subtype,
                "data": message.data,
            })

    async def interrupt(self, id: int) -> None:
        """Interrupt current query."""
        if self.current_task and not self.current_task.done():
            await self.client.interrupt()
            self.current_task.cancel()
        send_result(id, {"status": "interrupted"})

    async def shutdown(self, id: int) -> None:
        """Shutdown the bridge."""
        if self.client:
            await self.client.disconnect()
        send_result(id, {"status": "shutdown"})
        self.running = False

    async def run(self) -> None:
        """Main loop - read JSON-RPC from stdin."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self.running:
            try:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line.decode())
                # Don't await - handle requests concurrently so permission responses
                # can be processed while a query is running
                asyncio.create_task(self.handle_request(req))
            except json.JSONDecodeError as e:
                send_error(None, -32700, f"Parse error: {e}")
            except Exception as e:
                send_error(None, -32000, f"Internal error: {e}")


async def main():
    bridge = Bridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
