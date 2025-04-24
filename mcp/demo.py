# -*- coding: utf-8 -*-
"""
@author: Zed
@file: demo.py
@time: 2025/4/23 15:42
@describe:自定义描述
"""
# "D:\godot\Godot.exe" --headless --path "D:\godot_data\mcp" --script "C:\Users\admin\PycharmProjects\Work2024\ai_quant\主线\ai\mcp\src\scripts\godot_operations.gd" get_uid "{\"file_path\": \"res://main.gd\"}"

# "D:\godot\Godot.exe" --headless --path "D:\godot_data\mcp"
# --script "C:\Users\admin\PycharmProjects\Work2024\ai_quant\主线\ai\mcp\src\scripts\godot_operations.gd"
# get_uid "{\"file_path\": \"res://main.gd\"}"

# "D:\godot\Godot.exe" --headless --path "D:\godot_data\mcp" --script "C:\Users\admin\PycharmProjects\Work2024\ai_quant\主线\ai\mcp\src\scripts\godot_operations.gd" save_scene "{\"scene_path\": \"res://main.tscn\"}"
# {"jsonrpc": "2.0", "method": "save_scene", "params": {"project_path": "D:\\godot_data\\mcp", "scene_path": "res://main.tscn"}, "id": 2}
# tt
#{ "type": "call_tool", "id": 2,"params": {"name": "save_scene", "arguments": {"projectPath": "D:\\godot_data\\mcp",  "scenePath": "res://main.tscn"} } }
 # {"jsonrpc": "2.0","type": "call_tool", "params": {"name":"save_scene","arguments": {"projectPath": "D:\\godot_data\\mcp", "scenePath": "res://main.tscn"}}, "id": 2}
# {"jsonrpc": "2.0","type": "call_tool", "params": {"name": "create_scene", "arguments": {"projectPath": "D:\\godot_data\\mcp", "scenePath": "res://s2.tscn", "rootNodeType": "Node2D"}}, "id": 3}
# "command": "C:\\Users\\admin\\.conda\\envs\\py310a\\python.exe",
#           "args": ["D:\pywork\quant\a_mcp\save_godot.py"],

#  "D:\godot\Godot.exe" --headless --path "D:\godot_data\mcp" --script "D:\pywork\quant\a_mcp\scripts\godot_operations.gd" create_scene '{"scene_path": "res://s2.tscn", "root_node_type": "Node2D"}' --debug-godot

#  {"jsonrpc": "2.0","type": "call_tool", "params": {"name": "create_scene", "arguments": {"projectPath": "D:\\godot_data\\mcp", "scenePath": "res://s5.tscn", "rootNodeType": "Node2D"}}, "id": 3}
# {"params": {"name": "create_scene", "arguments": {"projectPath": "D:\\godot_data\\mcp", "scenePath": "res://s5.tscn", "rootNodeType": "Node2D"}},"id": 3}

