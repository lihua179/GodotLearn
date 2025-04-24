import os
import sys
import json
import subprocess
import threading
import queue
import time
import platform
from typing import Dict, Any, List, Optional, Tuple
import asyncio


# Basic MCP types (simplified for stdio transport)
class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ErrorCode:
    MethodNotFound = -32601
    InvalidParams = -32602
    InternalError = -32603
    # Add other relevant error codes


# Check if debug mode is enabled
DEBUG_MODE: bool = os.environ.get('DEBUG', 'false').lower() == 'true'
GODOT_DEBUG_MODE: bool = True  # Always use GODOT DEBUG MODE


def log_debug(message: str):
    """Log debug messages if debug mode is enabled."""
    if DEBUG_MODE:
        print(f"[DEBUG] {message}", file=sys.stderr)


def create_error_response(message: str, possible_solutions: List[str] = None) -> Dict[str, Any]:
    """Create a standardized error response with possible solutions."""
    print(f"[SERVER] Error response: {message}", file=sys.stderr)
    if possible_solutions:
        print(f"[SERVER] Possible solutions: {', '.join(possible_solutions)}", file=sys.stderr)

    response: Dict[str, Any] = {
        "content": [
            {"type": "text", "text": message},
        ],
        "isError": True,
    }

    if possible_solutions:
        response["content"].append({
            "type": "text",
            "text": "Possible solutions:\n- " + "\n- ".join(possible_solutions),
        })

    return response


def validate_path(path: str) -> bool:
    """Validate a path to prevent path traversal attacks."""
    # Basic validation to prevent path traversal
    if not path or '..' in path.split(os.sep):
        return False
    # Add more validation as needed
    return True


def is_godot_44_or_later(version: str) -> bool:
    """Check if the Godot version is 4.4 or later."""
    try:
        parts = version.split('.')
        if len(parts) >= 2:
            major = int(parts[0])
            minor = int(parts[1])
            return major > 4 or (major == 4 and minor >= 4)
    except ValueError:
        pass
    return False


class GodotServer:
    """Main server class for the Godot MCP server."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.active_process: Optional[subprocess.Popen] = None
        self.active_process_output: List[str] = []
        self.active_process_errors: List[str] = []
        self.godot_path: Optional[str] = None
        # Assuming the operations script is in the same directory structure
        self.operations_script_path: str = os.path.join(os.path.dirname(__file__),  'godot_operations.gd')
        self.validated_paths: Dict[str, bool] = {}
        self.strict_path_validation: bool = False

        # Parameter name mappings between snake_case and camelCase
        self.parameter_mappings: Dict[str, str] = {
            'project_path': 'projectPath',
            'scene_path': 'scenePath',
            'root_node_type': 'rootNodeType',
            'parent_node_path': 'parentNodePath',
            'node_type': 'nodeType',
            'node_name': 'nodeName',
            'texture_path': 'texturePath',
            'node_path': 'nodePath',
            'output_path': 'outputPath',
            'mesh_item_names': 'meshItemNames',
            'new_path': 'newPath',
            'file_path': 'filePath',
            'directory': 'directory',
            'recursive': 'recursive',
            'scene': 'scene',
        }

        # Reverse mapping from camelCase to snake_case
        self.reverse_parameter_mappings: Dict[str, str] = {v: k for k, v in self.parameter_mappings.items()}

        # Apply configuration if provided
        if config:
            if 'debugMode' in config:
                global DEBUG_MODE
                DEBUG_MODE = config['debugMode']
            if 'godotDebugMode' in config:
                global GODOT_DEBUG_MODE
                GODOT_DEBUG_MODE = config['godotDebugMode']
            if 'strictPathValidation' in config:
                self.strict_path_validation = config['strictPathValidation']

            # Store and validate custom Godot path if provided
            if 'godotPath' in config and config['godotPath']:
                normalized_path = os.path.normpath(config['godotPath'])
                self.godot_path = normalized_path
                log_debug(f"Custom Godot path provided: {self.godot_path}")

                # Validate immediately with sync check
                if not self.is_valid_godot_path_sync(self.godot_path):
                    print(f"[SERVER] Invalid custom Godot path provided: {self.godot_path}", file=sys.stderr)
                    self.godot_path = None  # Reset to trigger auto-detection later

        if DEBUG_MODE:
            print(f"[DEBUG] Operations script path: {self.operations_script_path}", file=sys.stderr)

        # Setup tool handlers (will be called by the request handler)
        self.tool_handlers = self.setup_tool_handlers()

        # Threading for reading stdout/stderr without blocking
        self._stdout_queue = queue.Queue()
        self._stderr_queue = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    def _enqueue_output(self, out, queue):
        for line in iter(out.readline, b''):
            queue.put(line.decode('utf-8').strip())
        out.close()

    def _start_output_threads(self, process: subprocess.Popen):
        self._stdout_queue = queue.Queue()
        self._stderr_queue = queue.Queue()

        self._stdout_thread = threading.Thread(target=self._enqueue_output, args=(process.stdout, self._stdout_queue))
        self._stderr_thread = threading.Thread(target=self._enqueue_output, args=(process.stderr, self._stderr_queue))

        self._stdout_thread.daemon = True
        self._stderr_thread.daemon = True

        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_output_queues(self):
        while not self._stdout_queue.empty():
            line = self._stdout_queue.get_nowait()
            self.active_process_output.append(line)
            log_debug(f"[Godot stdout] {line}")
        while not self._stderr_queue.empty():
            line = self._stderr_queue.get_nowait()
            self.active_process_errors.append(line)
            log_debug(f"[Godot stderr] {line}")

    def is_valid_godot_path_sync(self, path: str) -> bool:
        """Synchronous validation for constructor use."""
        try:
            log_debug(f"Quick-validating Godot path: {path}")
            return path.lower() == 'godot' or os.path.exists(path)
        except Exception as e:
            log_debug(f"Invalid Godot path: {path}, error: {e}")
            return False

    async def is_valid_godot_path(self, path: str) -> bool:
        """Validate if a Godot path is valid and executable."""
        # Check cache first
        if path in self.validated_paths:
            log_debug(f"Using cached validation for Godot path: {path} -> {self.validated_paths[path]}")
            return self.validated_paths[path]

        try:
            log_debug(f"Validating Godot path: {path}")

            # Check if the file exists (skip for 'godot' which might be in PATH)
            if path.lower() != 'godot' and not os.path.exists(path):
                log_debug(f"Path does not exist: {path}")
                self.validated_paths[path] = False
                return False

            # Try to execute Godot with --version flag
            command = [path, '--version'] if path.lower() != 'godot' else ['godot', '--version']
            # Use shell=True on Windows for 'godot' command to be found in PATH
            shell = platform.system() == 'Windows' and path.lower() == 'godot'
            log_debug(f"Executing validation command: {' '.join(command) if not shell else command}")
            process = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10, shell=shell)

            log_debug(f"Validation command stdout: {process.stdout.strip()}")
            log_debug(f"Validation command stderr: {process.stderr.strip()}")

            log_debug(f"Valid Godot path: {path}")
            self.validated_paths[path] = True
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as e:
            log_debug(f"Invalid Godot path: {path}, error: {e}")
            # If CalledProcessError, capture stdout/stderr if available
            if isinstance(e, subprocess.CalledProcessError):
                log_debug(f"Validation command stdout (error): {e.stdout.strip()}")
                log_debug(f"Validation command stderr (error): {e.stderr.strip()}")
            self.validated_paths[path] = False
            return False
        except Exception as e:
            log_debug(f"Unexpected error validating Godot path: {path}, error: {e}")
            self.validated_paths[path] = False
            return False

    async def detect_godot_path(self):
        """Detect the Godot executable path based on the operating system."""
        # If godot_path is already set and valid, use it
        if self.godot_path and await self.is_valid_godot_path(self.godot_path):
            log_debug(f"Using existing Godot path: {self.godot_path}")
            return

        # Check environment variable next
        if 'GODOT_PATH' in os.environ:
            normalized_path = os.path.normpath(os.environ['GODOT_PATH'])
            log_debug(f"Checking GODOT_PATH environment variable: {normalized_path}")
            if await self.is_valid_godot_path(normalized_path):
                self.godot_path = normalized_path
                log_debug(f"Using Godot path from environment: {self.godot_path}")
                return
            else:
                log_debug(f"GODOT_PATH environment variable is invalid")

        # Auto-detect based on platform
        os_platform = platform.system()
        log_debug(f"Auto-detecting Godot path for platform: {os_platform}")

        possible_paths: List[str] = [
            'godot',  # Check if 'godot' is in PATH first
        ]

        # Add platform-specific paths
        if os_platform == 'Darwin':  # macOS
            possible_paths.extend([
                '/Applications/Godot.app/Contents/MacOS/Godot',
                '/Applications/Godot_4.app/Contents/MacOS/Godot',
                os.path.join(os.path.expanduser('~'), 'Applications', 'Godot.app', 'Contents', 'MacOS', 'Godot'),
                os.path.join(os.path.expanduser('~'), 'Applications', 'Godot_4.app', 'Contents', 'MacOS', 'Godot')
            ])
        elif os_platform == 'Windows':
            possible_paths.extend([
                'D:\\godot\\Godot.exe',

                os.path.join(os.path.expanduser('~'), 'Godot', 'Godot.exe')
            ])
        elif os_platform == 'Linux':
            possible_paths.extend([
                '/usr/bin/godot',
                '/usr/local/bin/godot',
                '/snap/bin/godot',
                os.path.join(os.path.expanduser('~'), '.local', 'bin', 'godot')
            ])

        # Try each possible path
        for path in possible_paths:
            normalized_path = os.path.normpath(path)
            if await self.is_valid_godot_path(normalized_path):
                self.godot_path = normalized_path
                log_debug(f"Found Godot at: {normalized_path}")
                return

        # If we get here, we couldn't find Godot
        log_debug(f"Warning: Could not find Godot in common locations for {os_platform}")
        print(f"[SERVER] Could not find Godot in common locations for {os_platform}", file=sys.stderr)
        print(
            f"[SERVER] Set GODOT_PATH=/path/to/godot environment variable or pass {{ 'godotPath': '/path/to/godot' }} in the config to specify the correct path.",
            file=sys.stderr)

        if self.strict_path_validation:
            # In strict mode, throw an error
            raise RuntimeError(
                "Could not find a valid Godot executable. Set GODOT_PATH or provide a valid path in config.")
        else:
            # Fallback to a default path in non-strict mode; this may not be valid and requires user configuration for reliability
            if os_platform == 'Windows':
                self.godot_path = os.path.normpath('D:\\godot\\Godot.exe')
            elif os_platform == 'Darwin':
                self.godot_path = os.path.normpath('/Applications/Godot.app/Contents/MacOS/Godot')
            else:  # Default for Linux and others
                self.godot_path = os.path.normpath('/usr/bin/godot')

            log_debug(f"Using default path: {self.godot_path}, but this may not work.")
            print(f"[SERVER] Using default path: {self.godot_path}, but this may not work.", file=sys.stderr)
            print(
                f"[SERVER] This fallback behavior will be removed in a future version. Set strictPathValidation: true to opt-in to the new behavior.",
                file=sys.stderr)

    def cleanup(self):
        """Clean up resources when shutting down."""
        log_debug('Cleaning up resources')
        if self.active_process:
            log_debug('Killing active Godot process')
            try:
                self.active_process.terminate()  # Use terminate for graceful shutdown
                self.active_process.wait(timeout=5)  # Wait a bit
            except subprocess.TimeoutExpired:
                self.active_process.kill()  # Force kill if not terminated
            self.active_process = None
            self.active_process_output = []
            self.active_process_errors = []

    def normalize_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize parameters to camelCase format."""
        if not isinstance(params, dict):
            return params

        result: Dict[str, Any] = {}
        for key, value in params.items():
            normalized_key = self.parameter_mappings.get(key, key)
            if isinstance(value, dict):
                result[normalized_key] = self.normalize_parameters(value)
            else:
                result[normalized_key] = value
        return result

    def convert_camel_to_snake_case(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Convert camelCase keys to snake_case."""
        if not isinstance(params, dict):
            return params

        result: Dict[str, Any] = {}
        for key, value in params.items():
            # Convert camelCase to snake_case
            snake_key = self.reverse_parameter_mappings.get(key)
            if snake_key is None:
                # Generic camelCase to snake_case conversion
                snake_key = ''.join(['_' + i.lower() if i.isupper() else i for i in key]).lstrip('_')

            if isinstance(value, dict):
                result[snake_key] = self.convert_camel_to_snake_case(value)
            else:
                result[snake_key] = value
        return result

    async def execute_operation(self, operation: str, params: Dict[str, Any], project_path: str) -> Tuple[str, str]:
        """Execute a Godot operation using the operations script."""
        log_debug(f"Executing operation: {operation} in project: {project_path}")
        log_debug(f"Original operation params: {json.dumps(params)}")

        # Convert camelCase parameters to snake_case for Godot script
        snake_case_params = self.convert_camel_to_snake_case(params)
        log_debug(f"Converted snake_case params: {json.dumps(snake_case_params)}")

        # Ensure godot_path is set
        if not self.godot_path:
            await self.detect_godot_path()
            if not self.godot_path:
                raise RuntimeError('Could not find a valid Godot executable path')

        try:
            # Serialize the snake_case parameters to a valid JSON string
            params_json = json.dumps(snake_case_params)

            # Add debug arguments if debug mode is enabled
            debug_args = ['--debug-godot'] if GODOT_DEBUG_MODE else []

            # Construct the command
            # Use shell=True on Windows to handle paths with spaces and quotes correctly
            shell = platform.system() == 'Windows'
            # self.operations_script_path=r'D:\pywork\quant\a_mcp\godot_operations.gd'
            command = [
                self.godot_path,
                '--headless',
                '--path',
                project_path,
                '--script',
                self.operations_script_path,
                operation,
                params_json,  # Pass the JSON string as a single argument
                *debug_args,
            ]
            print('command',command)
            # If using shell=True on Windows, need to format the command string manually
            if shell:
                # Escape paths and JSON string for Windows command prompt
                escaped_godot_path = f'"{self.godot_path}"'
                escaped_project_path = f'"{project_path}"'
                escaped_script_path = f'"{self.operations_script_path}"'
                # JSON string needs careful escaping for Windows cmd
                # Replace backslashes with double backslashes, escape double quotes
                escaped_params_json = params_json.replace('\\', '\\\\').replace('"', '\\"')
                escaped_params_arg = f'"{escaped_params_json}"'  # Wrap in double quotes

                command_str = f'{escaped_godot_path} --headless --path {escaped_project_path} --script {escaped_script_path} {operation} {escaped_params_arg}'
                if debug_args:
                    command_str += ' ' + ' '.join(debug_args)
                log_debug(f"Command (Windows shell): {command_str}")
                process = subprocess.run(command_str, check=True, capture_output=True, text=True, shell=True)

            else:
                log_debug(f"Command (Non-Windows shell): {' '.join(command)}")
                process = subprocess.run(command, check=True, capture_output=True, text=True)

            log_debug(f"Operation stdout: {process.stdout.strip()}")
            log_debug(f"Operation stderr: {process.stderr.strip()}")

            return process.stdout, process.stderr

        except subprocess.CalledProcessError as e:

            # If the subprocess returns a non-zero exit code, it's an error
            log_debug(f"Operation failed with CalledProcessError. Return code: {e.returncode}")
            log_debug(f"Operation stdout (error): {e.stdout.strip()}")
            log_debug(f"Operation stderr (error): {e.stderr.strip()}")
            return e.stdout, e.stderr
        except Exception as e:
            log_debug(f"Unexpected error executing Godot operation: {e}")
            raise RuntimeError(f"Error executing Godot operation: {e}") from e

    async def get_project_structure_async(self, project_path: str) -> Dict[str, int]:
        """Get the structure of a Godot project asynchronously by counting files recursively."""
        structure = {
            "scenes": 0,
            "scripts": 0,
            "assets": 0,
            "other": 0,
        }

        try:
            for root, _, files in os.walk(project_path):
                # Skip hidden directories
                if os.path.basename(root).startswith('.'):
                    continue

                for file in files:
                    # Skip hidden files
                    if file.startswith('.'):
                        continue

                    file_path = os.path.join(root, file)
                    ext = os.path.splitext(file)[1].lower()

                    if ext == '.tscn':
                        structure['scenes'] += 1
                    elif ext in ['.gd', '.gdscript', '.cs']:
                        structure['scripts'] += 1
                    elif ext in ['.png', '.jpg', '.jpeg', '.webp', '.svg', '.ttf', '.wav', '.mp3', '.ogg']:
                        structure['assets'] += 1
                    else:
                        structure['other'] += 1
            return structure
        except Exception as e:
            log_debug(f"Error getting project structure asynchronously: {e}")
            return {
                "error": "Failed to get project structure",
                "scenes": 0,
                "scripts": 0,
                "assets": 0,
                "other": 0
            }

    def find_godot_projects(self, directory: str, recursive: bool) -> List[Dict[str, str]]:
        """Find Godot projects in a directory."""
        projects: List[Dict[str, str]] = []

        try:
            # Check if the directory itself is a Godot project
            project_file = os.path.join(directory, 'project.godot')
            if os.path.exists(project_file):
                projects.append({
                    'path': directory,
                    'name': os.path.basename(directory),
                })

            # If not recursive, only check immediate subdirectories
            if not recursive:
                for entry_name in os.listdir(directory):
                    entry_path = os.path.join(directory, entry_name)
                    if os.path.isdir(entry_path):
                        # Skip hidden directories
                        if entry_name.startswith('.'):
                            continue
                        subdir_project_file = os.path.join(entry_path, 'project.godot')
                        if os.path.exists(subdir_project_file):
                            projects.append({
                                'path': entry_path,
                                'name': entry_name,
                            })
            else:
                # Recursive search
                for root, dirs, _ in os.walk(directory):
                    # Modify dirs in-place to skip hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith('.')]

                    # Check if this directory is a Godot project
                    project_file = os.path.join(root, 'project.godot')
                    if os.path.exists(project_file):
                        projects.append({
                            'path': root,
                            'name': os.path.basename(root),
                        })

        except Exception as e:
            log_debug(f"Error searching directory {directory}: {e}")

        return projects

    def setup_tool_handlers(self) -> Dict[str, callable]:
        """Set up the tool handlers for the MCP server."""
        return {
            'list_tools': self.handle_list_tools,
            'launch_editor': self.handle_launch_editor,
            'run_project': self.handle_run_project,
            'get_debug_output': self.handle_get_debug_output,
            'stop_project': self.handle_stop_project,
            'get_godot_version': self.handle_get_godot_version,
            'list_projects': self.handle_list_projects,
            'get_project_info': self.handle_get_project_info,
            'create_scene': self.handle_create_scene,
            'add_node': self.handle_add_node,
            'load_sprite': self.handle_load_sprite,
            'export_mesh_library': self.handle_export_mesh_library,
            'save_scene': self.handle_save_scene,
            'get_uid': self.handle_get_uid,
            'update_project_uids': self.handle_update_project_uids,
        }

    async def handle_list_tools(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the list_tools request."""
        return {
            "tools": [
                {
                    "name": "launch_editor",
                    "description": "Launch Godot editor for a specific project",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                        },
                        "required": ["projectPath"],
                    },
                },
                {
                    "name": "run_project",
                    "description": "Run the Godot project and capture output",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "scene": {
                                "type": "string",
                                "description": "Optional: Specific scene to run",
                            },
                        },
                        "required": ["projectPath"],
                    },
                },
                {
                    "name": "get_debug_output",
                    "description": "Get the current debug output and errors",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
                {
                    "name": "stop_project",
                    "description": "Stop the currently running Godot project",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
                {
                    "name": "get_godot_version",
                    "description": "Get the installed Godot version",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
                {
                    "name": "list_projects",
                    "description": "List Godot projects in a directory",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "directory": {
                                "type": "string",
                                "description": "Directory to search for Godot projects",
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "Whether to search recursively (default: false)",
                            },
                        },
                        "required": ["directory"],
                    },
                },
                {
                    "name": "get_project_info",
                    "description": "Retrieve metadata about a Godot project",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                        },
                        "required": ["projectPath"],
                    },
                },
                {
                    "name": "create_scene",
                    "description": "Create a new Godot scene file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "scenePath": {
                                "type": "string",
                                "description": "Path where the scene file will be saved (relative to project)",
                            },
                            "rootNodeType": {
                                "type": "string",
                                "description": "Type of the root node (e.g., Node2D, Node3D)",
                                "default": "Node2D",
                            },
                        },
                        "required": ["projectPath", "scenePath"],
                    },
                },
                {
                    "name": "add_node",
                    "description": "Add a node to an existing scene",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "scenePath": {
                                "type": "string",
                                "description": "Path to the scene file (relative to project)",
                            },
                            "parentNodePath": {
                                "type": "string",
                                "description": "Path to the parent node (e.g., \"root\" or \"root/Player\")",
                                "default": "root",
                            },
                            "nodeType": {
                                "type": "string",
                                "description": "Type of node to add (e.g., Sprite2D, CollisionShape2D)",
                            },
                            "nodeName": {
                                "type": "string",
                                "description": "Name for the new node",
                            },
                            "properties": {
                                "type": "object",
                                "description": "Optional properties to set on the node",
                            },
                        },
                        "required": ["projectPath", "scenePath", "nodeType", "nodeName"],
                    },
                },
                {
                    "name": "load_sprite",
                    "description": "Load a sprite into a Sprite2D node",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "scenePath": {
                                "type": "string",
                                "description": "Path to the scene file (relative to project)",
                            },
                            "nodePath": {
                                "type": "string",
                                "description": "Path to the Sprite2D node (e.g., \"root/Player/Sprite2D\")",
                            },
                            "texturePath": {
                                "type": "string",
                                "description": "Path to the texture file (relative to project)",
                            },
                        },
                        "required": ["projectPath", "scenePath", "nodePath", "texturePath"],
                    },
                },
                {
                    "name": "export_mesh_library",
                    "description": "Export a scene as a MeshLibrary resource",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "scenePath": {
                                "type": "string",
                                "description": "Path to the scene file (.tscn) to export",
                            },
                            "outputPath": {
                                "type": "string",
                                "description": "Path where the mesh library (.res) will be saved",
                            },
                            "meshItemNames": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                },
                                "description": "Optional: Names of specific mesh items to include (defaults to all)",
                            },
                        },
                        "required": ["projectPath", "scenePath", "outputPath"],
                    },
                },
                {
                    "name": "save_scene",
                    "description": "Save changes to a scene file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "scenePath": {
                                "type": "string",
                                "description": "Path to the scene file (relative to project)",
                            },
                            "newPath": {
                                "type": "string",
                                "description": "Optional: New path to save the scene to (for creating variants)",
                            },
                        },
                        "required": ["projectPath", "scenePath"],
                    },
                },
                {
                    "name": "get_uid",
                    "description": "Get the UID for a specific file in a Godot project (for Godot 4.4+)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                            "filePath": {
                                "type": "string",
                                "description": "Path to the file (relative to project) for which to get the UID",
                            },
                        },
                        "required": ["projectPath", "filePath"],
                    },
                },
                {
                    "name": "update_project_uids",
                    "description": "Update UID references in a Godot project by resaving resources (for Godot 4.4+)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "projectPath": {
                                "type": "string",
                                "description": "Path to the Godot project directory",
                            },
                        },
                        "required": ["projectPath"],
                    },
                },
            ]
        }

    async def handle_launch_editor(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the launch_editor tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        if not project_path:
            return create_error_response(
                'Project path is required',
                ['Provide a valid path to a Godot project directory']
            )

        if not validate_path(project_path):
            return create_error_response(
                'Invalid project path',
                ['Provide a valid path without ".." or other potentially unsafe characters']
            )

        try:
            if not self.godot_path:
                await self.detect_godot_path()
                if not self.godot_path:
                    return create_error_response(
                        'Could not find a valid Godot executable path',
                        [
                            'Ensure Godot is installed correctly',
                            'Set GODOT_PATH environment variable to specify the correct path',
                        ]
                    )

            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            log_debug(f"Launching Godot editor for project: {project_path}")
            # Use shell=True on Windows to handle paths with spaces and quotes correctly
            shell = platform.system() == 'Windows'
            command = [self.godot_path, '-e', '--path', project_path]
            if shell:
                command_str = f'"{self.godot_path}" -e --path "{project_path}"'
                log_debug(f"Launch editor command (Windows shell): {command_str}")
                subprocess.Popen(command_str, shell=True)
            else:
                log_debug(f"Launch editor command (Non-Windows shell): {' '.join(command)}")
                subprocess.Popen(command)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Godot editor launched successfully for project at {project_path}.",
                    },
                ],
            }
        except Exception as e:
            return create_error_response(
                f"Failed to launch Godot editor: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_run_project(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the run_project tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        if not project_path:
            return create_error_response(
                'Project path is required',
                ['Provide a valid path to a Godot project directory']
            )

        if not validate_path(project_path):
            return create_error_response(
                'Invalid project path',
                ['Provide a valid path without ".." or other potentially unsafe characters']
            )

        try:
            if not self.godot_path:
                await self.detect_godot_path()
                if not self.godot_path:
                    return create_error_response(
                        'Could not find a valid Godot executable path',
                        [
                            'Ensure Godot is installed correctly',
                            'Set GODOT_PATH environment variable to specify the correct path',
                        ]
                    )

            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            # Kill any existing process
            if self.active_process:
                log_debug('Killing existing Godot process before starting a new one')
                self.cleanup()  # Use cleanup to terminate/kill

            cmd_args = ['-d', '--path', project_path]
            scene = args.get('scene')
            if scene and validate_path(scene):
                log_debug(f"Adding scene parameter: {scene}")
                cmd_args.append(scene)

            log_debug(f"Running Godot project: {project_path}")

            # Use shell=True on Windows to handle paths with spaces and quotes correctly
            shell = platform.system() == 'Windows'
            command = [self.godot_path, *cmd_args]
            if shell:
                # Manually construct command string for Windows shell
                escaped_godot_path = f'"{self.godot_path}"'
                escaped_project_path = f'"{project_path}"'
                command_str = f'{escaped_godot_path} -d --path {escaped_project_path}'
                if scene:
                    escaped_scene = f'"{scene}"'
                    command_str += f' {escaped_scene}'
                log_debug(f"Run project command (Windows shell): {command_str}")
                process = subprocess.Popen(command_str, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
            else:
                log_debug(f"Run project command (Non-Windows shell): {' '.join(command)}")
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            self.active_process = process
            self.active_process_output = []
            self.active_process_errors = []
            self._start_output_threads(process)

            # Check if process exited immediately (e.g., command not found)
            time.sleep(0.1)  # Give it a moment to start
            if process.poll() is not None:
                # Process exited, read any output/errors immediately
                self._read_output_queues()
                stdout_str = "\n".join(self.active_process_output)
                stderr_str = "\n".join(self.active_process_errors)
                self.active_process = None  # Clear active process
                return create_error_response(
                    f"Godot process exited immediately with code {process.returncode}",
                    [
                        f"Stdout: {stdout_str}",
                        f"Stderr: {stderr_str}",
                        "Check if the Godot executable path is correct",
                        "Verify the project path and scene path are valid",
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Godot project started in debug mode. Use get_debug_output to see output.",
                    },
                ],
            }
        except Exception as e:
            # Ensure process is cleaned up if an error occurs during startup
            if self.active_process:
                self.cleanup()
            return create_error_response(
                f"Failed to run Godot project: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_get_debug_output(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the get_debug_output tool."""
        if not self.active_process:
            return create_error_response(
                'No active Godot process.',
                [
                    'Use run_project to start a Godot project first',
                    'Check if the Godot process crashed unexpectedly',
                ]
            )

        # Read any new output from the queues
        self._read_output_queues()

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "output": self.active_process_output,
                            "errors": self.active_process_errors,
                        },
                        indent=2
                    ),
                },
            ],
        }

    async def handle_stop_project(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the stop_project tool."""
        if not self.active_process:
            return create_error_response(
                'No active Godot process to stop.',
                [
                    'Use run_project to start a Godot project first',
                    'The process may have already terminated',
                ]
            )

        log_debug('Stopping active Godot process')
        # Read any remaining output before stopping
        self._read_output_queues()

        self.cleanup()  # Use cleanup to terminate/kill

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "message": "Godot project stopped",
                            "finalOutput": self.active_process_output,
                            "finalErrors": self.active_process_errors,
                        },
                        indent=2
                    ),
                },
            ],
        }

    async def handle_get_godot_version(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the get_godot_version tool."""
        try:
            if not self.godot_path:
                await self.detect_godot_path()
                if not self.godot_path:
                    return create_error_response(
                        'Could not find a valid Godot executable path',
                        [
                            'Ensure Godot is installed correctly',
                            'Set GODOT_PATH environment variable to specify the correct path',
                        ]
                    )

            log_debug('Getting Godot version')
            # Use shell=True on Windows for 'godot' command to be found in PATH
            shell = platform.system() == 'Windows' and self.godot_path.lower() == 'godot'
            command = [self.godot_path, '--version']
            if shell:
                command_str = f'"{self.godot_path}" --version' if self.godot_path.lower() != 'godot' else 'godot --version'
                log_debug(f"Get version command (Windows shell): {command_str}")
                process = subprocess.run(command_str, check=True, capture_output=True, text=True, timeout=10,
                                         shell=True)
            else:
                log_debug(f"Get version command (Non-Windows shell): {' '.join(command)}")
                process = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)

            log_debug(f"Get version stdout: {process.stdout.strip()}")
            log_debug(f"Get version stderr: {process.stderr.strip()}")

            return {
                "content": [
                    {
                        "type": "text",
                        "text": process.stdout.strip(),
                    },
                ],
            }
        except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as e:
            log_debug(f"Get version failed with error: {e}")
            if isinstance(e, subprocess.CalledProcessError):
                log_debug(f"Get version stdout (error): {e.stdout.strip()}")
                log_debug(f"Get version stderr (error): {e.stderr.strip()}")
            return create_error_response(
                f"Failed to get Godot version: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                ]
            )
        except Exception as e:
            log_debug(f"Unexpected error getting Godot version: {e}")
            return create_error_response(
                f"Failed to get Godot version: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                ]
            )

    async def handle_list_projects(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the list_projects tool."""
        args = self.normalize_parameters(args)

        directory = args.get('directory')
        if not directory:
            return create_error_response(
                'Directory is required',
                ['Provide a valid directory path to search for Godot projects']
            )

        if not validate_path(directory):
            return create_error_response(
                'Invalid directory path',
                ['Provide a valid path without ".." or other potentially unsafe characters']
            )

        try:
            log_debug(f"Listing Godot projects in directory: {directory}")
            if not os.path.exists(directory):
                return create_error_response(
                    f"Directory does not exist: {directory}",
                    ['Provide a valid directory path that exists on the system']
                )

            recursive = args.get('recursive', False)
            projects = self.find_godot_projects(directory, recursive)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(projects, indent=2),
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to list projects: {e}")
            return create_error_response(
                f"Failed to list projects: {e}",
                [
                    'Ensure the directory exists and is accessible',
                    'Check if you have permission to read the directory',
                ]
            )

    async def handle_get_project_info(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the get_project_info tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        if not project_path:
            return create_error_response(
                'Project path is required',
                ['Provide a valid path to a Godot project directory']
            )

        if not validate_path(project_path):
            return create_error_response(
                'Invalid project path',
                ['Provide a valid path without ".." or other potentially unsafe characters']
            )

        try:
            if not self.godot_path:
                await self.detect_godot_path()
                if not self.godot_path:
                    return create_error_response(
                        'Could not find a valid Godot executable path',
                        [
                            'Ensure Godot is installed correctly',
                            'Set GODOT_PATH environment variable to specify the correct path',
                        ]
                    )

            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            log_debug(f"Getting project info for: {project_path}")

            # Get Godot version
            # Use shell=True on Windows for 'godot' command to be found in PATH
            shell = platform.system() == 'Windows' and self.godot_path.lower() == 'godot'
            command = [self.godot_path, '--version']
            try:
                if shell:
                    command_str = f'"{self.godot_path}" --version' if self.godot_path.lower() != 'godot' else 'godot --version'
                    log_debug(f"Get version command (Windows shell): {command_str}")
                    process = subprocess.run(command_str, check=True, capture_output=True, text=True, timeout=10,
                                             shell=True)
                else:
                    log_debug(f"Get version command (Non-Windows shell): {' '.join(command)}")
                    process = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)

                log_debug(f"Get version stdout: {process.stdout.strip()}")
                log_debug(f"Get version stderr: {process.stderr.strip()}")
                godot_version = process.stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as e:
                log_debug(f"Get version failed during project info: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    log_debug(f"Get version stdout (error): {e.stdout.strip()}")
                    log_debug(f"Get version stderr (error): {e.stderr.strip()}")
                # Continue with project info even if version fails, but log the error
                godot_version = f"Error getting version: {e}"

            # Get project structure using the recursive method
            project_structure = await self.get_project_structure_async(project_path)

            # Extract project name from project.godot file
            project_name = os.path.basename(project_path)
            try:
                with open(project_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip().startswith('config/name='):
                            # Extract the value within quotes
                            parts = line.strip().split('=', 1)
                            if len(parts) > 1:
                                name_value = parts[1].strip()
                                if name_value.startswith('"') and name_value.endswith('"'):
                                    project_name = name_value[1:-1]
                                    log_debug(f"Found project name in config: {project_name}")
                                    break  # Found the name, no need to read further
            except Exception as e:
                log_debug(f"Error reading project file: {e}")
                # Continue with default project name if extraction fails

            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "name": project_name,
                                "path": project_path,
                                "godotVersion": godot_version,
                                "structure": project_structure,
                            },
                            indent=2
                        ),
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to get project info: {e}")
            return create_error_response(
                f"Failed to get project info: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_create_scene(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the create_scene tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        scene_path = args.get('scenePath')
        root_node_type = args.get('rootNodeType', 'Node2D')

        if not project_path or not scene_path:
            return create_error_response(
                'Project path and scene path are required',
                ['Provide valid paths for both the project and the scene']
            )

        if not validate_path(project_path) or not validate_path(scene_path):
            return create_error_response(
                'Invalid path',
                ['Provide valid paths without ".." or other potentially unsafe characters']
            )

        try:
            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            params = {
                "scenePath": scene_path,
                "rootNodeType": root_node_type,
            }
            print(params,project_path)
            stdout, stderr = await self.execute_operation('create_scene', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to create scene: {stderr}",
                    [
                        'Check if the root node type is valid',
                        'Ensure you have write permissions to the scene path',
                        'Verify the scene path is valid',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Scene created successfully at: {scene_path}\n\nOutput: {stdout}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to create scene: {e}")
            return create_error_response(
                f"Failed to create scene: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_add_node(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the add_node tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        scene_path = args.get('scenePath')
        node_type = args.get('nodeType')
        node_name = args.get('nodeName')
        parent_node_path = args.get('parentNodePath', 'root')
        properties = args.get('properties', {})

        if not project_path or not scene_path or not node_type or not node_name:
            return create_error_response(
                'Missing required parameters',
                ['Provide projectPath, scenePath, nodeType, and nodeName']
            )

        if not validate_path(project_path) or not validate_path(scene_path):
            return create_error_response(
                'Invalid path',
                ['Provide valid paths without ".." or other potentially unsafe characters']
            )
        # Note: nodePath and parentNodePath are internal to the scene, not file paths, so no validate_path needed

        try:
            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            scene_full_path = os.path.join(project_path, scene_path)
            if not os.path.exists(scene_full_path):
                return create_error_response(
                    f"Scene file does not exist: {scene_path}",
                    [
                        'Ensure the scene path is correct',
                        'Use create_scene to create a new scene first',
                    ]
                )

            params = {
                "scenePath": scene_path,
                "parentNodePath": parent_node_path,
                "nodeType": node_type,
                "nodeName": node_name,
                "properties": properties,
            }

            stdout, stderr = await self.execute_operation('add_node', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to add node: {stderr}",
                    [
                        'Check if the node type is valid',
                        'Ensure the parent node path exists in the scene',
                        'Verify the scene file is valid',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Node '{node_name}' of type '{node_type}' added successfully to '{scene_path}'.\n\nOutput: {stdout}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to add node: {e}")
            return create_error_response(
                f"Failed to add node: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_load_sprite(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the load_sprite tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        scene_path = args.get('scenePath')
        node_path = args.get('nodePath')
        texture_path = args.get('texturePath')

        if not project_path or not scene_path or not node_path or not texture_path:
            return create_error_response(
                'Missing required parameters',
                ['Provide projectPath, scenePath, nodePath, and texturePath']
            )

        if (
                not validate_path(project_path) or
                not validate_path(scene_path) or
                not validate_path(texture_path)
        ):
            return create_error_response(
                'Invalid path',
                ['Provide valid paths without ".." or other potentially unsafe characters']
            )
        # Note: nodePath is internal to the scene, not a file path, so no validate_path needed

        try:
            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            scene_full_path = os.path.join(project_path, scene_path)
            if not os.path.exists(scene_full_path):
                return create_error_response(
                    f"Scene file does not exist: {scene_path}",
                    [
                        'Ensure the scene path is correct',
                        'Use create_scene to create a new scene first',
                    ]
                )

            texture_full_path = os.path.join(project_path, texture_path)
            if not os.path.exists(texture_full_path):
                return create_error_response(
                    f"Texture file does not exist: {texture_path}",
                    [
                        'Ensure the texture path is correct',
                        'Upload or create the texture file first',
                    ]
                )

            params = {
                "scenePath": scene_path,
                "nodePath": node_path,
                "texturePath": texture_path,
            }

            stdout, stderr = await self.execute_operation('load_sprite', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to load sprite: {stderr}",
                    [
                        'Check if the node path is correct',
                        'Ensure the node is a Sprite2D, Sprite3D, or TextureRect',
                        'Verify the texture file is a valid image format',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Sprite loaded successfully with texture: {texture_path}\n\nOutput: {stdout}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to load sprite: {e}")
            return create_error_response(
                f"Failed to load sprite: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_export_mesh_library(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the export_mesh_library tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        scene_path = args.get('scenePath')
        output_path = args.get('outputPath')
        mesh_item_names = args.get('meshItemNames')

        if not project_path or not scene_path or not output_path:
            return create_error_response(
                'Missing required parameters',
                ['Provide projectPath, scenePath, and outputPath']
            )

        if (
                not validate_path(project_path) or
                not validate_path(scene_path) or
                not validate_path(output_path)
        ):
            return create_error_response(
                'Invalid path',
                ['Provide valid paths without ".." or other potentially unsafe characters']
            )

        try:
            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            scene_full_path = os.path.join(project_path, scene_path)
            if not os.path.exists(scene_full_path):
                return create_error_response(
                    f"Scene file does not exist: {scene_path}",
                    [
                        'Ensure the scene path is correct',
                        'Use create_scene to create a new scene first',
                    ]
                )

            params: Dict[str, Any] = {
                "scenePath": scene_path,
                "outputPath": output_path,
            }

            if mesh_item_names is not None and isinstance(mesh_item_names, list):
                params["meshItemNames"] = mesh_item_names

            stdout, stderr = await self.execute_operation('export_mesh_library', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to export mesh library: {stderr}",
                    [
                        'Check if the scene contains valid 3D meshes',
                        'Ensure the output path is valid and writable',
                        'Verify the scene file is valid',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"MeshLibrary exported successfully to: {output_path}\n\nOutput: {stdout}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to export mesh library: {e}")
            return create_error_response(
                f"Failed to export mesh library: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_save_scene(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the save_scene tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        scene_path = args.get('scenePath')
        new_path = args.get('newPath')

        if not project_path or not scene_path:
            return create_error_response(
                'Missing required parameters',
                ['Provide projectPath and scenePath']
            )

        if not validate_path(project_path) or not validate_path(scene_path):
            return create_error_response(
                'Invalid path',
                ['Provide valid paths without ".." or other potentially unsafe characters']
            )

        if new_path and not validate_path(new_path):
            return create_error_response(
                'Invalid new path',
                ['Provide a valid new path without ".." or other potentially unsafe characters']
            )

        try:
            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            scene_full_path = os.path.join(project_path, scene_path)
            if not os.path.exists(scene_full_path):
                return create_error_response(
                    f"Scene file does not exist: {scene_path}",
                    [
                        'Ensure the scene path is correct',
                        'Use create_scene to create a new scene first',
                    ]
                )

            params: Dict[str, Any] = {
                "scenePath": scene_path,
            }

            if new_path:
                params["newPath"] = new_path

            stdout, stderr = await self.execute_operation('save_scene', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to save scene: {stderr}",
                    [
                        'Check if the scene file is valid',
                        'Ensure you have write permissions to the output path',
                        'Verify the scene can be properly packed',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            save_path = new_path if new_path else scene_path
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Scene saved successfully to: {save_path}\n\nOutput: {stdout}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to save scene: {e}")
            return create_error_response(
                f"Failed to save scene: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_get_uid(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the get_uid tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')
        file_path = args.get('filePath')

        if not project_path or not file_path:
            return create_error_response(
                'Missing required parameters',
                ['Provide projectPath and filePath']
            )

        if not validate_path(project_path) or not validate_path(file_path):
            return create_error_response(
                'Invalid path',
                ['Provide valid paths without ".." or other potentially unsafe characters']
            )

        try:
            if not self.godot_path:
                await self.detect_godot_path()
                if not self.godot_path:
                    return create_error_response(
                        'Could not find a valid Godot executable path',
                        [
                            'Ensure Godot is installed correctly',
                            'Set GODOT_PATH environment variable to specify the correct path',
                        ]
                    )

            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            file_full_path = os.path.join(project_path, file_path)
            if not os.path.exists(file_full_path):
                return create_error_response(
                    f"File does not exist: {file_path}",
                    ['Ensure the file path is correct']
                )

            # Get Godot version to check if UIDs are supported
            # Use shell=True on Windows for 'godot' command to be found in PATH
            shell = platform.system() == 'Windows' and self.godot_path.lower() == 'godot'
            command = [self.godot_path, '--version']
            try:
                if shell:
                    command_str = f'"{self.godot_path}" --version' if self.godot_path.lower() != 'godot' else 'godot --version'
                    log_debug(f"Get version command (Windows shell): {command_str}")
                    process = subprocess.run(command_str, check=True, capture_output=True, text=True, timeout=10,
                                             shell=True)
                else:
                    log_debug(f"Get version command (Non-Windows shell): {' '.join(command)}")
                    process = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)

                log_debug(f"Get version stdout: {process.stdout.strip()}")
                log_debug(f"Get version stderr: {process.stderr.strip()}")
                version = process.stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as e:
                log_debug(f"Get version failed during get UID: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    log_debug(f"Get version stdout (error): {e.stdout.strip()}")
                    log_debug(f"Get version stderr (error): {e.stderr.strip()}")
                return create_error_response(
                    f"Failed to get Godot version to check UID support: {e}",
                    [
                        'Ensure Godot is installed correctly',
                        'Check if the GODOT_PATH environment variable is set correctly',
                    ]
                )

            if not is_godot_44_or_later(version):
                return create_error_response(
                    f"UIDs are only supported in Godot 4.4 or later. Current version: {version}",
                    [
                        'Upgrade to Godot 4.4 or later to use UIDs',
                        'Use resource paths instead of UIDs for this version of Godot',
                    ]
                )

            params = {
                "filePath": file_path,
            }

            stdout, stderr = await self.execute_operation('get_uid', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to get UID: {stderr}",
                    [
                        'Check if the file is a valid Godot resource',
                        'Ensure the file path is correct',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"UID for {file_path}: {stdout.strip()}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to get UID: {e}")
            return create_error_response(
                f"Failed to get UID: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_update_project_uids(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the update_project_uids tool."""
        args = self.normalize_parameters(args)

        project_path = args.get('projectPath')

        if not project_path:
            return create_error_response(
                'Project path is required',
                ['Provide a valid path to a Godot project directory']
            )

        if not validate_path(project_path):
            return create_error_response(
                'Invalid project path',
                ['Provide a valid path without ".." or other potentially unsafe characters']
            )

        try:
            if not self.godot_path:
                await self.detect_godot_path()
                if not self.godot_path:
                    return create_error_response(
                        'Could not find a valid Godot executable path',
                        [
                            'Ensure Godot is installed correctly',
                            'Set GODOT_PATH environment variable to specify the correct path',
                        ]
                    )

            project_file = os.path.join(project_path, 'project.godot')
            if not os.path.exists(project_file):
                return create_error_response(
                    f"Not a valid Godot project: {project_path}",
                    [
                        'Ensure the path points to a directory containing a project.godot file',
                        'Use list_projects to find valid Godot projects',
                    ]
                )

            # Get Godot version to check if UIDs are supported
            # Use shell=True on Windows for 'godot' command to be found in PATH
            shell = platform.system() == 'Windows' and self.godot_path.lower() == 'godot'
            command = [self.godot_path, '--version']
            try:
                if shell:
                    command_str = f'"{self.godot_path}" --version' if self.godot_path.lower() != 'godot' else 'godot --version'
                    log_debug(f"Get version command (Windows shell): {command_str}")
                    process = subprocess.run(command_str, check=True, capture_output=True, text=True, timeout=10,
                                             shell=True)
                else:
                    log_debug(f"Get version command (Non-Windows shell): {' '.join(command)}")
                    process = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)

                log_debug(f"Get version stdout: {process.stdout.strip()}")
                log_debug(f"Get version stderr: {process.stderr.strip()}")
                version = process.stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as e:
                log_debug(f"Get version failed during update UIDs: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    log_debug(f"Get version stdout (error): {e.stdout.strip()}")
                    log_debug(f"Get version stderr (error): {e.stderr.strip()}")
                return create_error_response(
                    f"Failed to get Godot version to check UID support: {e}",
                    [
                        'Ensure Godot is installed correctly',
                        'Check if the GODOT_PATH environment variable is set correctly',
                    ]
                )

            if not is_godot_44_or_later(version):
                return create_error_response(
                    f"UIDs are only supported in Godot 4.4 or later. Current version: {version}",
                    [
                        'Upgrade to Godot 4.4 or later to use UIDs',
                        'Use resource paths instead of UIDs for this version of Godot',
                    ]
                )

            params = {
                "projectPath": project_path,
            }

            stdout, stderr = await self.execute_operation('resave_resources', params, project_path)

            if stderr and ("Failed to" in stderr or "error" in stderr.lower()):
                return create_error_response(
                    f"Failed to update project UIDs: {stderr}",
                    [
                        'Check if the project is valid',
                        'Ensure you have write permissions to the project directory',
                        f'Godot stdout: {stdout}'  # Include stdout for debugging
                    ]
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Project UIDs updated successfully.\n\nOutput: {stdout}",
                    },
                ],
            }
        except Exception as e:
            log_debug(f"Failed to update project UIDs: {e}")
            return create_error_response(
                f"Failed to update project UIDs: {e}",
                [
                    'Ensure Godot is installed correctly',
                    'Check if the GODOT_PATH environment variable is set correctly',
                    'Verify the project path is accessible',
                ]
            )

    async def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming MCP requests."""
        log_debug(f"Received request: {json.dumps(request)}")
        request_type = request.get('type')
        request_id = request.get('id')
        params = request.get('params', {})

        response: Dict[str, Any] = {
            "id": request_id,
            "result": None,
            "error": None,
        }

        try:
            # print(request_type)
            if request_type == 'list_tools':
                response['result'] = await self.handle_list_tools(params)
            elif request_type == 'call_tool':
                tool_name = params.get('name')
                tool_args = params.get('arguments', {})
                # print(tool_name,self.tool_handlers)
                if tool_name in self.tool_handlers:

                    response['result'] = await self.tool_handlers[tool_name](tool_args)
                else:
                    raise McpError(ErrorCode.MethodNotFound, f"Unknown tool: {tool_name}")
            else:
                raise McpError(ErrorCode.MethodNotFound, f"Unknown request type: {request_type}")

        except McpError as e:
            response['error'] = {"code": e.code, "message": e.message}
            # Also log the error to stderr for visibility
            # print(f"[SERVER] MCP Error: Code {e.code}, Message: {e.message}", file=sys.stderr)
        except Exception as e:
            response['error'] = {"code": ErrorCode.InternalError, "message": str(e)}
            # Also log the error to stderr for visibility
            print(f"[SERVER] Internal Error: {e}", file=sys.stderr)

        log_debug(f"Sending response: {json.dumps(response)}")
        return response

    async def run(self):
        """Run the MCP server."""
        try:
            # Detect Godot path before starting the server
            await self.detect_godot_path()

            if not self.godot_path:
                print("[SERVER] Failed to find a valid Godot executable path", file=sys.stderr)
                print("[SERVER] Please set GODOT_PATH environment variable or provide a valid path", file=sys.stderr)
                sys.exit(1)

            # Check if the path is valid
            is_valid = await self.is_valid_godot_path(self.godot_path)

            if not is_valid:
                if self.strict_path_validation:
                    # In strict mode, exit if the path is invalid
                    print(f"[SERVER] Invalid Godot path: {self.godot_path}", file=sys.stderr)
                    print("[SERVER] Please set a valid GODOT_PATH environment variable or provide a valid path",
                          file=sys.stderr)
                    sys.exit(1)
                else:
                    # In compatibility mode, warn but continue with the default path
                    print(f"[SERVER] Warning: Using potentially invalid Godot path: {self.godot_path}", file=sys.stderr)
                    print("[SERVER] This may cause issues when executing Godot commands", file=sys.stderr)
                    print(
                        "[SERVER] This fallback behavior will be removed in a future version. Set strictPathValidation: true to opt-in to the new behavior.",
                        file=sys.stderr)

            print(f"[SERVER] Using Godot at: {self.godot_path}", file=sys.stderr)
            print("Godot MCP server running on stdio", file=sys.stderr)

            # Simple stdio server loop
            while True:
                try:

                    line = sys.stdin.readline()
                    if not line:
                        break  # EOF
                    request = json.loads(line)
                    response = await self.handle_request(request)
                    print(json.dumps(response), flush=True)
                except json.JSONDecodeError:
                    print(json.dumps({"id": None, "result": None,
                                      "error": {"code": ErrorCode.InvalidParams, "message": "Invalid JSON"}}),
                          flush=True)
                except Exception as e:
                    print(json.dumps(
                        {"id": None, "result": None, "error": {"code": ErrorCode.InternalError, "message": str(e)}}),
                          flush=True)

        except Exception as e:
            print(f"[SERVER] Failed to start: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            self.cleanup()


# Main execution block
if __name__ == "__main__":
    # Basic argument parsing for config (optional)
    config: Dict[str, Any] = {}
    # You could add more sophisticated argument parsing here if needed

    server = GodotServer(config)

    # Run the async server
    asyncio.run(server.run())
