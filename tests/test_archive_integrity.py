from pathlib import Path

def test_required_files_exist():
    required = [
        "Dockerfile.mcp", "Dockerfile.graph", "docker-compose.yml",
        "src/fc27_mcp_server.py", "src/graph.py", "src/graph_api.py",
        "src/live_match_watcher.py", "src/live_ws_server.py",
        "src/post_match_rundown.py", "src/rtsp_frame_analyzer.py",
        "host_agent/capture_agent.py", "host_agent/install.ps1",
    ]
    assert not [name for name in required if not Path(name).is_file()]

def test_readme_references_existing_documents():
    text = Path("README.md").read_text()
    for name in ["config.example.env", "docker-compose.yml", "docker-compose.gpu.yml", "docker-compose.cpu.yml", "docs/VOICE_CAPABILITIES.md"]:
        assert name in text
        assert Path(name).exists()

def test_watcher_sends_graph_api_token():
    text = Path("src/live_match_watcher.py").read_text()
    assert "GRAPH_API_TOKEN" in text
    assert '"X-API-Key"' in text
