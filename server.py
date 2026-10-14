"""ComfyUI MCP Server - Main entry point"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import requests

from mcp.server.fastmcp import FastMCP

from comfyui_client import ComfyUIClient
from managers.asset_registry import AssetRegistry
from managers.defaults_manager import DefaultsManager
from managers.publish_manager import PublishConfig, PublishManager
from managers.workflow_manager import WorkflowManager
from tools.asset import register_asset_tools
from tools.configuration import register_configuration_tools
from tools.generation import register_workflow_generation_tools, register_regenerate_tool
from tools.job import register_job_tools
from tools.publish import register_publish_tools
from tools.workflow import register_workflow_tools

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MCP_Server")

# Configuration paths
WORKFLOW_DIR = Path(os.getenv("COMFY_MCP_WORKFLOW_DIR", str(Path(__file__).parent / "workflows")))

# Asset registry configuration
ASSET_TTL_HOURS = int(os.getenv("COMFY_MCP_ASSET_TTL_HOURS", "24"))

# ComfyUI connection configuration
COMFYUI_URL = os.getenv("COMFYUI_URL", "http://localhost:8188")

# Publish configuration (optional env var for COMFYUI_OUTPUT_ROOT only)
COMFYUI_OUTPUT_ROOT = os.getenv("COMFYUI_OUTPUT_ROOT")


def say(message: str = "") -> None:
    """Print an operator-facing banner line to stderr: under --stdio, stdout carries JSON-RPC only."""
    print(message, file=sys.stderr)


def print_startup_banner():
    """Print a nice startup banner for the server."""
    say("\n" + "=" * 70)
    say("[*] ComfyUI-MCP-Server".center(70))
    say("=" * 70)
    say(f"  Connecting to ComfyUI at: {COMFYUI_URL}")
    say(f"  Workflow directory: {WORKFLOW_DIR}")
    say(f"  Asset TTL: {ASSET_TTL_HOURS} hours")
    say("=" * 70 + "\n")


def check_comfyui_available(base_url: str) -> bool:
    """Check if ComfyUI is available by attempting to fetch model list.
    
    Returns True if ComfyUI is responding, False otherwise.
    """
    try:
        response = requests.get(f"{base_url}/object_info/CheckpointLoaderSimple", timeout=5)
        if response.status_code == 200:
            # Try to parse the response to ensure it's valid
            data = response.json()
            checkpoint_info = data.get("CheckpointLoaderSimple", {})
            if isinstance(checkpoint_info, dict):
                return True
        return False
    except (requests.RequestException, ValueError, KeyError):
        return False


# Print startup banner
print_startup_banner()

# ComfyUI may be asleep and started on demand, so its absence is not fatal: the server
# comes up anyway, reads the checkpoint list once ComfyUI answers, and a generation
# call made while it is still down returns "ComfyUI is not running".
COMFYUI_AVAILABLE_AT_START = check_comfyui_available(COMFYUI_URL)
if not COMFYUI_AVAILABLE_AT_START:
    say("[!] ComfyUI is not running at " + COMFYUI_URL + "; starting anyway, tools connect once it is up.")
    logger.warning(f"ComfyUI not available at {COMFYUI_URL} at startup; continuing without it")

# Global ComfyUI client (fallback since context isn't available)
comfyui_client = ComfyUIClient(COMFYUI_URL)
workflow_manager = WorkflowManager(WORKFLOW_DIR)
defaults_manager = DefaultsManager(comfyui_client)
asset_registry = AssetRegistry(ttl_hours=ASSET_TTL_HOURS, comfyui_base_url=COMFYUI_URL)

# Publish manager (always initialized, uses auto-detection)
try:
    publish_config = PublishConfig(
        comfyui_output_root=COMFYUI_OUTPUT_ROOT,
        comfyui_url=COMFYUI_URL
    )
    publish_manager = PublishManager(publish_config)
    logger.info(f"Publish manager initialized with project_root={publish_config.project_root} (method: {publish_config.project_root_method})")
    logger.info(f"Publish root: {publish_config.publish_root}")
    if publish_config.comfyui_output_root:
        logger.info(f"ComfyUI output root: {publish_config.comfyui_output_root} (method: {publish_config.comfyui_output_method})")
    else:
        logger.info(f"ComfyUI output root: not configured (tried {len(publish_config.comfyui_tried_paths)} paths)")
except Exception as e:
    logger.warning(f"Failed to initialize publish manager: {e}. Publishing features may be unavailable.")
    # Still create a minimal manager so tools can register and return errors
    try:
        from managers.publish_manager import PublishConfig, PublishManager
        publish_config = PublishConfig(comfyui_url=COMFYUI_URL)
        publish_manager = PublishManager(publish_config)
    except Exception:
        publish_manager = None


# Define application context (for future use)
class AppContext:
    def __init__(self, comfyui_client: ComfyUIClient):
        self.comfyui_client = comfyui_client


# Lifespan management (placeholder for future context support)
@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage application lifecycle"""
    logger.info("Starting MCP server lifecycle...")
    try:
        # Startup: Could add ComfyUI health check here in the future
        logger.info("ComfyUI client initialized globally")
        yield AppContext(comfyui_client=comfyui_client)
    finally:
        # Shutdown: Cleanup (if needed)
        logger.info("Shutting down MCP server")


# Initialize FastMCP with lifespan and port configuration
# Using port 9000 for consistency with previous version
# Enable stateless_http to avoid requiring session management
mcp = FastMCP(
    "ComfyUI_MCP_Server",
    lifespan=app_lifespan,
    port=9000,
    stateless_http=True
)

# Register all MCP tools
register_configuration_tools(mcp, comfyui_client, defaults_manager)
register_workflow_tools(mcp, workflow_manager, comfyui_client, defaults_manager, asset_registry)
register_asset_tools(mcp, asset_registry)
register_workflow_generation_tools(mcp, workflow_manager, comfyui_client, defaults_manager, asset_registry)
register_regenerate_tool(mcp, comfyui_client, asset_registry)
register_job_tools(mcp, comfyui_client, asset_registry)
# Always register publish tools (unconditional)
if publish_manager:
    register_publish_tools(mcp, asset_registry, publish_manager)
else:
    logger.error("Publish manager not available - publish tools will not be registered")

if __name__ == "__main__":
    # Check if running as MCP command (stdio) or standalone (streamable-http)
    # When run as command by MCP client (like Cursor), use stdio transport
    # When run standalone, use streamable-http for HTTP access
    comfyui_state = "up" if COMFYUI_AVAILABLE_AT_START else "not running yet"
    if len(sys.argv) > 1 and sys.argv[1] == "--stdio":
        say("\n" + "=" * 70)
        say("[+] Server Ready".center(70))
        say("=" * 70)
        say(f"  Transport: stdio (for MCP clients)")
        say(f"  ComfyUI at {COMFYUI_URL}: {comfyui_state}")
        say("=" * 70 + "\n")
        logger.info("Starting MCP server with stdio transport (for MCP clients)")
        logger.info(f"ComfyUI at {COMFYUI_URL}: {comfyui_state}")
        try:
            mcp.run(transport="stdio")
        except KeyboardInterrupt:
            say("\n[*] Server stopped.")
    else:
        say("\n" + "=" * 70)
        say("[+] Server Ready".center(70))
        say("=" * 70)
        say(f"  Transport: streamable-http")
        say(f"  Endpoint: http://127.0.0.1:9000/mcp")
        say(f"  ComfyUI at {COMFYUI_URL}: {comfyui_state}")
        say("=" * 70 + "\n")
        logger.info("Starting MCP server with streamable-http transport on http://127.0.0.1:9000/mcp")
        logger.info(f"ComfyUI at {COMFYUI_URL}: {comfyui_state}")
        try:
            mcp.run(transport="streamable-http")
        except KeyboardInterrupt:
            say("\n[*] Server stopped.")
