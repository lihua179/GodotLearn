import os
import sys
import json
import subprocess
import threading
import queue
import time
import platform
import atexit
from typing import Dict, Any, List, Optional, Tuple

# Import FastMCP
from mcp.server.fastmcp import FastMCP


# Basic MCP types (simplified for stdio transport) - Keep for context or potential use
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


# Create FastMCP instance
mcp = FastMCP("Godot")


class GodotMCP:  # Renamed from GodotServer
    """Main class for Godot MCP tools, using FastMCP."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.active_process: Optional[subprocess.Popen] = None
        self.active_process_output: List[str] = []
        self.active_process_errors: List[str] = []
        self.godot_path: Optional[str] = None
        # Assuming the operations script is in the same directory structure
        self.operations_script_path: str = os.path.join(os.path.dirname(__file__), 'src', 'scripts',
                                                        'godot_operations.gd')
        self.validated_paths: Dict[str, bool] = {}
        self.strict_path_validation: bool = False

        # Parameter name mappings (camelCase to snake_case for internal use)
        # Kept for execute_operation parameter conversion
        self.reverse_parameter_mappings: Dict[str, str] = {
            'projectPath': 'project_path',
            'scenePath': 'scene_path',
            'rootNodeType': 'root_node_type',
            'parentNodePath': 'parent_node_path',
            'nodeType': 'node_type',
            'nodeName': 'node_name',
            'texturePath': 'texture_path',
            'nodePath': 'node_path',
            'outputPath': 'output_path',
            'meshItemNames': 'mesh_item_names',
            'newPath': 'new_path',
            'filePath': 'file_path',
            'directory': 'directory',
            'recursive': 'recursive',
            'scene': 'scene',
        }

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

        # Removed self.tool_handlers setup, FastMCP handles this

        # Threading for reading stdout/stderr without blocking (still needed for run_project)
        self._stdout_queue = queue.Queue()
        self._stderr_queue = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        # Detect Godot path during initialization
        self.detect_godot_path_sync()
        if not self.godot_path:
            # Handle case where detection fails immediately
            if self.strict_path_validation:
                raise RuntimeError("Failed to find a valid Godot executable path during initialization.")
            else:
                print("[SERVER] Warning: Failed to find Godot path during init, using fallback. Tools may fail.",
                      file=sys.stderr)

        # Register tools with FastMCP
        self._register_tools()
        # Register cleanup function
        atexit.register(self.cleanup)

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

    def is_valid_godot_path(self, path: str) -> bool:
        """Validate if a Godot path is valid and executable (Synchronous version)."""
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
                log_debug(f"Validation command stdout (error): {e.stdout.strip() if e.stdout else ''}")
                log_debug(f"Validation command stderr (error): {e.stderr.strip() if e.stderr else ''}")
            self.validated_paths[path] = False
            return False
        except Exception as e:
            log_debug(f"Unexpected error validating Godot path: {path}, error: {e}")
            self.validated_paths[path] = False
            return False

    def detect_godot_path_sync(self):
        """Detect the Godot executable path based on the operating system (Synchronous version)."""
        # If godot_path is already set and valid, use it
        if self.godot_path and self.is_valid_godot_path(self.godot_path):
            log_debug(f"Using existing Godot path: {self.godot_path}")
            return

        # Check environment variable next
        if 'GODOT_PATH' in os.environ:
            normalized_path = os.path.normpath(os.environ['GODOT_PATH'])
            log_debug(f"Checking GODOT_PATH environment variable: {normalized_path}")
            if self.is_valid_godot_path(normalized_path):
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
                'C:\\Program Files\\Godot\\Godot.exe',
                'C:\\Program Files (x86)\\Godot\\Godot.exe',
                'C:\\Program Files\\Godot_4\\Godot.exe',
                'C:\\Program Files (x86)\\Godot_4\\Godot.exe',
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
            if self.is_valid_godot_path(normalized_path):
                self.godot_path = normalized_path
                log_debug(f"Found Godot at: {normalized_path}")
                print(f"[SERVER] Using Godot at: {self.godot_path}", file=sys.stderr)  # Log found path
                return

        # If we get here, we couldn't find Godot
        log_debug(f"Warning: Could not find Godot in common locations for {os_platform}")
        print(f"[SERVER] Could not find Godot in common locations for {os_platform}", file=sys.stderr)
        print(
            f"[SERVER] Set GODOT_PATH=/path/to/godot environment variable or pass {{ 'godotPath': '/path/to/godot' }} in the config to specify the correct path.",
            file=sys.stderr)

        if self.strict_path_validation:
            # In strict mode, set path to None to signal failure
            self.godot_path = None
            print("[SERVER] Strict path validation enabled, could not find Godot.", file=sys.stderr)
        else:
            # Fallback to a default path in non-strict mode; this may not be valid
            if os_platform == 'Windows':
                fallback_path = os.path.normpath('C:\\Program Files\\Godot\\Godot.exe')
            elif os_platform == 'Darwin':
                fallback_path = os.path.normpath('/Applications/Godot.app/Contents/MacOS/Godot')
            else:  # Default for Linux and others
                fallback_path = os.path.normpath('/usr/bin/godot')

            self.godot_path = fallback_path
            log_debug(f"Using fallback default path: {self.godot_path}, but this may not work.")
            print(f"[SERVER] Using fallback default path: {self.godot_path}, but this may not work.", file=sys.stderr)
            print(
                f"[SERVER] This fallback behavior will be removed in a future version. Set strictPathValidation: true to opt-in to the new behavior.",
                file=sys.stderr)
            # Still log the used path
            print(f"[SERVER] Using Godot at: {self.godot_path}", file=sys.stderr)

    def cleanup(self):
        """Clean up resources when shutting down."""
        log_debug('Cleaning up resources')
        if self.active_process:
            log_debug('Killing active Godot process')
            try:
                # Check if process is still running before terminating
                if self.active_process.poll() is None:
                    self.active_process.terminate()  # Use terminate for graceful shutdown
                    self.active_process.wait(timeout=5)  # Wait a bit
            except subprocess.TimeoutExpired:
                if self.active_process.poll() is None:  # Check again before killing
                    self.active_process.kill()  # Force kill if not terminated
            except Exception as e:
                log_debug(f"Error during process termination: {e}")  # Log potential errors during cleanup

            self.active_process = None
            self.active_process_output = []
            self.active_process_errors = []

    def convert_camel_to_snake_case(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Convert camelCase keys to snake_case for internal Godot script use."""
        if not isinstance(params, dict):
            return params

        result: Dict[str, Any] = {}
        for key, value in params.items():
            # Use predefined mapping first
            snake_key = self.reverse_parameter_mappings.get(key)
            if snake_key is None:
                # Generic camelCase to snake_case conversion if not in map
                snake_key = ''.join(['_' + i.lower() if i.isupper() else i for i in key]).lstrip('_')
                log_debug(f"Generic camel->snake conversion: {key} -> {snake_key}")

            if isinstance(value, dict):
                result[snake_key] = self.convert_camel_to_snake_case(value)  # Recurse for nested dicts
            elif isinstance(value, list):
                # Handle lists of dictionaries recursively if needed
                result[snake_key] = [self.convert_camel_to_snake_case(item) if isinstance(item, dict) else item for item
                                     in value]
            else:
                result[snake_key] = value
        return result

    def execute_operation(self, operation: str, params: Dict[str, Any], project_path: str) -> Tuple[str, str]:
        """Execute a Godot operation using the operations script (Synchronous version)."""
        log_debug(f"Executing operation: {operation} in project: {project_path}")
        log_debug(f"Original operation params (camelCase): {json.dumps(params)}")

        # Convert camelCase parameters to snake_case for Godot script
        snake_case_params = self.convert_camel_to_snake_case(params)
        log_debug(f"Converted snake_case params: {json.dumps(snake_case_params)}")

        # Ensure godot_path is set
        if not self.godot_path:
            self.detect_godot_path_sync()
            if not self.godot_path:
                raise RuntimeError('Could not find a valid Godot executable path')

        try:
            # Serialize the snake_case parameters to a valid JSON string
            params_json = json.dumps(snake_case_params, separators=(',', ':'))

            # Add debug arguments if debug mode is enabled
            debug_args = ['--debug-godot'] if GODOT_DEBUG_MODE else []

            # Use shell=True on Windows to handle paths with spaces and quotes correctly
            shell = platform.system() == 'Windows'
            if shell:
                # Escape paths for Windows command prompt
                escaped_godot_path = f'"{self.godot_path}"'
                escaped_project_path = f'"{project_path}"'
                escaped_script_path = f'"{self.operations_script_path}"'
                # Just wrap the JSON string in double quotes
                escaped_params_arg = f'"{params_json}"'

                command_str = f'{escaped_godot_path} --headless --path {escaped_project_path} --script {escaped_script_path} {operation} {escaped_params_arg}'
                if debug_args:
                    command_str += ' ' + ' '.join(debug_args)
                log_debug(f"Command (Windows shell): {command_str}")
                process = subprocess.run(command_str, check=True, capture_output=True, text=True, shell=True,
                                         timeout=60)  # Added timeout
            else:
                # Non-Windows or shell=False case (original logic)
                command = [
                    self.godot_path,
                    '--headless',
                    '--path',
                    project_path,
                    '--script',
                    self.operations_script_path,
                    operation,
                    params_json,  # Pass the JSON string as a direct argument
                    *debug_args,
                ]
                log_debug(f"Command (Non-Windows shell): {' '.join(command)}")
                process = subprocess.run(command, check=True, capture_output=True, text=True,
                                         timeout=60)  # Added timeout

            log_debug(f"Operation stdout: {process.stdout.strip()}")
            log_debug(f"Operation stderr: {process.stderr.strip()}")

            return process.stdout, process.stderr

        except subprocess.CalledProcessError as e:
            log_debug(f"Operation failed with CalledProcessError. Return code: {e.returncode}")
            log_debug(f"Operation stdout (error): {e.stdout.strip() if e.stdout else ''}")
            log_debug(f"Operation stderr (error): {e.stderr.strip() if e.stderr else ''}")
            return e.stdout, e.stderr
        except subprocess.TimeoutExpired as e:
            log_debug(f"Godot operation timed out: {operation}")
            log_debug(f"Operation stdout (timeout): {e.stdout.strip() if e.stdout else ''}")
            log_debug(f"Operation stderr (timeout): {e.stderr.strip() if e.stderr else ''}")
            raise RuntimeError(f"Godot operation '{operation}' timed out") from e
        except Exception as e:
            log_debug(f"Unexpected error executing Godot operation: {e}")
            raise RuntimeError(f"Error executing Godot operation '{operation}': {e}") from e

    def get_project_structure(self, project_path: str) -> Dict[str, Any]:
        """Get the structure of a Godot project by counting files recursively (Synchronous)."""
        structure = {
            "scenes": 0,
            "scripts": 0,
            "assets": 0,
            "other": 0,
        }

        try:
            for root, dirs, files in os.walk(project_path):
                # Skip hidden directories like .git, .import
                dirs[:] = [d for d in dirs if not d.startswith('.')]  # Modify dirs in-place

                for file in files:
                    # Skip hidden files
                    if file.startswith('.'):
                        continue

                    # Skip import metadata files generated by Godot
                    if file.endswith('.import'):
                        continue

                    ext = os.path.splitext(file)[1].lower()

                    if ext == '.tscn':
                        structure['scenes'] += 1
                    elif ext in ['.gd', '.gdscript', '.cs']:
                        structure['scripts'] += 1
                    elif ext in ['.png', '.jpg', '.jpeg', '.webp', '.svg', '.ttf', '.wav', '.mp3', '.ogg', '.glb',
                                 '.gltf', '.obj', '.tres', '.res']:  # Added more asset types
                        structure['assets'] += 1
                    # Don't count project.godot as 'other'
                    elif file == 'project.godot':
                        continue
                    else:
                        structure['other'] += 1
            return structure
        except Exception as e:
            log_debug(f"Error getting project structure: {e}")
            # Return structure with error flag or raise? Raise is better for FastMCP
            raise RuntimeError(f"Failed to get project structure for {project_path}: {e}") from e

    def find_godot_projects(self, directory: str, recursive: bool) -> List[Dict[str, str]]:
        """Find Godot projects in a directory."""
        projects: List[Dict[str, str]] = []
        abs_directory = os.path.abspath(directory)  # Use absolute path for consistency

        try:
            log_debug(f"Searching for projects in '{abs_directory}', recursive={recursive}")
            # Check if the directory itself is a Godot project
            project_file = os.path.join(abs_directory, 'project.godot')
            if os.path.exists(project_file):
                projects.append({
                    'path': abs_directory,  # Return absolute path
                    'name': os.path.basename(abs_directory),
                })
                log_debug(f"Found project at root: {abs_directory}")
                # If found at root, don't search subdirs unless recursive?
                # Current logic allows finding root AND subdirs if recursive. Let's keep it.

            # Walk the directory structure
            if recursive:
                for root, dirs, _ in os.walk(abs_directory, topdown=True):
                    # Skip hidden directories like .git, .venv, etc.
                    dirs[:] = [d for d in dirs if not d.startswith('.')]

                    # Check if this directory contains project.godot
                    # Avoid re-adding the root directory if it was already added
                    if root != abs_directory:
                        project_file = os.path.join(root, 'project.godot')
                        if os.path.exists(project_file):
                            abs_root = os.path.abspath(root)
                            projects.append({
                                'path': abs_root,  # Return absolute path
                                'name': os.path.basename(abs_root),
                            })
                            log_debug(f"Found project recursively at: {abs_root}")
                            # Don't recurse further into a found project directory?
                            # This prevents finding nested projects if desired.
                            # For now, let's allow finding nested ones. To prevent: dirs[:] = []

            else:  # Not recursive, check only immediate subdirectories
                for entry_name in os.listdir(abs_directory):
                    entry_path = os.path.join(abs_directory, entry_name)
                    if os.path.isdir(entry_path):
                        # Skip hidden directories
                        if entry_name.startswith('.'):
                            continue
                        subdir_project_file = os.path.join(entry_path, 'project.godot')
                        if os.path.exists(subdir_project_file):
                            abs_entry_path = os.path.abspath(entry_path)
                            projects.append({
                                'path': abs_entry_path,  # Return absolute path
                                'name': entry_name,
                            })
                            log_debug(f"Found project in immediate subdir: {abs_entry_path}")

        except FileNotFoundError:
            raise FileNotFoundError(f"Directory not found: {directory}")
        except PermissionError:
            raise PermissionError(f"Permission denied accessing directory: {directory}")
        except Exception as e:
            log_debug(f"Error searching directory {directory}: {e}")
            # Raise the error for FastMCP
            raise RuntimeError(f"Failed to list projects in {directory}: {e}") from e

        # Remove duplicates just in case (e.g., if root and recursive overlap)
        unique_projects = []
        seen_paths = set()
        for proj in projects:
            if proj['path'] not in seen_paths:
                unique_projects.append(proj)
                seen_paths.add(proj['path'])

        log_debug(f"Found {len(unique_projects)} unique projects.")
        return unique_projects

    def _register_tools(self):
        """Registers methods with the FastMCP instance."""
        # Manually register each method decorated implicitly
        mcp.tool()(self.launch_editor)
        mcp.tool()(self.run_project)
        mcp.tool()(self.get_debug_output)
        mcp.tool()(self.stop_project)
        mcp.tool()(self.get_godot_version)
        mcp.tool()(self.list_projects)
        mcp.tool()(self.get_project_info)
        mcp.tool()(self.create_scene)
        mcp.tool()(self.add_node)
        mcp.tool()(self.load_sprite)
        mcp.tool()(self.export_mesh_library)
        mcp.tool()(self.save_scene)
        mcp.tool()(self.get_uid)
        mcp.tool()(self.update_project_uids)
        log_debug("Registered Godot tools with FastMCP")

    # --- Tool Methods ---
    # Note: Docstrings become descriptions. Parameter names are camelCase.
    # Return values are the success payload. Errors are raised exceptions.

    def launch_editor(self, projectPath: str, scene: Optional[str] = None) -> Dict[str, Any]:
        """Launch Godot editor for a specific project"""
        if not projectPath:
            raise ValueError('Project path is required')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath):  # Basic validation still useful
            raise ValueError('Invalid project path format (e.g., contains "..")')

        try:
            if not self.godot_path:
                self.detect_godot_path_sync()  # Try detecting again
                if not self.godot_path:
                    raise RuntimeError('Could not find a valid Godot executable path')

            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            log_debug(f"Launching Godot editor for project: {projectPath}")
            # Use shell=True on Windows to handle paths with spaces and quotes correctly
            shell = platform.system() == 'Windows'
            command = [self.godot_path, '-e', '--path', projectPath]
            if shell:
                # Manually construct command string for Windows shell
                escaped_godot_path = f'"{self.godot_path}"'
                escaped_project_path = f'"{projectPath}"'
                command_str = f'{escaped_godot_path} -e --path {escaped_project_path}'
                if scene:
                    # Scene path likely doesn't need quotes unless it has spaces, but safer to add
                    escaped_scene = f'"{scene}"'
                    command_str += f' {escaped_scene}'
                log_debug(f"Launch editor command (Windows shell): {command_str}")
                # Use Popen for non-blocking launch
                subprocess.Popen(command_str, shell=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)  # Redirect output
            else:
                log_debug(f"Launch editor command (Non-Windows shell): {' '.join(command)}")
                # Use Popen for non-blocking launch
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # Redirect output

            return {
                "message": f"Godot editor launch initiated for project at {projectPath}."
            }
        except FileNotFoundError as e:
            log_debug(f"Error launching editor: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to launch editor: {e}")
            raise RuntimeError(f"Failed to launch Godot editor: {e}") from e

    def run_project(self, projectPath: str, scene: Optional[str] = None) -> Dict[str, Any]:
        """Run the Godot project and capture output"""
        if not projectPath:
            raise ValueError('Project path is required')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath):  # Basic validation still useful
            raise ValueError('Invalid project path format (e.g., contains "..")')
        if scene and not validate_path(scene):  # Validate scene if provided (it's a relative path usually)
            raise ValueError('Invalid scene path format (e.g., contains "..")')

        try:
            if not self.godot_path:
                self.detect_godot_path_sync()  # Try detecting again
                if not self.godot_path:
                    raise RuntimeError('Could not find a valid Godot executable path')

            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            # Kill any existing process
            if self.active_process and self.active_process.poll() is None:
                log_debug('Stopping existing Godot process before starting a new one')
                self.stop_project()  # Use the stop_project method

            cmd_args = ['-d', '--path', projectPath]  # Debug mode is default for run_project
            if scene:
                log_debug(f"Adding scene parameter: {scene}")
            shell = platform.system() == 'Windows'
            command = [self.godot_path, *cmd_args]
            if shell:
                # Manually construct command string for Windows shell
                escaped_godot_path = f'"{self.godot_path}"'
                escaped_project_path = f'"{projectPath}"'
                command_str = f'{escaped_godot_path} -d --path {escaped_project_path}'
                if scene:
                    # Scene path likely doesn't need quotes unless it has spaces, but safer to add
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

            # Check if process exited immediately (e.g., command not found, invalid scene)
            time.sleep(0.2)  # Give it a moment to start or fail
            if process.poll() is not None:
                # Process exited, read any output/errors immediately
                self._read_output_queues()  # Ensure queues are read
                # Wait for threads to finish? Might hang if process died unexpectedly.
                # Let's just read what we have.
                stdout_str = "\n".join(self.active_process_output)
                stderr_str = "\n".join(self.active_process_errors)
                self.active_process = None  # Clear active process
                raise RuntimeError(
                    f"Godot process exited immediately with code {process.returncode}. "
                    f"Stdout: '{stdout_str[:200]}...' Stderr: '{stderr_str[:200]}...'"  # Truncate output in error
                )

            return {
                "message": "Godot project started in debug mode. Use get_debug_output to see output.",
            }
        except FileNotFoundError as e:
            log_debug(f"Error running project: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            # Ensure process is cleaned up if an error occurs during startup attempt
            if self.active_process:
                self.cleanup()
            log_debug(f"Failed to run project: {e}")
            raise RuntimeError(f"Failed to run Godot project: {e}") from e

    def get_debug_output(self) -> Dict[str, Any]:
        """Get the current debug output and errors from the running project"""
        if not self.active_process or self.active_process.poll() is not None:
            # Also check if process terminated since last check
            if self.active_process and self.active_process.poll() is not None:
                log_debug("Getting debug output, but process terminated.")
                # Read final output before reporting no active process
                self._read_output_queues()
                final_output = self.active_process_output[:]  # Copy lists
                final_errors = self.active_process_errors[:]
                self.cleanup()  # Ensure cleanup happens
                return {
                    "message": "Godot process terminated.",
                    "output": final_output,
                    "errors": final_errors
                }
            else:
                raise RuntimeError('No active Godot process. Use run_project first.')

        # Read any new output from the queues
        self._read_output_queues()

        # Return copies of the lists
        return {
            "output": self.active_process_output[:],
            "errors": self.active_process_errors[:],
        }

    def stop_project(self) -> Dict[str, Any]:
        """Stop the currently running Godot project"""
        if not self.active_process or self.active_process.poll() is not None:
            # If process terminated itself, allow getting final output via stop
            if self.active_process and self.active_process.poll() is not None:
                log_debug("Stop called, but process already terminated.")
                self._read_output_queues()  # Get final output
                final_output = self.active_process_output[:]
                final_errors = self.active_process_errors[:]
                self.cleanup()
                return {
                    "message": "Godot process already terminated",
                    "finalOutput": final_output,
                    "finalErrors": final_errors,
                }
            else:
                raise RuntimeError('No active Godot process to stop.')

        log_debug('Stopping active Godot process')
        # Read any remaining output before stopping
        self._read_output_queues()
        # Capture output before cleanup clears it
        final_output = self.active_process_output[:]
        final_errors = self.active_process_errors[:]

        self.cleanup()  # Use cleanup to terminate/kill

        return {
            "message": "Godot project stopped",
            "finalOutput": final_output,
            "finalErrors": final_errors,
        }

    def get_godot_version(self) -> str:
        """Get the installed Godot version"""
        try:
            if not self.godot_path:
                self.detect_godot_path_sync()  # Try detecting again
                if not self.godot_path:
                    raise RuntimeError('Could not find a valid Godot executable path')

            log_debug(f'Getting Godot version using: {self.godot_path}')
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
            log_debug(f"Get version stderr: {process.stderr.strip()}")  # Log stderr too

            # Return only the stripped stdout string
            return process.stdout.strip()

        except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as e:
            stderr_info = ""
            if isinstance(e, subprocess.CalledProcessError):
                stderr_info = f" Stderr: '{e.stderr.strip() if e.stderr else ''}'"
            log_debug(f"Get version failed with error: {e}{stderr_info}")
            raise RuntimeError(f"Failed to get Godot version: {e}{stderr_info}") from e
        except Exception as e:
            log_debug(f"Unexpected error getting Godot version: {e}")
            raise RuntimeError(f"Failed to get Godot version: {e}") from e

    def list_projects(self, directory: str, recursive: bool = False) -> List[Dict[str, str]]:
        """List Godot projects in a directory"""
        if not directory:
            raise ValueError('Directory is required')

        if not validate_path(directory):  # Basic validation still useful
            raise ValueError('Invalid directory path format (e.g., contains "..")')

        abs_directory = os.path.abspath(directory)  # Use absolute path

        try:
            log_debug(f"Listing Godot projects in directory: {abs_directory}, recursive={recursive}")
            if not os.path.exists(abs_directory):
                raise FileNotFoundError(f"Directory does not exist: {abs_directory}")
            if not os.path.isdir(abs_directory):
                raise NotADirectoryError(f"Path is not a directory: {abs_directory}")

            # Use the internal helper method
            projects = self.find_godot_projects(abs_directory, recursive)
            return projects  # Return the list of dicts directly

        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            log_debug(f"Error listing projects: {e}")
            raise  # Re-raise specific errors
        except Exception as e:
            log_debug(f"Failed to list projects: {e}")
            raise RuntimeError(f"Failed to list projects: {e}") from e

    def get_project_info(self, projectPath: str) -> Dict[str, Any]:
        """Retrieve metadata about a Godot project"""
        if not projectPath:
            raise ValueError('Project path is required')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath):  # Basic validation still useful
            raise ValueError('Invalid project path format (e.g., contains "..")')

        try:
            # No need to check godot_path here, get_godot_version will do it.

            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            log_debug(f"Getting project info for: {projectPath}")

            # Get Godot version using the dedicated tool method
            try:
                godot_version = self.get_godot_version()
            except Exception as e:
                log_debug(f"Get version failed during project info: {e}")
                # Continue with project info even if version fails, but report error
                godot_version = f"Error getting version: {e}"

            # Get project structure using the internal helper
            try:
                project_structure = self.get_project_structure(projectPath)
            except Exception as e:
                log_debug(f"Get structure failed during project info: {e}")
                project_structure = {"error": f"Failed to get structure: {e}"}

            # Extract project name from project.godot file
            project_name = os.path.basename(projectPath)  # Default name
            try:
                with open(project_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line_strip = line.strip()
                        # More robust parsing for application/config/name="Project Name"
                        if line_strip.startswith('config/name='):
                            parts = line_strip.split('=', 1)
                            if len(parts) > 1:
                                name_value = parts[1].strip()
                                # Handle quoted and unquoted names
                                if name_value.startswith('"') and name_value.endswith('"'):
                                    project_name = name_value[1:-1]
                                else:
                                    project_name = name_value  # Use as is if not quoted
                                    log_debug(f"Found project name in config: {project_name}")
                                    break  # Found the name, no need to read further
            except Exception as e:
                log_debug(f"Error reading project file name: {e}")
                # Continue with default project name if extraction fails

            return {
                "name": project_name,
                "path": projectPath,  # Return absolute path
                "godotVersion": godot_version,
                "structure": project_structure,
            }
        except FileNotFoundError as e:
            log_debug(f"Error getting project info: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to get project info: {e}")
            raise RuntimeError(f"Failed to get project info: {e}") from e

    def create_scene(self, projectPath: str, scenePath: str, rootNodeType: str = 'Node2D') -> str:
        """Create a new Godot scene file"""
        if not projectPath or not scenePath:
            raise ValueError('Project path and scene path are required')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        # scenePath is relative to project, validate format
        if not validate_path(projectPath) or not validate_path(scenePath):
            raise ValueError('Invalid path format (e.g., contains "..")')

        # Basic validation for scenePath extension
        if not scenePath.lower().endswith(".tscn"):
            log_debug(f"Scene path '{scenePath}' doesn't end with .tscn, appending.")
            scenePath += ".tscn"

        try:
            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            # Check if scene already exists? Overwrite is default behavior of script.
            full_scene_path = os.path.join(projectPath, scenePath)
            if os.path.exists(full_scene_path):
                log_debug(f"Scene already exists at {scenePath}, will be overwritten by Godot script.")

            params = {
                "scenePath": scenePath,  # Pass relative path to script
                "rootNodeType": rootNodeType,
            }

            stdout, stderr = self.execute_operation('create_scene', params, projectPath)

            # Check stderr for common Godot errors
            if stderr and ("error" in stderr.lower() or "failed" in stderr.lower()):
                # Improve error message using stderr content
                error_msg = f"Failed to create scene '{scenePath}'. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                # Distinguish specific errors if possible
                if "Invalid node type" in stderr:
                    raise ValueError(f"Invalid root node type: '{rootNodeType}'. Godot stderr: {stderr.strip()}")
                raise RuntimeError(error_msg)
            elif "Cannot create file" in stdout:  # Check stdout too for some errors
                error_msg = f"Failed to create scene file '{scenePath}'. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            # Return simple success message, stdout might contain details
            return f"Scene '{scenePath}' created successfully (Root: {rootNodeType}). Godot output: {stdout.strip()}"

        except FileNotFoundError as e:
            log_debug(f"Error creating scene: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to create scene: {e}")
            raise RuntimeError(f"Failed to create scene: {e}") from e

    def add_node(self, projectPath: str, scenePath: str, nodeType: str, nodeName: str, parentNodePath: str = 'root',
                 properties: Optional[Dict[str, Any]] = None) -> str:
        """Add a node to an existing scene"""
        if not projectPath or not scenePath or not nodeType or not nodeName:
            raise ValueError('Missing required parameters: projectPath, scenePath, nodeType, nodeName')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath) or not validate_path(scenePath):
            raise ValueError('Invalid path format (e.g., contains "..")')
        # nodeName, parentNodePath, nodeType usually don't need path validation

        try:
            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            scene_full_path = os.path.join(projectPath, scenePath)
            if not os.path.exists(scene_full_path):
                raise FileNotFoundError(f"Scene file does not exist: {scenePath}")

            params = {
                "scenePath": scenePath,  # Pass relative path
                "parentNodePath": parentNodePath,
                "nodeType": nodeType,
                "nodeName": nodeName,
                "properties": properties or {},  # Ensure properties is a dict
            }

            stdout, stderr = self.execute_operation('add_node', params, projectPath)

            # Check stderr/stdout for errors
            if stderr and ("error" in stderr.lower() or "failed" in stderr.lower()):
                error_msg = f"Failed to add node '{nodeName}'. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                if "Invalid node type" in stderr:
                    raise ValueError(f"Invalid node type: '{nodeType}'. Godot stderr: {stderr.strip()}")
                if "Parent node not found" in stderr:
                    raise ValueError(f"Parent node not found: '{parentNodePath}'. Godot stderr: {stderr.strip()}")
                if "Node name already exists" in stderr:
                    raise ValueError(
                        f"Node name '{nodeName}' already exists under parent '{parentNodePath}'. Godot stderr: {stderr.strip()}")

                raise RuntimeError(error_msg)
            elif "Failed to add node" in stdout:  # Check stdout too
                error_msg = f"Failed to add node '{nodeName}'. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            return f"Node '{nodeName}' ({nodeType}) added successfully to '{scenePath}' under '{parentNodePath}'. Godot output: {stdout.strip()}"

        except FileNotFoundError as e:
            log_debug(f"Error adding node: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to add node: {e}")
            raise RuntimeError(f"Failed to add node: {e}") from e

    def load_sprite(self, projectPath: str, scenePath: str, nodePath: str, texturePath: str) -> str:
        """Load a texture into a Sprite2D, Sprite3D or TextureRect node"""
        if not projectPath or not scenePath or not nodePath or not texturePath:
            raise ValueError('Missing required parameters: projectPath, scenePath, nodePath, texturePath')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if (
                not validate_path(projectPath) or
                not validate_path(scenePath) or
                not validate_path(texturePath)  # texturePath is relative to project
        ):
            raise ValueError('Invalid path format (e.g., contains "..")')
        # nodePath is internal scene path

        try:
            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            scene_full_path = os.path.join(projectPath, scenePath)
            if not os.path.exists(scene_full_path):
                raise FileNotFoundError(f"Scene file does not exist: {scenePath}")

            texture_full_path = os.path.join(projectPath, texturePath)
            if not os.path.exists(texture_full_path):
                raise FileNotFoundError(f"Texture file does not exist: {texturePath}")

            params = {
                "scenePath": scenePath,  # Relative paths for script
                "nodePath": nodePath,
                "texturePath": texturePath,
            }

            stdout, stderr = self.execute_operation('load_sprite', params, projectPath)

            # Check stderr/stdout for errors
            if stderr and ("error" in stderr.lower() or "failed" in stderr.lower()):
                error_msg = f"Failed to load sprite for node '{nodePath}'. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                if "Node not found" in stderr:
                    raise ValueError(f"Node not found at path: '{nodePath}'. Godot stderr: {stderr.strip()}")
                if "is not a Sprite" in stderr or "is not a TextureRect" in stderr:  # Check script's error message
                    raise TypeError(
                        f"Node '{nodePath}' is not a compatible type (Sprite2D, Sprite3D, TextureRect). Godot stderr: {stderr.strip()}")
                raise RuntimeError(error_msg)
            elif "Failed to load texture" in stdout or "Node not found" in stdout:  # Check stdout too
                error_msg = f"Failed to load sprite for node '{nodePath}'. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            return f"Texture '{texturePath}' loaded successfully into node '{nodePath}' in scene '{scenePath}'. Godot output: {stdout.strip()}"

        except (FileNotFoundError, TypeError) as e:
            log_debug(f"Error loading sprite: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to load sprite: {e}")
            raise RuntimeError(f"Failed to load sprite: {e}") from e

    def export_mesh_library(self, projectPath: str, scenePath: str, outputPath: str,
                            meshItemNames: Optional[List[str]] = None) -> str:
        """Export a scene as a MeshLibrary resource"""
        if not projectPath or not scenePath or not outputPath:
            raise ValueError('Missing required parameters: projectPath, scenePath, outputPath')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if (
                not validate_path(projectPath) or
                not validate_path(scenePath) or  # Relative path
                not validate_path(outputPath)  # Relative path
        ):
            raise ValueError('Invalid path format (e.g., contains "..")')

        # Ensure output path has .res extension? Godot script might handle it.
        if not outputPath.lower().endswith((".res", ".tres")):
            log_debug(f"MeshLibrary output path '{outputPath}' missing .res/.tres extension, appending .res")
            outputPath += ".res"

        try:
            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            scene_full_path = os.path.join(projectPath, scenePath)
            if not os.path.exists(scene_full_path):
                raise FileNotFoundError(f"Scene file does not exist: {scenePath}")

            params: Dict[str, Any] = {
                "scenePath": scenePath,  # Relative paths for script
                "outputPath": outputPath,
            }

            if meshItemNames is not None and isinstance(meshItemNames, list):
                # Ensure names are strings
                params["meshItemNames"] = [str(name) for name in meshItemNames]

            stdout, stderr = self.execute_operation('export_mesh_library', params, projectPath)

            # Check stderr/stdout for errors
            if stderr and ("error" in stderr.lower() or "failed" in stderr.lower()):
                error_msg = f"Failed to export MeshLibrary to '{outputPath}'. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)
            elif "Failed to export" in stdout or "No meshes found" in stdout:  # Check stdout too
                error_msg = f"Failed to export MeshLibrary to '{outputPath}'. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            return f"MeshLibrary exported successfully from '{scenePath}' to '{outputPath}'. Godot output: {stdout.strip()}"

        except FileNotFoundError as e:
            log_debug(f"Error exporting mesh library: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to export mesh library: {e}")
            raise RuntimeError(f"Failed to export mesh library: {e}") from e

    def save_scene(self, projectPath: str, scenePath: str, newPath: Optional[str] = None) -> str:
        """Save changes to a scene file, optionally to a new path"""
        if not projectPath or not scenePath:
            raise ValueError('Missing required parameters: projectPath, scenePath')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath) or not validate_path(scenePath):
            raise ValueError('Invalid path format (e.g., contains "..")')

        if newPath and not validate_path(newPath):
            raise ValueError('Invalid new path format (e.g., contains "..")')

        # Ensure paths have .tscn extension?
        if not scenePath.lower().endswith(".tscn"):
            log_debug(f"Scene path '{scenePath}' missing .tscn extension, operation might fail.")
        if newPath and not newPath.lower().endswith(".tscn"):
            log_debug(f"New scene path '{newPath}' missing .tscn extension, appending.")
            newPath += ".tscn"

        try:
            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            scene_full_path = os.path.join(projectPath, scenePath)
            if not os.path.exists(scene_full_path):
                raise FileNotFoundError(f"Scene file does not exist: {scenePath}")

            params: Dict[str, Any] = {
                "scenePath": scenePath,  # Relative paths for script
            }

            if newPath:
                params["newPath"] = newPath

            stdout, stderr = self.execute_operation('save_scene', params, projectPath)

            # Check stderr/stdout for errors
            if stderr and ("error" in stderr.lower() or "failed" in stderr.lower()):
                error_msg = f"Failed to save scene '{scenePath}'. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)
            elif "Failed to save scene" in stdout or "Cannot create file" in stdout:  # Check stdout too
                error_msg = f"Failed to save scene '{scenePath}'. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            save_target = newPath if newPath else scenePath
            return f"Scene saved successfully to '{save_target}'. Godot output: {stdout.strip()}"

        except FileNotFoundError as e:
            log_debug(f"Error saving scene: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to save scene: {e}")
            raise RuntimeError(f"Failed to save scene: {e}") from e

    def get_uid(self, projectPath: str, filePath: str) -> str:
        """Get the UID for a specific file in a Godot project (for Godot 4.4+)"""
        if not projectPath or not filePath:
            raise ValueError('Missing required parameters: projectPath, filePath')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath) or not validate_path(filePath):  # filePath is relative
            raise ValueError('Invalid path format (e.g., contains "..")')

        try:
            if not self.godot_path:
                self.detect_godot_path_sync()  # Try detecting again
                if not self.godot_path:
                    raise RuntimeError('Could not find a valid Godot executable path')

            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            file_full_path = os.path.join(projectPath, filePath)
            if not os.path.exists(file_full_path):
                raise FileNotFoundError(f"File does not exist: {filePath}")

            # Get Godot version to check if UIDs are supported
            try:
                version = self.get_godot_version()
            except Exception as e:
                raise RuntimeError(f"Failed to get Godot version to check UID support: {e}") from e

            if not is_godot_44_or_later(version):
                raise RuntimeError(f"UIDs require Godot 4.4+ (Current version: {version})")

            params = {
                "filePath": filePath,  # Relative path for script
            }

            stdout, stderr = self.execute_operation('get_uid', params, projectPath)

            # Check stderr/stdout for errors
            if stderr and ("error" in stderr.lower() or "failed" in stderr.lower()):
                error_msg = f"Failed to get UID for '{filePath}'. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                if "not a resource" in stderr:
                    raise ValueError(
                        f"File '{filePath}' is not a recognized Godot resource. Godot stderr: {stderr.strip()}")
                raise RuntimeError(error_msg)
            elif "Failed to get UID" in stdout or "not found" in stdout:  # Check stdout too
                error_msg = f"Failed to get UID for '{filePath}'. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            uid = stdout.strip()
            if not uid.startswith("uid://"):  # Basic validation of output
                log_debug(f"Got unexpected non-UID output: {uid}")
                raise RuntimeError(f"Command did not return a valid UID. Output: {uid}")

            return uid  # Return the UID string directly

        except FileNotFoundError as e:
            log_debug(f"Error getting UID: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to get UID: {e}")
            # Don't wrap RuntimeError in another RuntimeError
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"Failed to get UID: {e}") from e

    def update_project_uids(self, projectPath: str) -> str:
        """Update UID references in a Godot project by resaving resources (for Godot 4.4+)"""
        if not projectPath:
            raise ValueError('Project path is required')

        projectPath = os.path.abspath(projectPath)  # Use absolute path

        if not validate_path(projectPath):
            raise ValueError('Invalid project path format (e.g., contains "..")')

        try:
            if not self.godot_path:
                self.detect_godot_path_sync()  # Try detecting again
                if not self.godot_path:
                    raise RuntimeError('Could not find a valid Godot executable path')

            project_file = os.path.join(projectPath, 'project.godot')
            if not os.path.exists(project_file):
                raise FileNotFoundError(f"Not a valid Godot project (project.godot not found): {projectPath}")

            # Get Godot version to check if UIDs are supported
            try:
                version = self.get_godot_version()
            except Exception as e:
                raise RuntimeError(f"Failed to get Godot version to check UID support: {e}") from e

            if not is_godot_44_or_later(version):
                raise RuntimeError(f"UIDs require Godot 4.4+ (Current version: {version})")

            # Note: The script uses 'resave_resources' operation name
            # The 'projectPath' parameter for the script isn't needed as it's passed via --path
            params = {}  # No specific parameters needed for the script operation itself

            stdout, stderr = self.execute_operation('resave_resources', params, projectPath)

            # Check stderr/stdout for errors
            # This operation can be noisy, check for specific failure markers
            if stderr and ("error" in stderr.lower() or "failed to save" in stderr.lower()):
                error_msg = f"Failed to update project UIDs. Godot stderr: {stderr.strip()}"
                log_debug(error_msg)
                # Don't raise immediately on any stderr, as resave can have warnings.
                # Only raise on clear failure indicators.
                if "Failed to save resource" in stderr:  # Example failure indicator
                    raise RuntimeError(error_msg)

            # Check stdout for failure messages too
            if "Failed to resave resources" in stdout:
                error_msg = f"Failed to update project UIDs. Godot stdout: {stdout.strip()}"
                log_debug(error_msg)
                raise RuntimeError(error_msg)

            # Return success message, include stdout for info as it lists saved files
            return f"Project UIDs update process completed. Godot output:\n{stdout.strip()}"

        except FileNotFoundError as e:
            log_debug(f"Error updating UIDs: {e}")
            raise  # Re-raise specific error
        except Exception as e:
            log_debug(f"Failed to update project UIDs: {e}")
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"Failed to update project UIDs: {e}") from e


# Main execution block
if __name__ == "__main__":
    # Basic argument parsing for config (optional)
    config: Dict[str, Any] = {}
    # Example: Allow setting Godot path via argument
    if len(sys.argv) > 1:
        # Very basic arg parsing: --godot-path /path/to/godot
        try:
            idx = sys.argv.index('--godot-path')
            if idx + 1 < len(sys.argv):
                config['godotPath'] = sys.argv[idx + 1]
                print(f"[INFO] Using Godot path from command line: {config['godotPath']}", file=sys.stderr)
        except ValueError:
            pass  # Ignore if argument not found
        # Example: --strict-paths
        if '--strict-paths' in sys.argv:
            config['strictPathValidation'] = True
            print("[INFO] Strict path validation enabled via command line.", file=sys.stderr)

    # Instantiate the class (which initializes, detects Godot path, registers tools)
    try:
        godot_service = GodotMCP(config)
    except Exception as e:
        print(f"[FATAL] Failed to initialize GodotMCP service: {e}", file=sys.stderr)
        sys.exit(1)

    # Run the FastMCP server using stdio transport
    print("Starting FastMCP server for Godot service on stdio...", file=sys.stderr)
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        print(f"[FATAL] FastMCP server error: {e}", file=sys.stderr)
        # Ensure cleanup runs if server crashes
        godot_service.cleanup()
        sys.exit(1)
    finally:
        # This might not be reached if mcp.run blocks indefinitely until killed,
        # but atexit should handle cleanup.
        log_debug("FastMCP server finished.")
        # No explicit cleanup here needed if atexit is registered
